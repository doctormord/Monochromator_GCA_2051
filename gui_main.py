"""
gui_main.py — Qt6 UI + wiring of the VRS41 controller (Stage C).

WHAT THIS MODULE DOES
---------------------
Builds the complete PyQt6 window (header + live readouts, left a pyqtgraph plot
and console as tabs, right a SCROLLABLE panel with COLLAPSIBLE sections:
Connection / Move / Scan / Averaging / Queue / Backlash / DAQ / Export, and a
status bar at the bottom) and WIRES the foundation/orchestration modules to it --
through exactly the same three seams as the old Tk version:

  - registers the console as the log sink (app_context.set_log_sink);
  - registers the value vars into the ui registry (register_var) -> motion/daq
    read them via ui_get;
  - registers widget objects into app_context.refs -> scan_engine drives them
    duck-typed (.get()/.delete()/.insert()/.configure()/listbox/messagebox API);
  - registers the GUI hooks (set_buttons_*, set_direction_label,
    set_reference_button_enabled, show_warning/error/info, ui_after).

WHY ADAPTERS + THREAD MARSHALLING (the actual Qt port)
------------------------------------------------------
scan_engine/motion/daq stay UNCHANGED in THIS lift. They expect the Tkinter
widget API (entry.get()/delete()/insert(), btn.configure(state=...),
listbox.insert/curselection/size/get/delete, messagebox.show*/askyesno) and call
it partly FROM WORKER THREADS (e.g. entry_current during a scan, set_buttons_*
after each step). Tkinter tolerates cross-thread access largely; Qt does NOT --
touching a widget from a foreign thread can crash Qt. Therefore:

  1. Every Qt widget the logic talks to is wrapped in a thin adapter that mimics
     the Tk API.
  2. Each adapter holds a Python-side SHADOW value. Reading (.get()) goes against
     the shadow (thread-safe, no Qt read needed); writing updates the shadow
     IMMEDIATELY and MARSHALS the actual widget change via _Invoker onto the GUI
     thread.
  3. ui_hook callbacks and ui_after also run via _Invoker on the GUI thread.

This keeps the whole logic layer untouched while the GUI stays Qt-thread-safe.
In the next step (real Qt signals in scan_engine) the adapters can go away; until
then they are the deliberately thin bridge.

Origin: replaces the Tkinter version (kept as gui_main_tk.py for reference, doc
policy: don't delete). PlotManager reimplemented on pyqtgraph with the same public
API as before (start_new_scan/append_scan_point/finalize_scan/append_jog_point/
clear_all/get_points/_schedule_redraw/_lock/_plots) so scan_engine reuses it 1:1.

NOTE (doc policy, see HANDOVER.md): docstrings/comments are preserved.
"""

import os
import threading
import time

import serial

import pyqtgraph as pg
from PyQt6 import QtCore, QtGui, QtWidgets
from PyQt6.QtCore import Qt, QObject, QThread, QTimer, pyqtSignal

import app_context
from app_context import (
    state, log, set_fsm, register_var, register_hook, refs, set_log_sink,
)
from device_constants import (
    DEFAULT_PORT, DEFAULT_BAUD, SUPPRESS_TIMEOUT_MESSAGES,
    AVG_SAMPLES_DEFAULT, AVG_MODE_DEFAULT, AVG_FRACTION_PCT_DEFAULT,
    PRESETTLE_MS_DEFAULT,
    CAL_CENTER_NM_DEFAULT, CAL_SPAN_NM_DEFAULT, CAL_STEP_NM_DEFAULT,
    CAL_DWELL_S_DEFAULT, RAMP_SP,
    AI_MONITOR_ENABLED, AI_MONITOR_HZ, AI_WARN_V, AI_ALARM_V, AI_BAR_MAX_V,
    C_BG, C_PANEL, C_PANEL2, C_BORDER, C_TEXT, C_MUTED,
    C_ACCENT, C_ACCENT_D, C_ON_ACCENT, C_WARN, C_ERROR, C_OK, C_AMBER,
    C_DISABLED_FG, C_DISABLED_BD, C_LEGEND_BG, PLOT_PALETTE,
    SEND_LEGACY_HP0,
)
import app_config
from protocol_faulhaber import (
    ser_ok, safe_stop, init_motor, drain_stale_serial, read_temperature,
)
from motion import reference_run_action, estimate_free_run_resolution_nm, set_current_wavelength
from daq import read_pmt_voltage, daq_source_label, DAQ_AVAILABLE
import backlash_cal
from scan_engine import (
    goto_wavelength_action, scan_action, free_run_scan_action, pause_action, resume_action,
    stop_action, jog_minus_action, jog_plus_action, mark_point_action,
    add_scan_to_queue, remove_selected_scan, run_scan_queue_action,
    clear_scan_queue, clear_plot_action, export_active_scan_csv,
    update_selected_scan,
)


# =====================================================================
# THEME (PS-2600A style: dark lab dashboard, teal accent)
# =====================================================================
# Central colors so the plot and QSS share the same look.
# Colours now live in device_constants.py (UI THEME section) so the whole look
# can be adjusted in one place by hand. Imported below with the other
# constants; these names are used unchanged throughout this file.

_QSS = f"""
QWidget {{
    background: {C_BG};
    color: {C_TEXT};
    /* Segoe UI is Windows-only; -apple-system/Helvetica Neue cover macOS,
       DejaVu/Noto cover Linux. Avoids the same alias-table lookup cost. */
    font-family: "Segoe UI", "-apple-system", "Helvetica Neue", "DejaVu Sans", "Noto Sans", sans-serif;
    font-size: 13px;
}}
QLabel#Title {{ font-size: 16px; font-weight: 600; }}
QLabel#TitleDim {{ color: {C_MUTED}; font-size: 16px; }}
QLabel#SectionHeader {{
    color: {C_MUTED}; font-size: 11px; font-weight: 700;
    letter-spacing: 1px; padding: 2px 0;
}}
QLabel#ReadoutCap {{ color: {C_MUTED}; font-size: 10px; letter-spacing: 1px; }}
QLabel#ReadoutVal {{ font-size: 20px; font-weight: 600; }}
QLabel#ReadoutValAccent {{ font-size: 20px; font-weight: 600; color: {C_ACCENT}; }}
QLabel#Unit {{ color: {C_MUTED}; }}
QFrame#Card, QFrame#Section {{
    background: {C_PANEL}; border: 1px solid {C_BORDER}; border-radius: 8px;
}}
QLineEdit, QComboBox, QListWidget, QPlainTextEdit {{
    background: {C_PANEL2}; border: 1px solid {C_BORDER};
    border-radius: 5px; padding: 4px 6px; selection-background-color: {C_ACCENT_D};
}}
QLineEdit:focus, QComboBox:focus {{ border: 1px solid {C_ACCENT}; }}
/* Mono stack covering all three platforms. "Consolas" alone (Windows-only)
   made Qt on macOS/Linux fall back through its alias tables on every start:
   qt.qpa.fonts: Populating font family aliases took 56 ms. */
QPlainTextEdit {{ font-family: "Menlo", "SF Mono", "Consolas", "DejaVu Sans Mono", "Liberation Mono", monospace; font-size: 12px; }}
QPushButton {{
    background: {C_PANEL2}; border: 1px solid {C_BORDER};
    border-radius: 5px; padding: 6px 12px;
}}
QPushButton:hover {{ border: 1px solid {C_ACCENT}; }}
QPushButton:disabled {{ color: {C_DISABLED_FG}; border-color: {C_DISABLED_BD}; }}
QPushButton#Primary {{
    background: {C_ACCENT_D}; border: 1px solid {C_ACCENT};
    color: {C_ON_ACCENT}; font-weight: 700; padding: 9px 12px;
}}
QPushButton#Primary:hover {{ background: {C_ACCENT}; }}
QPushButton#Danger {{ color: {C_ERROR}; border: 1px solid {C_ERROR}; }}
QPushButton#SectionToggle {{
    text-align: left; background: transparent; border: none;
    color: {C_MUTED}; font-size: 11px; font-weight: 700; letter-spacing: 1px;
    padding: 8px 4px;
}}
QPushButton#SectionToggle:hover {{ color: {C_TEXT}; }}
QTabBar::tab {{
    background: transparent; color: {C_MUTED};
    padding: 6px 14px; border-bottom: 2px solid transparent;
}}
QTabBar::tab:selected {{ color: {C_TEXT}; border-bottom: 2px solid {C_ACCENT}; }}
QTabWidget::pane {{ border: none; }}
QScrollArea {{ border: none; }}
QCheckBox::indicator {{
    width: 16px; height: 16px; border: 1px solid {C_BORDER};
    border-radius: 4px; background: {C_PANEL2};
}}
QCheckBox::indicator:checked {{ background: {C_ACCENT}; border-color: {C_ACCENT}; }}
QStatusBar {{ background: {C_PANEL}; border-top: 1px solid {C_BORDER}; color: {C_MUTED}; }}
QStatusBar QLabel {{ color: {C_MUTED}; }}
"""


# =====================================================================
# GUI-THREAD MARSHALLER
# =====================================================================
class _Invoker(QObject):
    """Run arbitrary callables on the GUI thread.

    WHERE/WHEN: the logic modules (scan_engine/motion) call ui_hook(...) and the
    widget adapters FROM WORKER THREADS. Qt widgets may only be changed from the
    GUI thread, so every such call is delivered here via a signal (AutoConnection
    -> QueuedConnection across thread boundaries) and executed on the GUI thread.
    WHY: prevents Qt crashes from cross-thread widget access (see module docstring)."""
    _sig = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self._sig.connect(self._run)

    def _run(self, fn):
        try:
            fn()
        except Exception as e:
            print(f"[GUI] marshalled call failed: {e}")

    def post(self, fn):
        """Run fn() on the GUI thread for sure (non-blocking)."""
        self._sig.emit(fn)


def _on_gui_thread():
    """True if the current thread is the Qt GUI thread. Used to skip the
    (otherwise deadlocking) blocking when a call is already on the GUI thread."""
    app = QtWidgets.QApplication.instance()
    return app is not None and QThread.currentThread() == app.thread()


# =====================================================================
# TK-COMPATIBLE WIDGET ADAPTERS (shadow value + GUI-thread marshalling)
# =====================================================================
class _EntryAdapter:
    """Maps the tk.Entry/StringVar API onto a QLineEdit.

    USED BY: scan_engine (entry_current/goto/start/end/step/wait) and ui_get
    (entry_wait as a var). API: get(), delete(first,last), insert(index,text).
    The shadow string is the source of truth for reads (thread-safe); writes
    update the shadow immediately and marshal setText onto the GUI thread. User
    edits in the field update the shadow via textChanged (runs on the GUI thread)."""

    def __init__(self, qle: QtWidgets.QLineEdit, initial: str = ""):
        self._w = qle
        # If no explicit initial is passed, adopt the widget's CURRENT text as
        # the shadow's starting value. Without this, a field that was pre-filled
        # from saved settings (e.g. Step = "0.1" after a restart) kept an EMPTY
        # shadow until the user edited it, because textChanged only fires on
        # FUTURE edits -- so get() returned "" and scan validation failed with
        # "start/end/step must be numbers" until the value was retyped. That was
        # the "0.1 is shown but Scan won't start; change it to 0.10 or retype
        # and it works" bug: Start/End got retyped every scan so their shadows
        # were populated, but Step was usually left at its default and stayed "".
        if not initial:
            initial = qle.text()
        self._value = str(initial)
        if initial:
            qle.setText(str(initial))
        qle.textChanged.connect(self._on_text_changed)

    def _on_text_changed(self, txt):
        self._value = txt

    def get(self):
        return self._value

    def delete(self, first, last=None):
        # The logic only ever calls delete(0, "end") -> clear the whole field.
        if first in (0, "0") and (last in ("end", None) or last == "end"):
            self._value = ""
        else:
            try:
                a = int(first)
                b = len(self._value) if last in ("end", None) else int(last)
                self._value = self._value[:a] + self._value[b:]
            except Exception:
                self._value = ""
        _invoker.post(lambda v=self._value: self._w.setText(v))

    def insert(self, index, text):
        try:
            i = len(self._value) if index in ("end", None) else int(index)
        except Exception:
            i = len(self._value)
        self._value = self._value[:i] + str(text) + self._value[i:]
        _invoker.post(lambda v=self._value: self._w.setText(v))


class _EntryAdapterMsToSec(_EntryAdapter):
    """Same as _EntryAdapter, but the widget displays/accepts MILLISECONDS
    while get() returns the value in SECONDS as text.

    USED BY: entry_wait (the "Wait / dwell" field). The user wanted to enter
    dwell in ms; scan_engine.validate_scan_inputs(), motion.timeout_for_nm()
    (via ui_get('entry_wait', ...)), and mark_point_action() all already
    consume this value as SECONDS and are otherwise unmodified -- so the
    ms<->s conversion happens ONLY here, at the single point where the GUI
    hands the value to the logic layer. Nothing ever writes into this field
    from the backend (only the user types into it), so only get() needs the
    conversion; insert()/delete() are inherited unchanged."""

    def get(self):
        try:
            return str(float(super().get()) / 1000.0)
        except Exception:
            return "0"


class _VarAdapter:
    """StringVar-like value holder with a shadow, coupled to a Qt widget.

    USED BY: register_var values (avg_mode/avg_samples/avg_fraction/presettle/
    slip) for ui_get, plus internal vars (export_dir/pattern/autosave/daq_*).
    get() reads the shadow; set(v) sets the shadow + marshals the widget update
    (if a setter is given). Widget signals call update()."""

    def __init__(self, initial, widget_setter=None):
        self._value = str(initial)
        self._setter = widget_setter  # callable(str), invoked on the GUI thread

    def get(self):
        return self._value

    def set(self, v):
        self._value = str(v)
        if self._setter is not None:
            _invoker.post(lambda val=self._value: self._setter(val))

    def update(self, v):
        """Called from the Qt signal (GUI thread): update the shadow without
        re-marshalling (prevents signal loops)."""
        self._value = str(v)


class _ButtonAdapter:
    """Maps btn.configure(state=...) onto QPushButton.setEnabled.
    USED BY: set_buttons_*/scan_engine, often from worker threads -> setEnabled is
    marshalled onto the GUI thread."""

    def __init__(self, qbtn: QtWidgets.QPushButton):
        self._w = qbtn

    def configure(self, **kw):
        if "state" in kw:
            enabled = (kw["state"] == "normal")
            _invoker.post(lambda e=enabled: self._w.setEnabled(e))


class _ListboxAdapter:
    """Maps the tk.Listbox API onto a QListWidget.
    USED BY: scan_engine queue (insert("end"/i,txt), curselection(), delete(idx),
    delete(0,"end"), size(), get(i)). The shadow list is the read source; all
    mutations marshal onto the widget. curselection reads a shadow index kept via
    currentRowChanged (GUI thread)."""

    def __init__(self, qlw: QtWidgets.QListWidget):
        self._w = qlw
        self._items = []      # Shadow of the row strings
        self._cur = -1        # Shadow of the currently selected index
        qlw.currentRowChanged.connect(self._on_row_changed)

    def _on_row_changed(self, row):
        self._cur = row

    def insert(self, index, text):
        if index in ("end", None):
            self._items.append(str(text))
            _invoker.post(lambda t=str(text): self._w.addItem(t))
        else:
            i = int(index)
            self._items.insert(i, str(text))
            _invoker.post(lambda i=i, t=str(text): self._w.insertItem(i, t))

    def delete(self, first, last=None):
        if first in (0, "0") and last in ("end", None) and last is not None:
            # delete(0, "end") -> clear all
            self._items = []
            self._cur = -1
            _invoker.post(self._w.clear)
        else:
            i = int(first)
            if 0 <= i < len(self._items):
                self._items.pop(i)
            _invoker.post(lambda i=i: self._w.takeItem(i))

    def curselection(self):
        return (self._cur,) if self._cur is not None and self._cur >= 0 else ()

    def selection_set(self, index):
        """Restore a selection (used after the queue list is rebuilt in place,
        so 'Update selected' does not silently drop the user's selection)."""
        try:
            i = int(index)
        except Exception:
            return
        if 0 <= i < len(self._items):
            self._cur = i
            _invoker.post(lambda i=i: self._w.setCurrentRow(i))

    def size(self):
        return len(self._items)

    def get(self, i):
        try:
            return self._items[int(i)]
        except Exception:
            return ""


class _MessageboxShim:
    """Maps tkinter.messagebox.show*/askyesno onto QMessageBox.
    USED BY: scan_engine (showwarning/showerror/showinfo/askyesno). The show*
    variants are non-blocking and marshalled onto the GUI thread. askyesno MUST
    return a bool synchronously; when called from a worker thread the dialog is
    shown on the GUI thread and the worker blocks on an event until the user
    answers."""

    def _show(self, kind, title, msg):
        def do():
            box = QtWidgets.QMessageBox(_window)
            box.setWindowTitle(str(title))
            box.setText(str(msg))
            icon = {"warn": QtWidgets.QMessageBox.Icon.Warning,
                    "error": QtWidgets.QMessageBox.Icon.Critical,
                    "info": QtWidgets.QMessageBox.Icon.Information}.get(kind)
            if icon:
                box.setIcon(icon)
            box.exec()
        _invoker.post(do)

    def showwarning(self, title, msg):
        self._show("warn", title, msg)

    def showerror(self, title, msg):
        self._show("error", title, msg)

    def showinfo(self, title, msg):
        self._show("info", title, msg)

    def askyesno(self, title, msg):
        result = {"v": False}
        if _on_gui_thread():
            r = QtWidgets.QMessageBox.question(
                _window, str(title), str(msg),
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No)
            return r == QtWidgets.QMessageBox.StandardButton.Yes
        ev = threading.Event()

        def do():
            r = QtWidgets.QMessageBox.question(
                _window, str(title), str(msg),
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No)
            result["v"] = (r == QtWidgets.QMessageBox.StandardButton.Yes)
            ev.set()
        _invoker.post(do)
        ev.wait()
        return result["v"]


# =====================================================================
# PLOT-MANAGER (pyqtgraph, same public API as the Tk version)
# =====================================================================
class PlotManager:
    """Multi-scan plot on pyqtgraph, thread-safe.

    Same public API as the old matplotlib version so scan_engine stays unchanged:
    start_new_scan/set_active/append_scan_point/finalize_scan/append_jog_point/
    clear_all/get_points/_schedule_redraw plus the attributes _lock and _plots
    (scan_engine reads _plots.keys() under _lock).

    THREADING: data points come from worker threads -> only the Python lists are
    changed under _lock and a dirty flag is set. The actual curve.setData()/
    addItem() happens in the QTimer-driven _refresh() or via _invoker on the GUI
    thread. So pyqtgraph is never touched from a foreign thread."""

    _PALETTE = PLOT_PALETTE

    def __init__(self, plot_item: pg.PlotItem):
        self._pi = plot_item
        self._lock = threading.Lock()
        self._plots = {}          # pid -> {"x","y","curve","label","status","meta"}
        self._active_id = None
        self._dirty = False
        # jog layer (markers only)
        self._jog = {"x": [], "y": [],
                     "scatter": pg.ScatterPlotItem(size=7, brush=pg.mkBrush(C_ACCENT))}
        self._pi.addItem(self._jog["scatter"])
        # redraw timer (~15 Hz) on the GUI thread
        self._timer = QTimer()
        self._timer.timeout.connect(self._refresh)
        self._timer.start(66)

    def _schedule_redraw(self):
        """Compat hook: scan_engine calls this after jog/mark. The QTimer runs
        continuously anyway; here it is enough to set dirty."""
        self._dirty = True

    def _refresh(self):
        """GUI thread (QTimer): create any pending curves and, if dirty, re-set
        all series + jog."""
        with self._lock:
            for meta in self._plots.values():
                if meta["curve"] is None:
                    color = self._PALETTE[(len(self._pi.listDataItems())) % len(self._PALETTE)]
                    meta["curve"] = self._pi.plot([], [], pen=pg.mkPen(color, width=2),
                                                  name=meta["label"])
            if not self._dirty:
                return
            for meta in self._plots.values():
                if meta["curve"] is not None:
                    meta["curve"].setData(meta["x"], meta["y"])
            self._jog["scatter"].setData(self._jog["x"], self._jog["y"])
            self._dirty = False

    def start_new_scan(self, label: str, meta: dict) -> str:
        """Create a new series. The curve object is created LAZILY in _refresh
        (GUI thread); here only the dict entry is created synchronously so the
        calling worker thread gets the pid immediately and can append points."""
        pid = f"scan_{len(self._plots) + 1}"
        with self._lock:
            self._plots[pid] = {"x": [], "y": [], "curve": None,
                                "label": label, "status": "active",
                                "meta": dict(meta or {})}
            self._active_id = pid
            self._dirty = True
        return pid

    def set_active(self, plot_id: str):
        with self._lock:
            if plot_id in self._plots:
                self._active_id = plot_id

    def append_scan_point(self, plot_id: str, x_nm: float, y_val: float):
        with self._lock:
            meta = self._plots.get(plot_id)
            if not meta or meta["status"] != "active":
                return
            meta["x"].append(float(x_nm))
            meta["y"].append(float(y_val))
            self._dirty = True

    def finalize_scan(self, plot_id: str, status: str):
        with self._lock:
            meta = self._plots.get(plot_id)
            if meta:
                meta["status"] = status

    def append_jog_point(self, x_nm: float, y_val: float):
        with self._lock:
            self._jog["x"].append(float(x_nm))
            self._jog["y"].append(float(y_val))
            self._dirty = True

    def list_scans(self):
        """[(pid, label, visible), ...] in insertion order, for the Traces menu."""
        with self._lock:
            return [(pid, m.get("label", pid),
                     bool(m.get("curve") is None or m["curve"].isVisible()))
                    for pid, m in self._plots.items()]

    def set_scan_visible(self, pid, visible: bool):
        """Show/hide ONE trace without discarding its data, so it can be
        brought back. Curve visibility is a pyqtgraph call, so it is
        marshalled onto the GUI thread like every other plot mutation."""
        with self._lock:
            meta = self._plots.get(pid)
            if not meta:
                return
            curve = meta.get("curve")
            meta["hidden"] = not visible
        if curve is not None:
            _invoker.post(lambda: curve.setVisible(visible))

    def remove_scan(self, pid):
        """Discard ONE scan entirely: its curve, its points and its legend
        entry -- without this, the only option is Clear Plot, which throws
        away every trace, so a single bad sweep among several good ones could
        not be taken out of the picture on its own."""
        with self._lock:
            meta = self._plots.pop(pid, None)
            if self._active_id == pid:
                self._active_id = None
            self._dirty = True
        if not meta:
            return
        curve = meta.get("curve")

        def do():
            if curve is not None:
                try:
                    # This also drops the legend entry: PlotItem.removeItem()
                    # ends with `if self.legend is not None:
                    # self.legend.removeItem(item)` (verified in the pyqtgraph
                    # 0.14.0 source, and measured -- removing one of three
                    # traces takes the legend from 3 entries to 2 with no
                    # further call). An extra _plot_legend.removeItem() here
                    # was redundant and has been dropped.
                    self._pi.removeItem(curve)
                except Exception:
                    pass
        _invoker.post(do)

    def clear_all(self):
        """Remove all series. removeItem/clear must run on the GUI thread; the
        shadow structure is cleared immediately."""
        with self._lock:
            self._plots.clear()
            self._jog["x"] = []
            self._jog["y"] = []
            self._active_id = None
            self._dirty = True

        def do():
            self._pi.clear()
            self._jog["scatter"] = pg.ScatterPlotItem(size=7, brush=pg.mkBrush(C_ACCENT))
            self._pi.addItem(self._jog["scatter"])
            # _pi.clear() also strips the measurement crosshair InfiniteLines,
            # which were added to this same PlotItem at build time. Without
            # re-attaching them here the crosshair vanished permanently after a
            # Clear Plot and only returned on an app restart (the reported bug).
            # Re-add them (their visibility state is preserved, so they simply
            # reappear on the next mouse-move over the scope). Guarded because
            # these module globals are defined just below this class and re-add
            # of an already-detached item is a no-op.
            for _ln in (_crosshair_v, _crosshair_h):
                try:
                    self._pi.addItem(_ln, ignoreBounds=True)
                except Exception:
                    pass
        _invoker.post(do)

    def get_points(self, plot_id):
        """xs, ys of a series from the internal store (thread-safe)."""
        with self._lock:
            rec = self._plots.get(plot_id)
            if rec:
                return list(rec.get("x") or []), list(rec.get("y") or [])
        return [], []


# =====================================================================
# COLLAPSIBLE SECTION (for the right-hand scroll panel)
# =====================================================================
class CollapsibleSection(QtWidgets.QWidget):
    """Collapsible section with a header (title + arrow) and a content area.

    USED BY: the right panel is several of these in a QScrollArea -- so ALL
    controls fit without the grid overflowing (as it did in the Tk version). The
    user simply collapses rarely used blocks."""

    def __init__(self, title: str, collapsed: bool = False):
        super().__init__()
        self._body = QtWidgets.QWidget()
        self._form = QtWidgets.QFormLayout(self._body)
        self._form.setContentsMargins(6, 2, 6, 8)
        self._form.setVerticalSpacing(6)
        self._form.setFieldGrowthPolicy(
            QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        # When the panel gets narrow, wrap the label ABOVE its field instead of
        # letting the label+field row overflow and clip (the "labels chopped to
        # 'TION' / fields cut off" the user reported). WrapLongRows keeps them
        # side-by-side at normal width and only stacks when there isn't room.
        self._form.setRowWrapPolicy(
            QtWidgets.QFormLayout.RowWrapPolicy.WrapLongRows)
        self._form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)

        self._toggle = QtWidgets.QPushButton(("▸ " if collapsed else "▾ ") + title)
        self._toggle.setObjectName("SectionToggle")
        self._toggle.setCheckable(True)
        self._toggle.setChecked(not collapsed)
        self._title = title
        self._toggle.clicked.connect(self._on_toggle)

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self._toggle)
        lay.addWidget(self._body)
        self._body.setVisible(not collapsed)

    def _on_toggle(self):
        vis = self._toggle.isChecked()
        self._body.setVisible(vis)
        self._toggle.setText(("▾ " if vis else "▸ ") + self._title)

    def add_row(self, label, widget):
        """Add a labelled row (label may be '' for full width)."""
        if label:
            self._form.addRow(label, widget)
        else:
            self._form.addRow(widget)

    def add_widget(self, widget):
        """Add a widget across the full width (no label)."""
        self._form.addRow(widget)


def _mk_row2(a, b):
    """Small helper: two widgets side by side in a container (for button pairs
    like Jog-/Jog+ or Add/Remove)."""
    w = QtWidgets.QWidget()
    h = QtWidgets.QHBoxLayout(w)
    h.setContentsMargins(0, 0, 0, 0)
    h.setSpacing(6)
    h.addWidget(a)
    h.addWidget(b)
    return w


# =====================================================================
# GUI BUILD
# =====================================================================
pg.setConfigOptions(antialias=True, background=C_BG, foreground=C_MUTED)

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
app.setStyleSheet(_QSS)

_invoker = _Invoker()

_window = QtWidgets.QMainWindow()
_window.setWindowTitle("Monochromator Controller — VRS41")
_window.resize(1180, 720)
# Minimum size so the layout can't be squeezed to the point where the right
# panel's controls get clipped -- below this the panel + plot simply don't
# both fit. The user's broken screenshot was a ~540px-wide window; this floor
# keeps that from happening.
_window.setMinimumSize(900, 560)
_central = QtWidgets.QWidget()
_window.setCentralWidget(_central)
_root_lay = QtWidgets.QVBoxLayout(_central)
_root_lay.setContentsMargins(12, 10, 12, 6)
_root_lay.setSpacing(8)


# ---- Header bar: title + live readouts (Current lambda / State) ----
_header = QtWidgets.QHBoxLayout()
_title = QtWidgets.QLabel("Monochromator")
_title.setObjectName("Title")
_title_dim = QtWidgets.QLabel("· VRS41 · Lab Controller")
_title_dim.setObjectName("TitleDim")
_header.addWidget(_title)
_header.addWidget(_title_dim)
_header.addStretch(1)


def _apply_tabular_numbers(label, sample: str, extra_px: int = 0, bold: bool = True):
    """Stop a frequently-updated numeric label from changing width.

    THE PROBLEM: the UI fonts render digits PROPORTIONALLY -- "1" is visibly
    narrower than "8" in Helvetica Neue / SF (macOS) and Segoe UI (Windows).
    A value that refreshes several times a second therefore changes width on
    almost every update, so the text jitters and everything laid out after it
    shifts sideways. Most obvious on the AI readout in the status bar (4 Hz)
    and on the large header values.

    TWO MEASURES, because either alone is not enough:
      1. Request the OpenType `tnum` feature (tabular figures), which makes
         every digit the same width. Fonts that lack the feature simply ignore
         it -- which is why measure 2 exists.
      2. Pin a minimum width computed from `sample`, a worst-case string for
         this label. Even with a font that has no tabular figures, the label
         can then no longer shrink below that width, so neighbouring widgets
         stay put. Only the digits themselves can still shift within the
         reserved space.

    `sample` should be the widest value the label will realistically show
    (all-8 digits, including sign and unit), not a typical one."""
    # ensurePolished() first: the font size of these labels comes from the
    # stylesheet (e.g. QLabel#ReadoutVal is 20px), and until the widget has
    # been polished label.font() still reports the 13px application default.
    # Measuring before that reserved barely half the width actually needed.
    try:
        label.ensurePolished()
    except Exception:
        pass
    f = QtGui.QFont(label.font())
    try:
        f.setFeature(QtGui.QFont.Tag("tnum"), 1)   # Qt 6.7+
    except Exception:
        pass                                       # older Qt: measure 2 carries it
    label.setFont(f)
    try:
        # Measure BOLD by default: every one of these labels renders its value
        # in bold or semibold (the header readouts via the stylesheet, the AI
        # value via inline <b>), and bold digits are wider than regular ones.
        # Measuring the regular weight reserved ~1 px too little, so the label
        # still grew on the widest values -- which defeats the whole point.
        fm_font = QtGui.QFont(f)
        if bold:
            fm_font.setBold(True)
        fm = QtGui.QFontMetrics(fm_font)
        label.setMinimumWidth(fm.horizontalAdvance(sample) + extra_px)
    except Exception:
        pass


def _mk_readout(cap, accent=False):
    """One readout block (small cap + big value) for the header."""
    box = QtWidgets.QVBoxLayout()
    box.setSpacing(0)
    lc = QtWidgets.QLabel(cap)
    lc.setObjectName("ReadoutCap")
    lc.setAlignment(Qt.AlignmentFlag.AlignRight)
    lv = QtWidgets.QLabel("—")
    lv.setObjectName("ReadoutValAccent" if accent else "ReadoutVal")
    lv.setAlignment(Qt.AlignmentFlag.AlignRight)
    box.addWidget(lc)
    box.addWidget(lv)
    return box, lv


_ro_lambda_box, ro_lambda = _mk_readout("CURRENT  λ (nm)", accent=True)
_ro_state_box, ro_state = _mk_readout("STATE")
# Crosshair readouts (PS-2600A style): live cursor position over the plot.
# ro_cursor_nm shows the wavelength under the mouse, ro_cursor_v the plotted
# intensity/voltage at that x on the active trace (see _on_plot_mouse_moved).
_ro_cursor_nm_box, ro_cursor_nm = _mk_readout("CURSOR  λ (nm)", accent=True)
_ro_cursor_v_box, ro_cursor_v = _mk_readout("CURSOR  V")
# Pin the numeric header readouts so they stop jittering as digits change.
# Samples are worst-case: 4 integer digits + decimals, and a leading minus for
# the voltage.
# extra_px=6: these labels are styled font-weight 600, which this font
# renders a hair wider than Qt's synthesised bold used for the measurement.
# The margin is cheaper and more robust than trying to reproduce the exact
# stylesheet weight in QFontMetrics.
_apply_tabular_numbers(ro_lambda,    "8888.888", 6)
_apply_tabular_numbers(ro_cursor_nm, "8888.888", 6)
_apply_tabular_numbers(ro_cursor_v,  "-8.8888", 6)
for b in (_ro_lambda_box, _ro_state_box, _ro_cursor_nm_box, _ro_cursor_v_box):
    _header.addLayout(b)
    _header.addSpacing(18)

# Emergency stop, top-right after the readouts. Always reachable (not buried
# in the SCAN section), always enabled while connected -- halt the motor NOW
# if a scan/free-run misbehaves. Same stop_action() as the SCAN Stop button.
# Kept compact (no oversized minimum width) so it never squeezes the header
# readouts or the right-hand panel; a fixed size policy so it doesn't stretch.
btn_estop = QtWidgets.QPushButton("■ STOP")
btn_estop.setObjectName("Danger")
btn_estop.setMinimumHeight(40)
btn_estop.setFixedWidth(96)
btn_estop.setToolTip("Emergency stop: halt the motor immediately")
_header.addWidget(btn_estop)
_root_lay.addLayout(_header)


# ---- Main area: splitter (left plot/console tabs, right scroll panel) ----
_splitter = QtWidgets.QSplitter(Qt.Orientation.Horizontal)
_root_lay.addWidget(_splitter, 1)

# Left side: Scope / Console tabs
_left_tabs = QtWidgets.QTabWidget()
_plot_widget = pg.PlotWidget()
_plot_item = _plot_widget.getPlotItem()
_plot_item.setLabel("bottom", "Wavelength", units="nm")
_plot_item.setLabel("left", "PMT Voltage", units="V")
_plot_item.showGrid(x=True, y=True, alpha=0.15)
# Legend: every scan curve is created with name=<its label>, but without an
# explicit addLegend() those names were never displayed -- so with several
# traces on screen there was no way to tell which colour was which scan.
# (The old tkinter build had this; it was lost in the Qt port.)
# Legend text size comes from the settings file (vrs41_settings.json ->
# legend_font_px). The default matches the axis tick labels (13 px) so the
# legend is as readable as the rest of the plot; a hardcoded small value was
# unreadable on a high-DPI display.
# NOTE: the plot is built BEFORE _saved_settings is loaded further down, so
# this reads the config directly. Going through _saved_settings here raised
# NameError, which the except swallowed -- the setting silently had no effect
# and the legend always came up at the default.
try:
    _legend_px = int(app_config.load().get("legend_font_px") or 13)
except Exception:
    _legend_px = 13
_legend_px = max(6, min(40, _legend_px))
_plot_legend = _plot_item.addLegend(offset=(8, 8),
                                    labelTextSize=f"{_legend_px}px",
                                    verSpacing=-2, colCount=1)
try:
    _plot_legend.setBrush(pg.mkBrush(*C_LEGEND_BG))   # readable on dark bg
    _plot_legend.setPen(pg.mkPen(C_BORDER))
    _plot_legend.setLabelTextColor(C_TEXT)
except Exception:
    pass
# Wrap the scope in a container with a thin top toolbar. The "Clear Plot"
# button lives here now (right on the scope, next to the plot it acts on)
# instead of buried at the bottom of the EXPORT section where nobody found
# it. The button itself is created once _btn() exists (see below) and dropped
# into _scope_toolbar; keeping a reference to the layout lets us do that.
_scope_wrap = QtWidgets.QWidget()
_scope_wrap_lay = QtWidgets.QVBoxLayout(_scope_wrap)
_scope_wrap_lay.setContentsMargins(0, 0, 0, 0)
_scope_wrap_lay.setSpacing(4)
_scope_toolbar = QtWidgets.QHBoxLayout()
_scope_toolbar.setContentsMargins(0, 0, 0, 0)
_scope_toolbar.addStretch(1)  # push toolbar buttons to the right
_scope_wrap_lay.addLayout(_scope_toolbar)
_scope_wrap_lay.addWidget(_plot_widget, 1)
_left_tabs.addTab(_scope_wrap, "Scope")

console = QtWidgets.QPlainTextEdit()
console.setReadOnly(True)
console.setMaximumBlockCount(5000)  # do not let the console grow unbounded
_left_tabs.addTab(console, "Console")
_splitter.addWidget(_left_tabs)

# PlotManager on the pyqtgraph PlotItem
plot_mgr = PlotManager(_plot_item)

# ---- Crosshair (PS-2600A style): teal V/H lines that follow the mouse over
# the plot, with wavelength + intensity shown in the header readouts. Purely
# a measurement/inspection aid -- reads nothing from hardware, changes no
# state. The intensity is looked up from the ACTIVE trace's plotted points
# (nearest x), so it reflects exactly what's on screen. ----
_crosshair_v = pg.InfiniteLine(angle=90, movable=False,
                               pen=pg.mkPen(C_ACCENT, width=1, style=Qt.PenStyle.DashLine))
_crosshair_h = pg.InfiniteLine(angle=0, movable=False,
                               pen=pg.mkPen(C_ACCENT, width=1, style=Qt.PenStyle.DashLine))
_crosshair_v.setVisible(False)
_crosshair_h.setVisible(False)
_plot_item.addItem(_crosshair_v, ignoreBounds=True)
_plot_item.addItem(_crosshair_h, ignoreBounds=True)


def _nearest_active_trace_y(x_nm):
    """Intensity/voltage of the ACTIVE trace at the plotted point nearest to
    x_nm, or None if there's no active trace/points. Reads the PlotManager's
    shadow lists under its own lock (same thread-safe path as get_points),
    so it never touches pyqtgraph objects from here."""
    try:
        pid = plot_mgr._active_id
        if not pid:
            return None
        with plot_mgr._lock:
            rec = plot_mgr._plots.get(pid)
            xs = list(rec.get("x") or []) if rec else []
            ys = list(rec.get("y") or []) if rec else []
        if not xs:
            return None
        # nearest x (lists are typically monotonic in nm, but don't assume it)
        best_i = min(range(len(xs)), key=lambda i: abs(xs[i] - x_nm))
        return ys[best_i]
    except Exception:
        return None


def _on_plot_mouse_moved(evt):
    """Mouse-move over the scope: place the crosshair and update the CURSOR
    readouts. evt is a scene-position (comes from the SignalProxy). Guarded
    so a stray event outside the view can't raise."""
    try:
        pos = evt[0] if isinstance(evt, (tuple, list)) else evt
        vb = _plot_item.getViewBox()
        if not _plot_item.sceneBoundingRect().contains(pos):
            _crosshair_v.setVisible(False)
            _crosshair_h.setVisible(False)
            return
        mp = vb.mapSceneToView(pos)
        x_nm = float(mp.x())
        y_val = _nearest_active_trace_y(x_nm)
        _crosshair_v.setPos(x_nm)
        _crosshair_v.setVisible(True)
        if y_val is not None:
            _crosshair_h.setPos(y_val)
            _crosshair_h.setVisible(True)
            ro_cursor_v.setText(f"{y_val:.4f}")
        else:
            # no trace to read: still show the vertical wavelength line,
            # but follow the mouse y for the horizontal one so the cross
            # is complete, and blank the voltage readout.
            _crosshair_h.setPos(float(mp.y()))
            _crosshair_h.setVisible(True)
            ro_cursor_v.setText("—")
        ro_cursor_nm.setText(f"{x_nm:.3f}")
    except Exception:
        pass


# SignalProxy throttles the high-frequency mouse-move signal (rateLimit Hz)
# so we don't rebuild the readout on every pixel -- standard pyqtgraph
# crosshair pattern.
_crosshair_proxy = pg.SignalProxy(_plot_item.scene().sigMouseMoved,
                                  rateLimit=60, slot=_on_plot_mouse_moved)

# Right side: scroll area with collapsible sections
_scroll = QtWidgets.QScrollArea()
_scroll.setWidgetResizable(True)
# Allow the horizontal scrollbar AS NEEDED instead of forcing it off: when the
# window gets narrow, hiding it meant the panel content (buttons/fields) was
# silently clipped off the right edge with no way to reach it (the "buttons cut
# off / labels chopped" the user saw). AsNeeded keeps it invisible at normal
# width but lets you reach everything when the window is small.
_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
# Lower minimum so the panel can shrink to fit a narrow window instead of
# overflowing it; max keeps it from eating the whole width on a wide window.
_scroll.setMinimumWidth(300)
_scroll.setMaximumWidth(440)
_panel = QtWidgets.QWidget()
# Hard cap the panel content width so no single wide widget (a long label, a
# stretched field) can ever blow the panel past what fits -- this was the root
# cause of the clipped/overflowing controls: one non-wrapping label forced the
# whole panel to ~490px. With widgetResizable the panel now tracks the
# viewport width up to this cap and wraps content instead of overflowing.
_panel.setMaximumWidth(430)
_panel_lay = QtWidgets.QVBoxLayout(_panel)
_panel_lay.setContentsMargins(4, 4, 4, 4)
_panel_lay.setSpacing(2)
_scroll.setWidget(_panel)
_splitter.addWidget(_scroll)
_splitter.setStretchFactor(0, 1)
_splitter.setStretchFactor(1, 0)
# Give the splitter a sane initial split so the right panel always gets its
# ~360px and the plot takes the rest -- without this the splitter can hand the
# plot too much and squeeze the panel off-screen on first show.
_splitter.setSizes([760, 360])
# The plot side may collapse if dragged; the panel side must not (so its
# controls can never be squeezed to nothing).
_splitter.setCollapsible(0, True)
_splitter.setCollapsible(1, False)


# --- helper widget factories ---------------------------------------
def _parse_float_loose(txt):
    """Parse a user-typed number, accepting BOTH decimal separators.

    WHY: the app's own fields (Start/End/Step/...) are plain QLineEdits parsed
    with float(), i.e. DOT only. Qt's QInputDialog.getDouble on the other hand
    is LOCALE-aware, so on a German system it demanded a COMMA -- the user had
    to type "406,2" in the post-connect dialog but "406.2" everywhere else in
    the same app. Rather than forcing one separator on the user, every place
    that reads a wavelength from free text now goes through here and accepts
    either.

    Accepts "406.2", "406,2", " 406.2 ". Thousands separators are NOT handled
    (deliberately: "1,234" is ambiguous and a wavelength never needs it -- it
    is read as 1.234).

    Returns float, or raises ValueError like float() does, so callers keep
    their existing try/except structure."""
    s = str(txt).strip().replace(",", ".")
    return float(s)


def _line_edit(initial=""):
    le = QtWidgets.QLineEdit()
    if initial:
        le.setText(str(initial))
    return le


def _btn(text, obj=None):
    b = QtWidgets.QPushButton(text)
    if obj:
        b.setObjectName(obj)
    return b


# Load persisted settings ONCE, before building any widget, so every field
# below can be pre-filled from it (falls back to the hardcoded default when
# a key was never saved, i.e. is still None -- see app_config.DEFAULT_CONFIG).
_saved_settings = app_config.load()


def _persist(key, value):
    """Write one key=value into the settings JSON. Never raises -- a failed
    save is already logged by app_config.save() itself. Called from the
    editingFinished/changed signal of every field that should survive a
    restart (see app_config.DEFAULT_CONFIG for the full key list)."""
    try:
        app_config.save(**{key: value})
    except Exception:
        pass


# Clear Plot lives on the scope toolbar (created above) rather than in EXPORT
# -- it acts on the plot, so it belongs next to it and is far easier to find
# there. Wired to clear_plot_action further down (section 9). Note the crosshair
# is re-attached automatically after a clear now (see PlotManager.clear_all).
btn_clear_plot = _btn("Clear Plot")
btn_clear_plot.setToolTip("Clear ALL traces from the scope (does not touch saved CSV files)")

# --- Traces menu: show/hide or delete INDIVIDUAL curves -------------------
# Without this menu the only way to get rid of a trace is Clear Plot, which
# removes every trace -- so one bad sweep among several good ones cannot be
# taken out of the picture on its own.
btn_traces = _btn("Traces \u25be")
btn_traces.setToolTip("Show, hide or delete individual traces")
_traces_menu = QtWidgets.QMenu(btn_traces)


def _rebuild_traces_menu():
    """Rebuilt on every open, because the set of traces changes constantly.
    Each entry is checkable (visibility); the submenu also offers Remove."""
    _traces_menu.clear()
    scans = plot_mgr.list_scans()
    if not scans:
        a = _traces_menu.addAction("(no traces)")
        a.setEnabled(False)
        return
    for pid, label, visible in scans:
        sub = _traces_menu.addMenu(label)
        act_vis = sub.addAction("Visible")
        act_vis.setCheckable(True)
        act_vis.setChecked(visible)
        act_vis.triggered.connect(
            lambda checked, p=pid: plot_mgr.set_scan_visible(p, checked))
        act_del = sub.addAction("Remove this trace")
        act_del.triggered.connect(lambda _=False, p=pid: plot_mgr.remove_scan(p))
    _traces_menu.addSeparator()
    act_all = _traces_menu.addAction("Show all")
    act_all.triggered.connect(
        lambda: [plot_mgr.set_scan_visible(p, True) for p, _, _ in plot_mgr.list_scans()])


_traces_menu.aboutToShow.connect(_rebuild_traces_menu)
btn_traces.setMenu(_traces_menu)

# --- PNG export ----------------------------------------------------------
# pyqtgraph can already do this from its right-click menu, but nobody finds it
# there. A button makes it discoverable and lets us default the background to
# WHITE: the on-screen theme is dark, and a dark PNG is rarely what you want
# in a report or a paper.
btn_save_png = _btn("Save PNG")
btn_save_png.setToolTip(
    "Save the plot as a PNG image.\n"
    "The exported image uses a WHITE background (the dark on-screen theme is "
    "for the screen); for full control over size and background use the "
    "right-click menu on the plot -> Export.")


def _save_plot_png():
    """Export the scope to PNG via pyqtgraph's ImageExporter."""
    default = time.strftime("plot_%Y%m%d_%H%M%S.png")
    start_dir = entry_export_dir.text() or os.getcwd()
    path, _ = QtWidgets.QFileDialog.getSaveFileName(
        _window, "Save plot as PNG", os.path.join(start_dir, default),
        "PNG image (*.png)")
    if not path:
        return
    if not path.lower().endswith(".png"):
        path += ".png"
    try:
        import pyqtgraph.exporters as pgexp
        ex = pgexp.ImageExporter(_plot_item)
        ex.parameters()['width'] = 1600          # sensible print resolution
        # Temporarily switch to a light background for the export only.
        old_bg = _plot_widget.backgroundBrush()
        _plot_widget.setBackground('w')
        for ax in ("bottom", "left"):
            _plot_item.getAxis(ax).setPen(pg.mkPen('k'))
            _plot_item.getAxis(ax).setTextPen(pg.mkPen('k'))
        app.processEvents()
        ex.export(path)
        # ...and restore the dark theme.
        _plot_widget.setBackground(old_bg)
        for ax in ("bottom", "left"):
            _plot_item.getAxis(ax).setPen(pg.mkPen(C_BORDER))
            _plot_item.getAxis(ax).setTextPen(pg.mkPen(C_MUTED))
        log(f"[PNG] Saved {path}")
    except Exception as e:
        log(f"[PNG] Export failed: {e}", "error")
        a_messagebox.showerror("Save PNG", f"Could not save the image:\n{e}")


btn_save_png.clicked.connect(_save_plot_png)

for _w in (btn_traces, btn_save_png, btn_clear_plot):
    _scope_toolbar.addWidget(_w)


# =========================== SECTION: CONNECTION ====================
_sec_conn = CollapsibleSection("CONNECTION")
entry_port = _line_edit(_saved_settings.get("port") or DEFAULT_PORT)
entry_baud = _line_edit(str(_saved_settings.get("baud") or DEFAULT_BAUD))
# Single Connect/Disconnect toggle: one button that changes its label and
# colour with the connection state. Disconnected -> "Connect", teal Primary
# style (inviting the connect action). Connected -> "Disconnect", red Danger
# style. The label/style swap happens in _apply_conn_button() (called from
# set_buttons_connected); the click is dispatched in _on_conn_toggle().
btn_conn_toggle = _btn("Connect", "Primary")
_conn_status = QtWidgets.QLabel("● disconnected")
_conn_status.setStyleSheet(f"color:{C_ERROR};")
_sec_conn.add_row("Port", entry_port)
_sec_conn.add_row("Baud", entry_baud)
_sec_conn.add_widget(btn_conn_toggle)
_sec_conn.add_widget(_conn_status)
_panel_lay.addWidget(_sec_conn)

# =========================== SECTION: MOVE =========================
_sec_move = CollapsibleSection("MOVE")
# Restore the last known software-tracked wavelength, if we have one (see
# DEFAULT_CONFIG["last_current_nm"] for the caveat: this is NOT a hardware
# readback). Also seed state['current_nm'] so goto/scan math starts from the
# restored value, not the hardcoded 500.0 fallback. The user-facing caveat
# log line is emitted later, once the console log sink is registered.
_restored_nm = _saved_settings.get("last_current_nm")
if _restored_nm is not None:
    try:
        state["current_nm"] = float(_restored_nm)
    except Exception:
        _restored_nm = None
entry_current = _line_edit(f"{state.get('current_nm', 500.0):.3f}")
entry_goto = _line_edit()
btn_goto = _btn("Go To", "Primary")
entry_jog_step = _line_edit(str(_saved_settings.get("jog_step_nm") or "0.5"))
btn_jog_minus = _btn("Jog −")
btn_jog_plus = _btn("Jog +")
btn_mark_point = _btn("Mark Point")
_sec_move.add_row("Current λ (nm)", entry_current)
_sec_move.add_row("Go To (nm)", entry_goto)
_sec_move.add_widget(btn_goto)
_sec_move.add_row("Jog step (nm)", entry_jog_step)
_sec_move.add_widget(_mk_row2(btn_jog_minus, btn_jog_plus))
_sec_move.add_widget(btn_mark_point)
_panel_lay.addWidget(_sec_move)

# =========================== SECTION: CALIBRATION ==================
# OFFSET calibration only (for now): the system is purely relative (no
# absolute position sensor), so if the reference line sits at the wrong
# wavelength, the ANCHOR is off. This lets the user centre a known line and
# declare "we are at X nm now" -- re-anchoring current_nm without moving the
# motor. A true two-point (scale) calibration is deliberately NOT here yet;
# it needs two measured lines and would change STEPS_PER_NM globally -- see
# BACKLOG / the hint label below.
_sec_cal = CollapsibleSection("CALIBRATION", collapsed=True)
entry_cal_known_nm = _line_edit("")
btn_set_current = _btn("Set current λ to this value", "Primary")
_cal_hint = QtWidgets.QLabel(
    "Offset calibration: drive to a known reference line, centre it, enter its "
    "true wavelength here and apply — this re-anchors the position WITHOUT "
    "moving the motor (steps/nm scale unchanged). If a second known line still "
    "reads wrong afterwards, the error is scale, not offset — that needs a "
    "two-point calibration (not yet implemented).")
_cal_hint.setWordWrap(True)
_cal_hint.setStyleSheet(f"color:{C_MUTED}; font-size:11px;")
_sec_cal.add_row("Known λ here (nm)", entry_cal_known_nm)
_sec_cal.add_widget(btn_set_current)
_sec_cal.add_widget(_cal_hint)
# NOTE: _sec_cal is added to the panel AFTER the BACKLASH section (see below),
# so the panel order is ... Backlash / Calibration / DAQ / Export. The build
# stays here (near MOVE) only because entry_cal_known_nm/btn_set_current are
# referenced by wiring further down; the visual placement is set by the
# addWidget() call, not by where the widgets are constructed.

# =========================== SECTION: SCAN =========================
_sec_scan = CollapsibleSection("SCAN")
entry_start = _line_edit(_saved_settings.get("scan_start_nm") or "")
entry_end = _line_edit(_saved_settings.get("scan_end_nm") or "")
entry_step = _line_edit(_saved_settings.get("scan_step_nm") or "")
# Dwell is entered/displayed in ms here (default "0"); scan_engine/motion/daq
# keep consuming it as SECONDS unchanged -- see _EntryAdapterMsToSec below,
# which is the only place the ms<->s conversion happens.
entry_wait = _line_edit(_saved_settings.get("scan_wait_ms") or "0")
entry_wait.setToolTip(
    "Timebase: the TOTAL time budget for one scan point, in ms.\n"
    "Split as:  pre-settle  ->  averaging  ->  idle remainder.\n"
    "Not a pause AFTER the measurement -- it is the whole per-point budget "
    "that pre-settle and averaging are taken out of.")

# Created here, not down in the AVERAGING section, because the SCAN grid
# below places it directly under Timebase -- widgets must exist before
# addWidget().
entry_presettle = _line_edit(str(_saved_settings.get("presettle_ms") or PRESETTLE_MS_DEFAULT))
entry_presettle.setToolTip(
    "Dead time after arriving at a point, BEFORE any measurement, so "
    "mechanics and signal can settle.\n"
    "Subtracted from the Timebase, and it applies in BOTH averaging modes "
    "(samples and time).")
btn_scan = _btn("Start Scan", "Primary")
btn_free_run = _btn("Free Run Scan (continuous)")
btn_pause = _btn("Pause")
btn_resume = _btn("Resume")
btn_stop = _btn("Stop", "Danger")
btn_swap_start_end = _btn("⇅ Swap Start / End")
# Free Run speed override (SP, rpm). Hard-capped at RAMP_SP (device_
# constants.py -- the rig-confirmed normal speed) in _clamp_free_run_sp()
# below; this field can only slow the sweep down, never speed it up past
# what's actually been verified safe on this rig.
_saved_free_run_sp = _saved_settings.get("free_run_sp_rpm")
try:
    _saved_free_run_sp = max(1, min(int(_saved_free_run_sp), RAMP_SP)) if _saved_free_run_sp else None
except Exception:
    _saved_free_run_sp = None
entry_free_run_sp = _line_edit(str(_saved_free_run_sp if _saved_free_run_sp else RAMP_SP))
# Last VALID Free Run speed, used as the fallback when the field is emptied or
# contains garbage (see _on_free_run_sp_edited). Kept separate from the entry
# adapter on purpose: the adapter's shadow mirrors textChanged, so it is
# already empty by the time editingFinished fires on a cleared field.
_last_valid_free_run_sp = [int(_saved_free_run_sp if _saved_free_run_sp else RAMP_SP)]
_free_run_est = QtWidgets.QLabel("Est. resolution: —")
_free_run_est.setWordWrap(True)
_free_run_est.setStyleSheet(f"color:{C_MUTED}; font-size:11px;")


def _update_free_run_estimate():
    """Live info label: rough estimate of nm-between-samples + point count
    for the currently entered Free Run speed/range. Purely informational,
    never used for validation/clamping (that happens independently in
    _on_free_run_sp_edited / scan_engine.validate_free_run_inputs).
    Numbers come from motion.estimate_free_run_resolution_nm(), which
    interpolates device_constants.FREE_RUN_SP_STEPS_PER_POLL -- real
    probe_move_timing.py measurements, not a guessed formula. See that
    table's docstring for why this is an order-of-magnitude estimate
    (up to ~2x measured run-to-run variance), not a precise prediction."""
    try:
        sp = int(float(entry_free_run_sp.text()))
    except Exception:
        sp = RAMP_SP
    sp_clamped = max(1, min(sp, RAMP_SP))
    try:
        res_nm = estimate_free_run_resolution_nm(sp_clamped)
    except Exception:
        _free_run_est.setText("Est. resolution: —")
        return
    try:
        s = float(entry_start.text()); e = float(entry_end.text())
        span = abs(e - s)
        n_points = int(span / res_nm) if res_nm > 0 else 0
        _free_run_est.setText(
            f"Est. @ {sp_clamped} rpm: ~{res_nm:.4f} nm/point, ~{n_points} pts "
            f"over {span:.3f} nm (rough estimate, ±2x -- probe_move_timing.py)")
    except Exception:
        _free_run_est.setText(
            f"Est. @ {sp_clamped} rpm: ~{res_nm:.4f} nm/point "
            f"(rough estimate, ±2x -- probe_move_timing.py)")


entry_free_run_sp.textChanged.connect(lambda _t: _update_free_run_estimate())
entry_start.textChanged.connect(lambda _t: _update_free_run_estimate())
entry_end.textChanged.connect(lambda _t: _update_free_run_estimate())
_update_free_run_estimate()

# --- Start / End / Step / Wait in one aligned grid, with the Swap button
# tucked into a middle column BETWEEN the labels and the Start/End fields,
# spanning both of those rows -- this saves a row compared to a full-width
# Swap button below the fields, and keeps all four fields on one aligned
# column. Step and Wait sit in the same grid so they line up with Start/End. ---
btn_swap_start_end.setText("⇅")  # compact: full description is on the tooltip
btn_swap_start_end.setToolTip("Swap Start and End so the next scan runs the opposite direction")
_scan_grid_w = QtWidgets.QWidget()
_scan_grid = QtWidgets.QGridLayout(_scan_grid_w)
_scan_grid.setContentsMargins(0, 0, 0, 0)
_scan_grid.setHorizontalSpacing(6)
_scan_grid.setVerticalSpacing(6)
_scan_grid.addWidget(QtWidgets.QLabel("Start (nm)"), 0, 0)
_scan_grid.addWidget(QtWidgets.QLabel("End (nm)"), 1, 0)
_scan_grid.addWidget(QtWidgets.QLabel("Step (nm)"), 2, 0)
_scan_grid.addWidget(QtWidgets.QLabel("Timebase (ms)"), 3, 0)
_scan_grid.addWidget(btn_swap_start_end, 0, 1, 2, 1)  # col 1, rows 0-1 (Start+End)
_scan_grid.addWidget(entry_start, 0, 2)
_scan_grid.addWidget(entry_end, 1, 2)
_scan_grid.addWidget(entry_step, 2, 2)
_scan_grid.addWidget(entry_wait, 3, 2)
# Pre-settle sits directly under Timebase because it is SUBTRACTED from it:
# it is dead time after arriving at a point, before anything is measured, and
# it applies in BOTH averaging modes. Keeping it in the collapsed AVERAGING
# section made it look like an averaging-only option and hid the fact that it
# eats into the timebase.
_scan_grid.addWidget(QtWidgets.QLabel("Pre-settle (ms)"), 4, 0)
_scan_grid.addWidget(entry_presettle, 4, 2)
_scan_grid.setColumnStretch(0, 0)
_scan_grid.setColumnStretch(1, 0)
_scan_grid.setColumnStretch(2, 1)  # fields take the remaining width
_sec_scan.add_widget(_scan_grid_w)

_sec_scan.add_widget(btn_scan)       # Start Scan
_sec_scan.add_widget(btn_free_run)   # Free Run Scan -- directly under Start Scan
# The long Free Run explanation lives in the button's tooltip rather than an
# always-visible label, which would not fit the narrow panel.
btn_free_run.setToolTip(
    f"Free Run: ONE continuous move Start→End, raw POS+DAQ polling "
    f"(~20 Hz measured), no averaging, no Pause. Step/Wait not used. "
    f"Speed capped at {RAMP_SP} rpm (device_constants.RAMP_SP, the "
    f"rig-confirmed normal speed) -- can only be set slower, not faster.")
# Free Run speed field + live estimate go directly under the Free Run button.
_sec_scan.add_row("Free Run speed (SP, rpm)", entry_free_run_sp)
_sec_scan.add_widget(_free_run_est)
_sec_scan.add_widget(_mk_row2(btn_pause, btn_resume))
_sec_scan.add_widget(btn_stop)
_panel_lay.addWidget(_sec_scan)

# =========================== SECTION: AVERAGING ====================
_sec_avg = CollapsibleSection("AVERAGING", collapsed=True)
entry_avg_samples = _line_edit(str(_saved_settings.get("avg_samples") or AVG_SAMPLES_DEFAULT))
_avg_mode_combo = QtWidgets.QComboBox()
_avg_mode_combo.addItems(["samples", "time"])
_avg_mode_combo.setCurrentText(str(_saved_settings.get("avg_mode") or AVG_MODE_DEFAULT))
entry_avg_fraction = _line_edit(str(_saved_settings.get("avg_fraction_pct") or AVG_FRACTION_PCT_DEFAULT))
_sec_avg.add_row("Avg samples (N)", entry_avg_samples)
_sec_avg.add_row("Avg mode", _avg_mode_combo)
_sec_avg.add_row("Avg fraction (%)", entry_avg_fraction)
# Live explanation of what the percentage currently MEANS in milliseconds.
# Without it the field is abstract: "50 %" of what, and how much averaging is
# that actually? Recomputed from the real Timebase/Pre-settle fields, and
# after a scan point has been measured it also reports how many DAQ reads
# fitted into the window and the resulting rate (from daq.acquire_measurement's
# telemetry) instead of a guessed number.
_avg_hint = QtWidgets.QLabel("")
_avg_hint.setWordWrap(True)
_avg_hint.setStyleSheet(f"color:{C_MUTED}; font-size:11px;")
_sec_avg.add_widget(_avg_hint)
# Pre-settle's own row lives in the SCAN section (directly under Timebase),
# not here: it is subtracted from the timebase and applies to both averaging
# modes, so it belongs next to the budget it consumes, not inside a
# collapsed AVERAGING panel.
_panel_lay.addWidget(_sec_avg)

# =========================== SECTION: QUEUE ========================
_sec_queue = CollapsibleSection("SCAN QUEUE", collapsed=True)
queue_listbox = QtWidgets.QListWidget()
queue_listbox.setMaximumHeight(120)
btn_add_queue = _btn("Add from fields")
btn_remove_queue = _btn("Remove selected")
btn_run_queue = _btn("Run queue")
btn_clear_queue = _btn("Clear queue")
# Overwrites the selected entry with the current field values -- each queue
# entry carries its own acquisition settings now, so without this you would
# have to delete and re-add an entry just to change its averaging.
btn_update_queue = _btn("Update selected")
btn_update_queue.setToolTip(
    "Overwrite the selected queue entry with the values currently in the "
    "Scan and Averaging fields (wavelength range, timebase, pre-settle and "
    "averaging mode/samples/fraction).")
queue_listbox.setMaximumHeight(150)
_sec_queue.add_widget(queue_listbox)
_sec_queue.add_widget(_mk_row2(btn_add_queue, btn_update_queue))
_sec_queue.add_widget(_mk_row2(btn_remove_queue, btn_clear_queue))
_sec_queue.add_widget(btn_run_queue)
_panel_lay.addWidget(_sec_queue)

# =========================== SECTION: BACKLASH =====================
# Workflow: once per session click "Reference Run" (after Connect, before the
# first goto/scan) -> known direction; from then on every direction reversal gets
# the nm value below added. The nm VALUE persists (settings JSON); the direction
# state does NOT -> Reference Run every session.
#
# TERMINOLOGY: "backlash" and "slip" were used interchangeably for the SAME
# quantity -- the mechanical play in the drive train. The GUI showed both
# ("Slip / Backlash (nm)" as the field, "Slip: ..." in one status label,
# "Backlash: ..." in another), which read as two different values. All
# user-facing text now says BACKLASH, the precise engineering term and the one
# device_constants.SIM_BACKLASH_NM already used.
# Deliberately NOT renamed: the internal identifiers (slip_nm_var,
# _get_slip_nm, backlash_slip_nm in the settings file) and the [SLIP] log tag.
# The identifiers would be a wide, behaviour-neutral rename for no user
# benefit; the log tag is referenced as a grep target by MANUAL.md, by the T5
# test in CONTEXT.md and by rig logs already recorded, so renaming it would
# invalidate existing instructions and captures.
# The real distinction worth knowing is NOT slip-vs-backlash but: the value in
# this field is the CONFIGURED estimate, while the instrument has a TRUE
# mechanical play. Their difference is the calibration error -- measurable, and
# exactly what backlash_cal.py (the "Calibrate…" button) exists to reduce.
_sec_bl = CollapsibleSection("BACKLASH")
entry_slip = _line_edit(str(_saved_settings.get("backlash_slip_nm", 0.082)))
btn_reference_run = _btn("Reference Run")
btn_calibrate = _btn("Calibrate…", "Primary")
lbl_direction_state = QtWidgets.QLabel("Direction: unknown – run Reference Run first")
lbl_direction_state.setStyleSheet(f"color:{C_MUTED};")
_sec_bl.add_row("Backlash (nm)", entry_slip)
_sec_bl.add_widget(btn_reference_run)
_sec_bl.add_widget(btn_calibrate)
_sec_bl.add_widget(lbl_direction_state)
_panel_lay.addWidget(_sec_bl)

# CALIBRATION goes here in the panel (after BACKLASH, before DAQ) per the
# requested order. The section itself was built up near MOVE (see above).
_panel_lay.addWidget(_sec_cal)

# =========================== SECTION: DAQ ==========================
_sec_daq = CollapsibleSection("DAQ", collapsed=True)
entry_daq_dev = _line_edit(_saved_settings.get("daq_dev") or state.get("daq_dev", "Dev1"))
_daq_ai_combo = QtWidgets.QComboBox()
_daq_ai_combo.addItems([f"ai{i}" for i in range(0, 11)])
_daq_ai_combo.setCurrentText(_saved_settings.get("daq_ai") or state.get("daq_ai", "ai1"))
_sec_daq.add_row("Device", entry_daq_dev)
_sec_daq.add_row("Analog input", _daq_ai_combo)
_panel_lay.addWidget(_sec_daq)

# =========================== SECTION: EXPORT =======================
_sec_exp = CollapsibleSection("EXPORT", collapsed=True)
_chk_autosave = QtWidgets.QCheckBox("Auto-save CSV after scan")
_chk_autosave.setChecked(bool(_saved_settings.get("autosave_csv", False)))
entry_export_dir = _line_edit(_saved_settings.get("export_dir") or os.path.expanduser("~/scans"))
btn_browse = _btn("Browse…")
entry_pattern = _line_edit(_saved_settings.get("export_pattern") or "scan_{date}_{time}_{start}-{end}nm_{mode}.csv")
btn_save_now = _btn("Save CSV now")
# btn_clear_plot lives on the scope toolbar (created just after the _btn()
# factory, above), not here in EXPORT -- it acts on the plot, so it belongs
# next to it rather than buried at the bottom of a collapsed section.
_sec_exp.add_widget(_chk_autosave)
_sec_exp.add_row("Folder", entry_export_dir)
_sec_exp.add_widget(btn_browse)
_sec_exp.add_row("Pattern", entry_pattern)
_sec_exp.add_widget(btn_save_now)
_panel_lay.addWidget(_sec_exp)

_panel_lay.addStretch(1)


# ---- Status-Bar ----
_status = _window.statusBar()
_lbl_fsm = QtWidgets.QLabel("State: IDLE")
_lbl_conn = QtWidgets.QLabel("Status: disconnected")
_lbl_slip = QtWidgets.QLabel("Backlash: —")
# Drive housing temperature (TEM). Refreshed slowly and ONLY while idle -- see
# _refresh_temperature(). Tooltip spells out what the number is, because
# "housing" is not "winding": the drive derives coil/MOSFET temperature from
# this reading plus its current measurement via an internal thermal model, and
# that model (not this value) trips the overtemperature cut-out.
_lbl_temp = QtWidgets.QLabel("Temp: —")
_lbl_temp.setToolTip(
    "Drive housing temperature (TEM). This unit is an integrated motor+"
    "controller (GTYP 'CS-BX4'), so it tracks the motor housing -- but the "
    "winding runs hotter; the drive models coil temperature internally for "
    "its overtemperature cut-out. Polled every 15 s while idle only, never "
    "during a move or scan.")
# ---- Live analog-input monitor (AI) ----
# Always-on readout of the DAQ input so the PMT/HV can be adjusted WITHOUT
# starting a scan. Text value + colour-coded bar + max-hold.
# Thresholds and full scale come from device_constants (AI_WARN_V/AI_ALARM_V/
# AI_BAR_MAX_V) so they can be changed without touching GUI code.
_lbl_ai = QtWidgets.QLabel("AI: —")   # prefix set by _apply_ai_value
_lbl_ai.setMinimumWidth(150)
_ai_bar = QtWidgets.QProgressBar()
_ai_bar.setRange(0, 1000)          # 0.1 % resolution; value scaled below
_ai_bar.setValue(0)
_ai_bar.setTextVisible(False)
_ai_bar.setFixedWidth(120)
_ai_bar.setFixedHeight(12)
_lbl_ai_max = QtWidgets.QLabel("max —")
_btn_ai_reset = QtWidgets.QPushButton("Reset max")
_btn_ai_reset.setFixedHeight(20)
_ai_max_v = [None]        # max-hold, reset by the button
_ai_tip = (f"Live analog input ({{chan}}). Green < {AI_WARN_V:g} V, "
           f"yellow {AI_WARN_V:g}-{AI_ALARM_V:g} V, red >= {AI_ALARM_V:g} V; "
           f"bar full scale {AI_BAR_MAX_V:g} V "
           f"(device_constants.AI_WARN_V / AI_ALARM_V / AI_BAR_MAX_V).\n"
           f"Polled at {AI_MONITOR_HZ:g} Hz while IDLE only. During a scan it "
           f"shows the value the scan itself just measured -- opening a second "
           f"NI-DAQ task on the same channel would fail.")
for _w in (_lbl_ai, _ai_bar, _lbl_ai_max):
    _w.setToolTip(_ai_tip.replace("{chan}", "DAQ AI"))


def _ai_colour(v):
    """Bar/text colour for a voltage, per the configured thresholds."""
    if v is None:
        return C_MUTED
    if v >= AI_ALARM_V:
        return C_ERROR
    if v >= AI_WARN_V:
        return C_AMBER
    return C_OK


# Prefix for the AI readout. With no NI driver present read_pmt_voltage()
# silently returns SIMULATED data that looks like a real spectrum, so the
# readout has to say so -- otherwise a bench session with no hardware, or a
# rig session where the driver failed to load, looks exactly like a valid
# measurement. See daq.daq_source_label().
_AI_PREFIX = "AI" if DAQ_AVAILABLE else "AI (SIM)"


def _apply_ai_value(v):
    """Update the AI readout widgets (GUI thread). v is volts or None."""
    if v is None:
        _lbl_ai.setText(f"{_AI_PREFIX}: —")
        _ai_bar.setValue(0)
        return
    if _ai_max_v[0] is None or v > _ai_max_v[0]:
        _ai_max_v[0] = v
    col = _ai_colour(v)
    _lbl_ai.setText(f"{_AI_PREFIX}: <b><span style='color:{col}'>{v:.4f} V</span></b>")
    frac = 0.0 if AI_BAR_MAX_V <= 0 else max(0.0, min(1.0, v / float(AI_BAR_MAX_V)))
    _ai_bar.setValue(int(frac * 1000))
    _ai_bar.setStyleSheet(
        f"QProgressBar{{background:{C_BG};border:1px solid {C_BORDER};"
        f"border-radius:3px;}}"
        f"QProgressBar::chunk{{background:{col};border-radius:2px;}}")
    _lbl_ai_max.setText(f"max {_ai_max_v[0]:.4f} V")


def _reset_ai_max():
    _ai_max_v[0] = None
    _lbl_ai_max.setText("max —")


_btn_ai_reset.clicked.connect(_reset_ai_max)


def _refresh_ai():
    """Refresh the live AI readout.

    TWO PATHS, and the distinction matters for the hardware:
      * scan/move running -> do NOT touch the DAQ. Display
        state['last_pmt_v'], which the scan's own read just wrote
        (daq._note_last_pmt). Opening a second NI-DAQ task on a channel that
        the scan thread has reserved fails with a resource error, so the
        monitor must never compete for it.
      * idle -> actively read, in a worker thread (an NI-DAQ read creates and
        tears down a Task and is far too slow for the GUI thread).
    """
    if not AI_MONITOR_ENABLED:
        return
    if state.get("is_scanning") or state.get("fsm", "IDLE") != "IDLE":
        v = state.get("last_pmt_v")
        _apply_ai_value(None if v is None else float(v))
        return

    def worker():
        try:
            v = float(read_pmt_voltage())
        except Exception:
            v = None
        _invoker.post(lambda: _apply_ai_value(v))
    threading.Thread(target=worker, daemon=True).start()


if not DAQ_AVAILABLE:
    log("[DAQ] nidaqmx driver not available -- PMT voltages are SIMULATED, "
        "not measured. Scans and CSV exports will be marked accordingly.", "warn")
else:
    log("[DAQ] nidaqmx driver present -- real measurements.")

_ai_timer = QTimer()
_ai_timer.timeout.connect(_refresh_ai)
_ai_timer.start(int(1000.0 / max(0.5, AI_MONITOR_HZ)))

# ---- Status-bar assembly ----
# Built as GROUPS, not as one flat list with a separator after every widget.
# The flat version put a "|" after the progress bar and after the button too,
# left a stray empty cell, and let the button keep its default (oversized)
# height, so the footer looked ragged and the last item was clipped.
def _sep():
    """Thin vertical rule between status-bar groups."""
    f = QtWidgets.QFrame()
    f.setFrameShape(QtWidgets.QFrame.Shape.VLine)
    f.setFixedHeight(14)
    f.setStyleSheet(f"color:{C_BORDER};")
    return f


_STATUS_LBL_QSS = (f"color:{C_MUTED}; font-size:11px;")
_STATUS_VAL_QSS = (f"color:{C_TEXT}; font-size:11px;")

for _w in (_lbl_fsm, _lbl_conn, _lbl_slip, _lbl_temp):
    _w.setStyleSheet(_STATUS_VAL_QSS)

# AI group: label + bar + max + reset, laid out tightly in one container so
# the status bar treats it as a single item and spacing stays even.
_lbl_ai.setStyleSheet(_STATUS_VAL_QSS)
_lbl_ai.setMinimumWidth(0)
_lbl_ai_max.setStyleSheet(_STATUS_LBL_QSS)
_ai_bar.setFixedSize(90, 10)
_btn_ai_reset.setStyleSheet(
    f"QPushButton{{background:transparent;border:1px solid {C_BORDER};"
    f"border-radius:3px;color:{C_MUTED};font-size:10px;padding:1px 6px;}}"
    f"QPushButton:hover{{color:{C_TEXT};border-color:{C_MUTED};}}")
_btn_ai_reset.setFixedHeight(18)
_btn_ai_reset.setCursor(QtCore.Qt.CursorShape.PointingHandCursor)
_btn_ai_reset.setToolTip("Reset the max-hold value")

# Same treatment for the status bar. The AI value refreshes at 4 Hz and is
# the worst offender; Slip/Temp/max change rarely but shift their neighbours
# when they do. The AI sample allows for a sign and the bold rendering used
# for the value.
# Sample must cover TWO integer digits: the channel range goes to 10 V, so
# "10.0000 V" occurs in normal use, and a negative offset reading adds a sign.
# A one-digit sample let the label grow by 4 px as soon as the value went
# above 9.9999 V, which is exactly the jitter this is meant to prevent.
_apply_tabular_numbers(_lbl_ai,     "AI (SIM): -88.8888 V", 6)
_apply_tabular_numbers(_lbl_ai_max, "max -88.8888 V", 4)
_apply_tabular_numbers(_lbl_slip,   "Backlash: 8.888 nm", 4)
_apply_tabular_numbers(_lbl_temp,   "Temp: 888 °C", 4)

_ai_group = QtWidgets.QWidget()
_ai_lay = QtWidgets.QHBoxLayout(_ai_group)
_ai_lay.setContentsMargins(0, 0, 0, 0)
_ai_lay.setSpacing(6)
for _w in (_lbl_ai, _ai_bar, _lbl_ai_max, _btn_ai_reset):
    _ai_lay.addWidget(_w)

_status.setStyleSheet(
    f"QStatusBar{{background:{C_BG};border-top:1px solid {C_BORDER};}}"
    f"QStatusBar::item{{border:none;}}")   # kill Qt's default per-item frames
_status.setSizeGripEnabled(False)

for _i, _w in enumerate((_lbl_fsm, _lbl_conn, _lbl_slip, _lbl_temp, _ai_group)):
    if _i:
        _status.addWidget(_sep())
    _status.addWidget(_w)
_status.addWidget(QtWidgets.QWidget(), 1)   # trailing stretch, no dangling rule


# =====================================================================
# LOG-SINK
# =====================================================================
def _console_log_sink(msg, tag="info"):
    """Called from app_context.log() (often from worker threads): write one line
    to the console, colored by tag. Marshalled onto the GUI thread. Timeout spam
    is suppressed (as in the Tk version)."""
    try:
        if SUPPRESS_TIMEOUT_MESSAGES and (("[TIMEOUT]" in str(msg)) or ("timeout" in str(msg).lower())):
            return
    except Exception:
        pass
    color = {"warn": C_WARN, "error": C_ERROR}.get(tag, C_TEXT)

    def do():
        console.appendHtml(f'<span style="color:{color};">{_html_escape(str(msg))}</span>')
    _invoker.post(do)


def _html_escape(s):
    """Minimal HTML escaping for the console lines (appendHtml)."""
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


# =====================================================================
# BUTTON-STATE HOOKS (identical logic to the Tk version)
# =====================================================================
def _apply_conn_button(connected: bool):
    """Set the single Connect/Disconnect toggle's label + colour for the given
    state. Connected -> "Disconnect", red Danger style; disconnected ->
    "Connect", teal Primary style. Re-polishes the widget so the objectName
    style-swap actually takes effect under the global stylesheet. Marshalled
    onto the GUI thread (may be called from set_buttons_connected via a worker
    thread). Purely cosmetic -- the click dispatch in _on_conn_toggle() reads
    state['connected'], not the label."""
    def do():
        btn_conn_toggle.setText("Disconnect" if connected else "Connect")
        btn_conn_toggle.setObjectName("Danger" if connected else "Primary")
        # Force Qt to re-evaluate the stylesheet for the new objectName.
        btn_conn_toggle.style().unpolish(btn_conn_toggle)
        btn_conn_toggle.style().polish(btn_conn_toggle)
        btn_conn_toggle.update()
    _invoker.post(do)


def _on_conn_toggle():
    """Click handler for the single Connect/Disconnect toggle (GUI thread).
    Dispatches to on_disconnect() when currently connected, on_connect()
    otherwise. Uses the authoritative state flag, not the button label."""
    if state.get("connected"):
        on_disconnect()
    else:
        on_connect()


def set_buttons_connected(connected: bool):
    """Enable/disable buttons depending on connection state. WHERE/WHEN: called
    from on_connect/on_disconnect and via ui_hook from scan_engine (also from
    worker threads -> the _ButtonAdapter marshals internally)."""
    # Single toggle: always clickable while idle (you can either connect or
    # disconnect), its label/colour reflect the state via _apply_conn_button.
    a_btn_conn.configure(state="normal")
    _apply_conn_button(connected)
    a_btn_goto.configure(state=("normal" if connected else "disabled"))
    a_btn_scan.configure(state=("normal" if connected else "disabled"))
    a_btn_free_run.configure(state=("normal" if connected else "disabled"))
    a_btn_pause.configure(state="disabled")
    a_btn_resume.configure(state="disabled")
    a_btn_stop.configure(state=("normal" if connected else "disabled"))
    a_btn_estop.configure(state=("normal" if connected else "disabled"))
    for a in (a_btn_jog_minus, a_btn_jog_plus, a_btn_mark_point,
              a_btn_add_queue, a_btn_remove_queue, a_btn_run_queue, a_btn_clear_queue,
              a_btn_update_queue):
        a.configure(state=("normal" if connected else "disabled"))
    # live status indicator
    def do():
        _conn_status.setText("● connected" if connected else "● disconnected")
        _conn_status.setStyleSheet(f"color:{C_OK if connected else C_ERROR};")
        _lbl_conn.setText("Status: connected" if connected else "Status: disconnected")
    _invoker.post(do)


def set_buttons_scanning(scanning: bool):
    """Disable conflicting controls during a scan, toggle pause/resume.
    WHEN: via ui_hook from scan_engine (worker thread)."""
    if scanning:
        a_btn_goto.configure(state="disabled")
        a_btn_conn.configure(state="disabled")  # can't disconnect mid-scan
        a_btn_scan.configure(state="disabled")
        a_btn_free_run.configure(state="disabled")
        a_btn_pause.configure(state="normal")
        a_btn_resume.configure(state="disabled")
        a_btn_stop.configure(state="normal")
        a_btn_estop.configure(state="normal")
        for a in (a_btn_jog_minus, a_btn_jog_plus, a_btn_mark_point, a_btn_run_queue):
            a.configure(state="disabled")
    else:
        set_buttons_connected(state["connected"])


# =====================================================================
# CONNECT / DISCONNECT / CLOSE
# =====================================================================
def on_connect():
    """Open the serial connection and initialize the motor. WHEN: click on
    'Connect' (GUI thread)."""
    port = a_port.get().strip()
    try:
        baud = int(a_baud.get())
    except Exception:
        baud = DEFAULT_BAUD
    # SIM mode: port "SIM" -> inject the virtual FAULHABER (no motor needed).
    # The DAQ is simulated anyway (no NI driver). This lets the whole flow
    # (goto/scan/plot/queue/backlash/CSV) be tested purely in software.
    if port.upper() == "SIM":
        try:
            from sim_hardware import FakeFaulhaber
            state["ser"] = FakeFaulhaber()
            state["connected"] = True
            set_buttons_connected(True)
            set_fsm("IDLE")
            log("[SIM] Virtual FAULHABER connected — no hardware. DAQ uses simulator.")
            init_motor()
            log("[OK] Motor initialized (EN, %sV0, ANSW, ramp)." % ("HP0, " if SEND_LEGACY_HP0 else ""))
        except Exception as e:
            log(f"[ERROR] SIM connect failed: {e}", "error")
            state["ser"] = None
            state["connected"] = False
            set_buttons_connected(False)
            set_fsm("IDLE")
        return
    try:
        ser = serial.Serial(port, baud, timeout=0.8)
        state["ser"] = ser
        state["connected"] = True
        set_buttons_connected(True)
        set_fsm("IDLE")
        log(f"[OK] Connected {port} @ {baud} baud")
        drain_stale_serial(ser)
        init_motor()
        log("[OK] Motor initialized (EN, %sV0, ANSW, ramp)." % ("HP0, " if SEND_LEGACY_HP0 else ""))
        _prompt_reference_run_then_lambda()
    except Exception as e:
        log(f"[ERROR] Could not open {port}: {e}", "error")
        state["ser"] = None
        state["connected"] = False
        set_buttons_connected(False)
        set_fsm("IDLE")


def _prompt_current_lambda():
    """Ask the operator to confirm/correct the wavelength currently shown on
    the monochromator's OWN (mechanical) readout, and re-anchor
    state['current_nm'] to it via motion.set_current_wavelength() -- the same
    offset-calibration setter as the CALIBRATION section's 'Set current λ to
    this value'. Catches drift from a previous session or a manual nudge of
    the monochromator while it was disconnected. Cancel = leave untouched."""
    try:
        current_default = float(a_current.get())
    except Exception:
        current_default = float(state.get('current_nm', 500.0))
    # getText + _parse_float_loose instead of QInputDialog.getDouble: getDouble
    # is LOCALE-bound, so on a German system it accepted only a COMMA while
    # every other field in this app is parsed with float() and needs a DOT --
    # the user had to type "406,2" here and "406.2" everywhere else. This path
    # accepts both and re-prompts instead of silently doing nothing on a typo.
    while True:
        txt, ok = QtWidgets.QInputDialog.getText(
            _window, "Confirm current wavelength",
            "What wavelength (nm) does the monochromator's own mechanical readout "
            "show right now?\n\nOK re-anchors the tracked position to this value "
            "(no motor movement). Cancel leaves it as-is.",
            text=f"{current_default:.3f}")
        if not ok:
            return
        try:
            val = _parse_float_loose(txt)
        except Exception:
            a_messagebox.showwarning(
                "Confirm current wavelength",
                "Please enter a number, e.g. 406.2 (a comma also works).")
            continue
        set_current_wavelength(val)
        return


def _prompt_reference_run_then_lambda():
    """Post-connect workflow (real hardware only -- SIM has no physical dial
    to confirm against, and this would block the many headless SIM tests):
    ask whether to run the Reference Run now (recommended once per session,
    before the first Goto/Scan -- seats the backlash direction), then --
    whether or not it was run -- ask the operator to confirm the current
    wavelength. Reference Run itself runs in a background thread; the λ
    prompt is chained via its on_done callback so it only appears AFTER the
    move actually finishes, not immediately after the Yes click."""
    if a_messagebox.askyesno(
        "Reference Run",
        "Run Reference Run now?\n\nSeats the backlash direction -- recommended "
        "once per session, before the first Goto/Scan."):
        reference_run_action(on_done=lambda reached: _prompt_current_lambda())
    else:
        _prompt_current_lambda()


def on_disconnect():
    """Close the connection and make the motor safe. WHEN: click on
    'Disconnect' (GUI thread)."""
    try:
        if ser_ok():
            safe_stop()
            state["ser"].close()
        state["connected"] = False
        set_buttons_connected(False)
        set_fsm("IDLE")
        log("[OK] Disconnected and motor set safe.")
    except Exception as e:
        log(f"[WARN] Disconnect issue: {e}", "warn")


def on_close():
    """Clean shutdown on window close: stop the scan, make the motor safe,
    close the port."""
    try:
        state["stop_flag"] = True
        if ser_ok():
            safe_stop()
            state["ser"].close()
    except Exception:
        print("[WARN] on_close: suppressed exception during cleanup")


# =====================================================================
# SETTINGS PERSISTENCE + DAQ/EXPORT COUPLING
# =====================================================================
def _on_slip_edited():
    """Persist the slip value immediately once the field is confirmed/loses
    focus -> survives a restart. WHEN: editingFinished of the field."""
    try:
        val = max(0.0, float(a_slip.get()))
        app_config.save(backlash_slip_nm=val)
        log(f"[SETTINGS] Backlash saved: {val:.3f} nm")
        _invoker.post(lambda: _lbl_slip.setText(f"Backlash: {val:.3f} nm"))
    except Exception:
        pass  # invalid entry (e.g. mid-typing) -> do not save


def _on_swap_start_end():
    """Swap the Start/End fields so the very next scan runs in the opposite
    direction without retyping the range. WHEN: click on "Swap Start / End".
    Deliberately a separate button rather than merging with Start Scan/Stop
    (those stay dedicated: Stop is a safety action, not something to
    overload with a second meaning)."""
    s, e = entry_start.text(), entry_end.text()
    entry_start.setText(e)
    entry_end.setText(s)
    _persist("scan_start_nm", entry_start.text())
    _persist("scan_end_nm", entry_end.text())


def _browse_export_dir():
    """Folder-picker dialog for CSV export."""
    d = QtWidgets.QFileDialog.getExistingDirectory(_window, "Export folder")
    if d:
        a_export_dir.set(d)


# =====================================================================
# WIRING: Qt widgets -> adapters -> app_context (refs/vars/hooks)
# =====================================================================
# 1) Register the console as the log sink (before the initial state calls below).
set_log_sink(_console_log_sink)

# 2) Entry adapters (Tk Entry API on QLineEdit).
a_current   = _EntryAdapter(entry_current, f"{state.get('current_nm', 500.0):.3f}")
a_goto      = _EntryAdapter(entry_goto)
a_start     = _EntryAdapter(entry_start)
a_end       = _EntryAdapter(entry_end)
a_step      = _EntryAdapter(entry_step)
a_wait      = _EntryAdapterMsToSec(entry_wait)
# NO explicit `initial` for any field that was ALREADY built from the saved
# settings above. _EntryAdapter overwrites the widget with whatever initial it
# is handed, so passing a hardcoded default here silently threw the loaded
# value away -- the settings file said avg_samples=16 / avg_fraction_pct=42
# while the fields came up showing the constants 8 and 100, every session.
# With no initial the adapter adopts the widget's current text, which is the
# value loaded from the JSON.
a_port      = _EntryAdapter(entry_port)
a_baud      = _EntryAdapter(entry_baud)
a_jog_step  = _EntryAdapter(entry_jog_step)
a_free_run_sp = _EntryAdapter(entry_free_run_sp)

# 3) Var adapters (StringVar-like) for ui_get + internal use.
a_avg_samples = _EntryAdapter(entry_avg_samples)
a_avg_fraction = _EntryAdapter(entry_avg_fraction)
a_presettle   = _EntryAdapter(entry_presettle)
a_slip        = _EntryAdapter(entry_slip)

a_avg_mode = _VarAdapter(str(AVG_MODE_DEFAULT), _avg_mode_combo.setCurrentText)
_avg_mode_combo.currentTextChanged.connect(a_avg_mode.update)

a_autosave = _VarAdapter("0")
_chk_autosave.stateChanged.connect(
    lambda s: a_autosave.update("1" if s else "0"))

a_export_dir = _VarAdapter(os.path.expanduser("~/scans"), entry_export_dir.setText)
entry_export_dir.textChanged.connect(a_export_dir.update)

a_pattern = _VarAdapter("scan_{date}_{time}_{start}-{end}nm_{mode}.csv",
                        entry_pattern.setText)
entry_pattern.textChanged.connect(a_pattern.update)

# DAQ dev/ai -> mirror into state (motion/daq read state['daq_dev'/'daq_ai']).
def _sync_daq_dev(txt):
    state["daq_dev"] = txt
entry_daq_dev.textChanged.connect(_sync_daq_dev)
state["daq_dev"] = entry_daq_dev.text()

def _sync_daq_ai(txt):
    state["daq_ai"] = txt
_daq_ai_combo.currentTextChanged.connect(_sync_daq_ai)
state["daq_ai"] = _daq_ai_combo.currentText()

# 4) Button adapters (configure(state=...) onto setEnabled).
# Single Connect/Disconnect toggle button -> one adapter. The old
# a_btn_connect / a_btn_disconnect names are kept as ALIASES so existing
# references (set_buttons_*, refs['btn_disconnect'], scan_engine's goto lock)
# keep working -- they all now enable/disable the one toggle.
a_btn_conn        = _ButtonAdapter(btn_conn_toggle)
a_btn_connect     = a_btn_conn
a_btn_disconnect  = a_btn_conn
a_btn_goto        = _ButtonAdapter(btn_goto)
a_btn_scan        = _ButtonAdapter(btn_scan)
a_btn_free_run    = _ButtonAdapter(btn_free_run)
a_btn_pause       = _ButtonAdapter(btn_pause)
a_btn_resume      = _ButtonAdapter(btn_resume)
a_btn_stop        = _ButtonAdapter(btn_stop)
a_btn_estop       = _ButtonAdapter(btn_estop)
a_btn_jog_minus   = _ButtonAdapter(btn_jog_minus)
a_btn_jog_plus    = _ButtonAdapter(btn_jog_plus)
a_btn_mark_point  = _ButtonAdapter(btn_mark_point)
a_btn_add_queue   = _ButtonAdapter(btn_add_queue)
a_btn_update_queue = _ButtonAdapter(btn_update_queue)
a_btn_remove_queue = _ButtonAdapter(btn_remove_queue)
a_btn_run_queue   = _ButtonAdapter(btn_run_queue)
a_btn_clear_queue = _ButtonAdapter(btn_clear_queue)

# 5) Listbox + messagebox adapters.
a_queue = _ListboxAdapter(queue_listbox)
a_messagebox = _MessageboxShim()

# 6) Expose StringVars for motion/daq (ui_get).
for _n, _v in {
    "avg_mode_var": a_avg_mode, "avg_samples_var": a_avg_samples,
    "avg_fraction_var": a_avg_fraction, "presettle_ms_var": a_presettle,
    "slip_nm_var": a_slip, "entry_wait": a_wait,
}.items():
    register_var(_n, _v)

# 7) Expose widget objects for scan_engine (refs[...]).
refs.update({
    "entry_current": a_current, "entry_goto": a_goto,
    "entry_start": a_start, "entry_end": a_end,
    "entry_step": a_step, "entry_wait": a_wait,
    # Raw (millisecond) view of the timebase field. refs['entry_wait'] is a
    # ms->seconds adapter; the queue needs the value as displayed.
    "entry_wait_raw": _EntryAdapter(entry_wait),
    "entry_presettle": a_presettle, "avg_fraction_var": a_avg_fraction,
    "avg_mode_var": a_avg_mode, "avg_samples_var": a_avg_samples,
    "jog_step_var": a_jog_step, "autosave_csv_var": a_autosave,
    "export_dir_var": a_export_dir, "pattern_var": a_pattern,
    "slip_nm_var": a_slip, "queue_listbox": a_queue,
    "free_run_sp_var": a_free_run_sp,
    "btn_add_queue": a_btn_add_queue, "btn_clear_queue": a_btn_clear_queue,
    "btn_disconnect": a_btn_disconnect, "btn_goto": a_btn_goto,
    "btn_pause": a_btn_pause, "btn_remove_queue": a_btn_remove_queue,
    "btn_resume": a_btn_resume, "btn_run_queue": a_btn_run_queue,
    "btn_scan": a_btn_scan, "btn_free_run": a_btn_free_run, "btn_stop": a_btn_stop,
    "messagebox": a_messagebox, "plot_mgr": plot_mgr,
})

# 8) Register GUI hooks (called by motion/scan_engine via ui_hook, partly from
#    worker threads; the adapters/invoker marshal onto the GUI thread).
register_hook("set_buttons_scanning", set_buttons_scanning)
register_hook("set_buttons_connected", set_buttons_connected)
register_hook("set_direction_label",
              lambda txt: _invoker.post(lambda: lbl_direction_state.setText(txt)))
register_hook("set_reference_button_enabled",
              lambda en: a_btn_reference_run.configure(state=("normal" if en else "disabled")))
register_hook("show_warning", lambda title, msg: a_messagebox.showwarning(title, msg))
register_hook("show_error", lambda title, msg: a_messagebox.showerror(title, msg))
register_hook("show_info", lambda title, msg: a_messagebox.showinfo(title, msg))
register_hook("ui_after", lambda fn: _invoker.post(fn))

a_btn_reference_run = _ButtonAdapter(btn_reference_run)

# =====================================================================
# BACKLASH CALIBRATION DIALOG (PS-2600A style)
# =====================================================================
class CalibrationDialog(QtWidgets.QDialog):
    """Separate window for backlash calibration via a reference line.

    Crosses the line twice (up/down) via backlash_cal.run_calibration(), shows
    both sweeps live in a mini plot and the measured backlash. 'Apply & Save'
    writes the value into the slip field + persists it. Callbacks from the worker
    thread are marshalled onto the GUI thread via _invoker."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Backlash — Calibration")
        self.setModal(False)
        self.resize(680, 560)
        # Apply the app theme EXPLICITLY to this dialog. app.setStyleSheet()
        # is supposed to cascade to every widget, but a QDialog is its own
        # top-level window and in practice ended up with light/default field
        # and plot backgrounds here (visible in the rig screenshot: input
        # boxes and the mini plot both rendered pale against the dark app).
        # Setting it on the dialog removes the dependency on that cascade.
        self.setStyleSheet(_QSS)
        self.setAutoFillBackground(True)
        self._measured = None
        self._ux = []; self._uy = []; self._dx = []; self._dy = []

        lay = QtWidgets.QVBoxLayout(self)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(10)

        intro = QtWidgets.QLabel(
            "Reference lamp on, monochromator near the line. The line is crossed "
            "upward and downward; the offset of the two peaks is the backlash.")
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color:{C_MUTED};")
        lay.addWidget(intro)

        # inputs
        form_box = QtWidgets.QFrame(); form_box.setObjectName("Card")
        form = QtWidgets.QFormLayout(form_box)
        form.setContentsMargins(12, 10, 12, 10)
        self.in_center = QtWidgets.QLineEdit(_saved_settings.get("cal_center_nm") or CAL_CENTER_NM_DEFAULT)
        self.in_span = QtWidgets.QLineEdit(_saved_settings.get("cal_span_nm") or CAL_SPAN_NM_DEFAULT)
        self.in_step = QtWidgets.QLineEdit(_saved_settings.get("cal_step_nm") or CAL_STEP_NM_DEFAULT)
        self.in_dwell = QtWidgets.QLineEdit(_saved_settings.get("cal_dwell_s") or CAL_DWELL_S_DEFAULT)
        form.addRow("Reference line λ (nm)", self.in_center)
        form.addRow("Span ± (nm)", self.in_span)
        form.addRow("Step (nm)", self.in_step)
        form.addRow("Dwell (s)", self.in_dwell)
        lay.addWidget(form_box)

        # live plot (up = teal, down = amber)
        # Background/foreground set on the instance, not left to pyqtgraph's
        # global config: the global default is applied at import time and the
        # dialog's plot came out light-themed while the main scope was dark.
        self._plot = pg.PlotWidget(background=C_BG)
        self._plot.getPlotItem().getAxis("bottom").setPen(pg.mkPen(C_BORDER))
        self._plot.getPlotItem().getAxis("left").setPen(pg.mkPen(C_BORDER))
        self._plot.getPlotItem().getAxis("bottom").setTextPen(pg.mkPen(C_MUTED))
        self._plot.getPlotItem().getAxis("left").setTextPen(pg.mkPen(C_MUTED))
        self._plot.getPlotItem().setLabel("bottom", "Wavelength", units="nm")
        self._plot.getPlotItem().setLabel("left", "Signal", units="V")
        self._plot.getPlotItem().showGrid(x=True, y=True, alpha=0.15)
        self._plot.getPlotItem().addLegend(offset=(10, 10))
        self._cu = self._plot.plot([], [], pen=pg.mkPen(C_ACCENT, width=2), name="up (+)")
        self._cd = self._plot.plot([], [], pen=pg.mkPen(C_WARN, width=2), name="down (−)")
        lay.addWidget(self._plot, 1)

        # status + result
        self._status = QtWidgets.QLabel("Ready.")
        self._status.setStyleSheet(f"color:{C_MUTED};")
        lay.addWidget(self._status)

        res = QtWidgets.QHBoxLayout()
        self._lbl_up = QtWidgets.QLabel("up-peak: —")
        self._lbl_down = QtWidgets.QLabel("down-peak: —")
        self._lbl_slip = QtWidgets.QLabel("Backlash: —")
        self._lbl_slip.setObjectName("ReadoutValAccent")
        res.addWidget(self._lbl_up); res.addWidget(self._lbl_down)
        res.addStretch(1); res.addWidget(self._lbl_slip)
        lay.addLayout(res)

        # buttons
        row = QtWidgets.QHBoxLayout()
        self.btn_run = _btn("Calibrate", "Primary")
        self.btn_abort = _btn("Abort", "Danger"); self.btn_abort.setEnabled(False)
        self.btn_apply = _btn("Apply & Save"); self.btn_apply.setEnabled(False)
        self.btn_close = _btn("Close")
        row.addWidget(self.btn_run); row.addWidget(self.btn_abort)
        row.addStretch(1); row.addWidget(self.btn_apply); row.addWidget(self.btn_close)
        lay.addLayout(row)

        self.btn_run.clicked.connect(self._on_run)
        self.btn_abort.clicked.connect(self._on_abort)
        self.btn_apply.clicked.connect(self._on_apply)
        self.btn_close.clicked.connect(self.close)

        # Persist these fields on change, same pattern as the main window's
        # fields (see _persist() + app_config.DEFAULT_CONFIG). Without this
        # they'd be exactly the kind of "value stuck in a .py file" the user
        # objected to.
        self.in_center.editingFinished.connect(lambda: _persist("cal_center_nm", self.in_center.text()))
        self.in_span.editingFinished.connect(lambda: _persist("cal_span_nm", self.in_span.text()))
        self.in_step.editingFinished.connect(lambda: _persist("cal_step_nm", self.in_step.text()))
        self.in_dwell.editingFinished.connect(lambda: _persist("cal_dwell_s", self.in_dwell.text()))

    # --- worker callbacks (marshalled onto the GUI thread) ------------
    def _on_point(self, d, nm, v):
        def do():
            if d > 0:
                self._ux.append(nm); self._uy.append(v); self._cu.setData(self._ux, self._uy)
            else:
                self._dx.append(nm); self._dy.append(v); self._cd.setData(self._dx, self._dy)
        _invoker.post(do)

    def _on_progress(self, txt):
        _invoker.post(lambda: self._status.setText(txt))

    def _on_finished(self, res):
        _invoker.post(lambda: self._finish(res))

    def _finish(self, res):
        self.btn_run.setEnabled(True)
        self.btn_abort.setEnabled(False)
        if res:
            self._measured = res["slip_nm"]
            self._lbl_up.setText(f"up-peak: {res['up_peak']:.3f} nm")
            self._lbl_down.setText(f"down-peak: {res['down_peak']:.3f} nm")
            self._lbl_slip.setText(f"Backlash: {res['slip_nm']:.3f} nm")
            self._status.setText("Done. Review the value and apply if OK.")
            self.btn_apply.setEnabled(True)
        else:
            self._status.setText("Aborted or no peak found.")

    # --- button handlers -----------------------------------------------
    def _on_run(self):
        if not ser_ok():
            self._status.setText("Not connected.")
            return
        try:
            center = float(self.in_center.text())
            span = float(self.in_span.text())
            step = float(self.in_step.text())
            dwell = float(self.in_dwell.text())
        except Exception:
            self._status.setText("Invalid input.")
            return
        self._ux = []; self._uy = []; self._dx = []; self._dy = []
        self._cu.setData([], []); self._cd.setData([], [])
        self._measured = None
        self.btn_apply.setEnabled(False)
        self.btn_run.setEnabled(False)
        self.btn_abort.setEnabled(True)
        self._status.setText("Calibration running…")
        backlash_cal.run_calibration(
            center, span, step, dwell,
            on_point=self._on_point, on_progress=self._on_progress,
            on_finished=self._on_finished)

    def _on_abort(self):
        backlash_cal.abort_calibration()
        self._status.setText("Abort requested…")

    def _on_apply(self):
        if self._measured is None:
            return
        val = float(self._measured)
        # a_slip is an _EntryAdapter -> set via delete/insert:
        try:
            a_slip.delete(0, "end"); a_slip.insert(0, f"{val:.3f}")
        except Exception:
            pass
        try:
            app_config.save(backlash_slip_nm=val)
            log(f"[CAL] Applied backlash {val:.3f} nm and saved.")
            _invoker.post(lambda: _lbl_slip.setText(f"Backlash: {val:.3f} nm"))
        except Exception as e:
            log(f"[CAL] Save failed: {e}", "warn")
        self.close()


_cal_dialog = None


def _open_cal_dialog():
    """Open the calibration dialog (singleton, so multiple windows can't start
    a measurement at once)."""
    global _cal_dialog
    if _cal_dialog is None:
        _cal_dialog = CalibrationDialog(_window)
    _cal_dialog.show()
    _cal_dialog.raise_()
    _cal_dialog.activateWindow()


# 9) Connect buttons to actions.
btn_conn_toggle.clicked.connect(_on_conn_toggle)
btn_goto.clicked.connect(goto_wavelength_action)
btn_scan.clicked.connect(scan_action)
btn_free_run.clicked.connect(free_run_scan_action)
btn_pause.clicked.connect(pause_action)
btn_resume.clicked.connect(resume_action)
btn_stop.clicked.connect(stop_action)
btn_estop.clicked.connect(stop_action)
btn_jog_minus.clicked.connect(jog_minus_action)
btn_jog_plus.clicked.connect(jog_plus_action)
btn_mark_point.clicked.connect(mark_point_action)
btn_add_queue.clicked.connect(add_scan_to_queue)
btn_update_queue.clicked.connect(update_selected_scan)
btn_remove_queue.clicked.connect(remove_selected_scan)
btn_run_queue.clicked.connect(run_scan_queue_action)
btn_clear_queue.clicked.connect(clear_scan_queue)
btn_clear_plot.clicked.connect(clear_plot_action)
btn_save_now.clicked.connect(lambda: export_active_scan_csv())
# NOT `connect(reference_run_action)`: Qt's clicked signal emits the button's
# `checked` bool, and PyQt passes it to any slot that accepts an argument.
# reference_run_action(on_done=None) accepts one, so the bool landed in
# on_done -- which then failed at the end of the move with
# "[GUI] marshalled call failed: 'bool' object is not callable". The move
# itself ran fine, only the completion callback blew up, which is why it
# looked like a cosmetic message rather than a wiring bug.
btn_reference_run.clicked.connect(lambda: reference_run_action())
btn_calibrate.clicked.connect(_open_cal_dialog)


def _on_set_current_lambda():
    """Offset-calibration button: re-anchor current_nm to the entered known
    wavelength (no motor move). Persists the new value straight away so a
    restart keeps the corrected anchor, and refreshes the last-saved-nm box
    so _refresh_readouts doesn't immediately re-persist a stale value."""
    txt = entry_cal_known_nm.text().strip()
    if not txt:
        a_messagebox.showwarning("Calibration", "Enter the known wavelength first.")
        return
    try:
        nm = _parse_float_loose(txt)
    except Exception:
        a_messagebox.showerror("Calibration", "Invalid wavelength.")
        return
    if set_current_wavelength(nm):
        try:
            _persist("last_current_nm", nm)
            _last_saved_nm[0] = nm
        except Exception:
            pass


btn_set_current.clicked.connect(_on_set_current_lambda)
btn_browse.clicked.connect(_browse_export_dir)
entry_slip.editingFinished.connect(_on_slip_edited)
btn_swap_start_end.clicked.connect(_on_swap_start_end)


def _on_free_run_sp_edited():
    """Clamp the Free Run speed field to [1, RAMP_SP] on every edit --
    RAMP_SP (device_constants.py) is the rig-confirmed normal speed, this
    field may only go SLOWER, never faster, regardless of what the user
    types. Rewrites the field with the clamped value so what's displayed is
    always what will actually be sent.

    INVALID/EMPTY INPUT FALLS BACK TO THE LAST VALID VALUE, NOT TO RAMP_SP.
    Falling back to RAMP_SP (the MAXIMUM speed) would be dangerous here:
    editingFinished also fires on focus loss, so merely clicking into the
    field, clearing it and clicking away would silently rewrite it to full
    speed -- the next sweep would then run up to 10x faster than the one
    that was set up, with no warning beyond the missing override line in the
    log. Falling back to the previous value keeps an accidental edit from
    becoming a speed change; a deliberate speed change still needs a valid
    number.

    NOTE: the last valid value is held in _last_valid_free_run_sp, NOT read
    back from the adapter -- the adapter's shadow follows textChanged, so by
    the time this runs on an emptied field the shadow is already ""."""
    try:
        val = int(float(entry_free_run_sp.text()))
    except Exception:
        val = _last_valid_free_run_sp[0]
        log(f"[FREE RUN] Speed field was empty/invalid -- kept {val} rpm "
            f"(NOT reset to the {RAMP_SP} rpm maximum).", "warn")
    clamped = max(1, min(val, RAMP_SP))
    if clamped != val:
        log(f"[FREE RUN] Speed {val} rpm exceeds the configured limit "
            f"({RAMP_SP} rpm, device_constants.RAMP_SP) -- clamped to {clamped}.", "warn")
    _last_valid_free_run_sp[0] = clamped
    a_free_run_sp.delete(0, "end"); a_free_run_sp.insert(0, str(clamped))
    _persist("free_run_sp_rpm", clamped)


entry_free_run_sp.editingFinished.connect(_on_free_run_sp_edited)

# Persist every remaining user-entered field on change (see
# app_config.DEFAULT_CONFIG for the full key list). QLineEdit -> editingFinished
# (fires on Enter/focus-loss, not per keystroke); QComboBox -> currentTextChanged;
# QCheckBox -> stateChanged. entry_slip/entry_current are handled separately
# above/below (slip has its own status-label update; current_nm is throttled
# in _refresh_readouts since it changes continuously during a scan).
entry_port.editingFinished.connect(lambda: _persist("port", entry_port.text()))
entry_baud.editingFinished.connect(lambda: _persist("baud", entry_baud.text()))
entry_start.editingFinished.connect(lambda: _persist("scan_start_nm", entry_start.text()))
entry_end.editingFinished.connect(lambda: _persist("scan_end_nm", entry_end.text()))
entry_step.editingFinished.connect(lambda: _persist("scan_step_nm", entry_step.text()))
entry_wait.editingFinished.connect(lambda: _persist("scan_wait_ms", entry_wait.text()))
entry_jog_step.editingFinished.connect(lambda: _persist("jog_step_nm", entry_jog_step.text()))
entry_avg_samples.editingFinished.connect(lambda: _persist("avg_samples", entry_avg_samples.text()))
entry_avg_fraction.editingFinished.connect(lambda: _persist("avg_fraction_pct", entry_avg_fraction.text()))
entry_presettle.editingFinished.connect(lambda: _persist("presettle_ms", entry_presettle.text()))
_avg_mode_combo.currentTextChanged.connect(lambda t: _persist("avg_mode", t))
entry_export_dir.editingFinished.connect(lambda: _persist("export_dir", entry_export_dir.text()))
entry_pattern.editingFinished.connect(lambda: _persist("export_pattern", entry_pattern.text()))
_chk_autosave.stateChanged.connect(lambda s: _persist("autosave_csv", bool(s)))
entry_daq_dev.editingFinished.connect(lambda: _persist("daq_dev", entry_daq_dev.text()))
_daq_ai_combo.currentTextChanged.connect(lambda t: _persist("daq_ai", t))

def _persist_all_fields():
    """Write EVERY persistent field to the settings file.

    WHY: each field persists itself on `editingFinished`, which only fires on
    focus loss or Enter. Typing a value and closing the window straight away
    therefore lost it -- most visibly for the averaging settings, which are
    often the last thing touched before starting a measurement. Called on
    close so what is on screen is what comes back next session."""
    pairs = [
        ("port", entry_port), ("baud", entry_baud),
        ("scan_start_nm", entry_start), ("scan_end_nm", entry_end),
        ("scan_step_nm", entry_step), ("scan_wait_ms", entry_wait),
        ("presettle_ms", entry_presettle),
        ("avg_samples", entry_avg_samples), ("avg_fraction_pct", entry_avg_fraction),
        ("free_run_sp_rpm", entry_free_run_sp), ("jog_step_nm", entry_jog_step),
        ("export_dir", entry_export_dir), ("export_pattern", entry_pattern),
        ("daq_dev", entry_daq_dev), ("slip_nm", entry_slip),
    ]
    for key, w in pairs:
        try:
            _persist(key, w.text())
        except Exception:
            pass
    try:
        _persist("avg_mode", _avg_mode_combo.currentText())
        _persist("daq_ai", _daq_ai_combo.currentText())
        _persist("autosave_csv", bool(_chk_autosave.isChecked()))
    except Exception:
        pass


# Intercept window close.
_orig_close = _window.closeEvent
def _close_event(ev):
    _persist_all_fields()
    on_close()
    ev.accept()
_window.closeEvent = _close_event


# =====================================================================
# LIVE-READOUT-/STATUS-REFRESH
# =====================================================================
_last_saved_nm = [None]  # mutable box so the closure below can update it


def _update_avg_hint():
    """Spell out, in milliseconds, what the current averaging settings mean.

    WHY: 'Avg fraction 50 %' is abstract -- 50 % of what, and how much
    averaging time is that? This turns the settings into the numbers the user
    actually cares about, recomputed live from the Timebase and Pre-settle
    fields. Once a point has actually been measured it additionally reports
    the MEASURED read count and rate from daq.acquire_measurement's telemetry
    (state['last_avg_*']) rather than a guessed sample rate.

    Pure display; touches no hardware. Runs on the GUI thread from the 250 ms
    readout timer."""
    try:
        timebase_ms = float(entry_wait.text() or 0)
    except Exception:
        timebase_ms = 0.0
    try:
        presettle_ms = float(entry_presettle.text() or 0)
    except Exception:
        presettle_ms = 0.0
    presettle_ms = max(0.0, min(presettle_ms, timebase_ms))
    avail_ms = max(0.0, timebase_ms - presettle_ms)
    mode = _avg_mode_combo.currentText()

    if mode == "time":
        try:
            frac = float(entry_avg_fraction.text() or 0)
        except Exception:
            frac = 0.0
        frac = max(0.0, min(100.0, frac))
        window_ms = avail_ms * frac / 100.0
        idle_ms = max(0.0, avail_ms - window_ms)
        txt = (f"{timebase_ms:.0f} ms timebase = {presettle_ms:.0f} ms pre-settle "
               f"+ {avail_ms:.0f} ms available. "
               f"{frac:.0f} % of that = <b>{window_ms:.0f} ms averaging</b>, "
               f"{idle_ms:.0f} ms idle.")
    else:
        try:
            n = int(float(entry_avg_samples.text() or 1))
        except Exception:
            n = 1
        txt = (f"{timebase_ms:.0f} ms timebase = {presettle_ms:.0f} ms pre-settle "
               f"+ {avail_ms:.0f} ms available. Mode 'samples': "
               f"<b>{n} DAQ reads</b> (fraction is ignored), remainder idle.")

    # Measured reality from the last acquired point, if there is one.
    try:
        w = state.get('last_avg_window_s')
        d = state.get('last_avg_dur_s')
        if d and d > 0:
            n_reads = state.get('last_avg_reads')
            if n_reads:
                # Only quote a rate when the measurement lasted long enough for
                # it to mean anything. With Timebase 0 the whole point takes
                # well under a millisecond and the division produced absurd
                # figures like "249661.0 Hz" in the rig screenshot -- a number
                # that is arithmetically correct and physically meaningless.
                if d >= 0.01:
                    txt += (f"<br>Last point: {d*1000:.0f} ms, {n_reads} reads "
                            f"({n_reads/d:.1f} Hz).")
                else:
                    txt += (f"<br>Last point: {d*1000:.1f} ms, {n_reads} reads "
                            f"(too short to quote a rate).")
            else:
                txt += f"<br>Last point measured in {d*1000:.0f} ms."
    except Exception:
        pass
    _avg_hint.setText(txt)


def _refresh_readouts():
    """Periodically (250 ms, GUI thread) refresh the header readouts + status bar
    from the shared state. Also persists 'last_current_nm' when it changed by
    a meaningful amount, so a restart can restore where the monochromator was
    left (see app_config.DEFAULT_CONFIG for the hardware-readback caveat).
    Throttled to real changes (not every 250 ms tick) so a running scan
    doesn't hammer the settings file with writes."""
    try:
        nm = float(state.get('current_nm', 0.0))
        ro_lambda.setText(f"{nm:.3f}")
        if _last_saved_nm[0] is None or abs(nm - _last_saved_nm[0]) >= 0.0005:
            _last_saved_nm[0] = nm
            _persist("last_current_nm", nm)
    except Exception:
        ro_lambda.setText("—")
    fsm = state.get("fsm", "IDLE")
    ro_state.setText(fsm)
    _lbl_fsm.setText(f"State: {fsm}")
    try:
        _update_avg_hint()
    except Exception:
        pass


_ro_timer = QTimer()
_ro_timer.timeout.connect(_refresh_readouts)
_ro_timer.start(250)


def _refresh_temperature():
    """Update the status-bar housing-temperature readout (TEM).

    WHEN: own slow timer, every 15 s, and ONLY while connected AND fully idle
    (no scan, no move, FSM == IDLE). Deliberately kept off the 250 ms readout
    timer: TEM costs a real serial round-trip on the same single-lock port
    that POS polling uses, and the Free Run / wait loops are already the
    measured bottleneck at ~19-20 Hz (see probe_move_timing.py). Injecting an
    extra query mid-move would steal poll slots from position tracking, so it
    simply doesn't run then -- the value goes stale-but-labelled rather than
    disturbing a scan.

    Runs in a worker thread because it touches the serial port; the label
    update is marshalled back onto the GUI thread."""
    if not state.get("connected"):
        _lbl_temp.setText("Temp: —")
        return
    if state.get("is_scanning") or state.get("fsm", "IDLE") != "IDLE":
        return  # leave the last value standing; do not touch the port
    def worker():
        try:
            t = read_temperature()
        except Exception:
            t = None
        def do():
            _lbl_temp.setText(f"Temp: {t} °C" if t is not None else "Temp: —")
        _invoker.post(do)
    threading.Thread(target=worker, daemon=True).start()


_temp_timer = QTimer()
_temp_timer.timeout.connect(_refresh_temperature)
_temp_timer.start(15000)


# =====================================================================
# INITIAL STATE
# =====================================================================
set_buttons_connected(False)
set_fsm("IDLE")
try:
    _lbl_slip.setText(f"Backlash: {float(a_slip.get()):.3f} nm")
except Exception:
    pass
if _restored_nm is not None:
    # Emitted here (not during widget construction) because the console log
    # sink is only registered a few lines above, in the wiring block.
    log(f"[POS] Restored last known position {state['current_nm']:.3f} nm from "
        f"the previous session. This is a SOFTWARE estimate, not a hardware "
        f"readback -- if the motor was moved (by hand or another program) "
        f"while the app was closed, this value is wrong. Verify with a "
        f"Reference Run / a known line before trusting it for precision work.",
        "warn")


def run():
    """Show the window and start the Qt event loop (blocks until close).
    WHEN: called from main.py."""
    _window.show()
    app.exec()
