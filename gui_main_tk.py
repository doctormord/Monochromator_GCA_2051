"""
gui_main.py — Tkinter UI + wiring of the VRS41 controller.

WHAT THIS MODULE DOES
---------------------
Builds the complete Tk window (connection, goto, scan parameters, averaging,
jog, queue, backlash controls, live plot, console) and WIRES the
foundation/orchestration modules to it:

  - registers the console as the log sink (app_context.set_log_sink), so
    every module writes into the console widget via app_context.log();
  - registers all tk.StringVars into the ui registry (register_var), so
    motion/daq read their values via ui_get;
  - registers buttons/entries/listbox/messagebox/PlotManager into
    app_context.refs, so scan_engine can drive them duck-typed;
  - registers the GUI hooks (set_buttons_*, set_direction_label,
    set_reference_button_enabled, show_warning/error, ui_after), which
    motion/scan_engine call via ui_hook;
  - connects the buttons to the actions in scan_engine/motion.

This is the ONLY module (besides main.py) that imports tkinter. The build
runs at import time (as in the old monolith); main.py then only starts the
mainloop.

Origin: assembled from the monolith (PlotManager, log, set_buttons,
on_connect/disconnect, widget build). get_points is now a real PlotManager
method (used to be a later monkeypatch).

IMPORTANT (doc policy, see HANDOVER.md): docstrings/comments are preserved.
"""

import os
import tkinter as tk
from tkinter import ttk
from tkinter import scrolledtext
from tkinter import filedialog as fd
from tkinter import messagebox

import serial

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

import app_context
from app_context import (
    state, log, set_fsm, register_var, register_hook, refs, set_log_sink,
)
from device_constants import (
    DEFAULT_PORT, DEFAULT_BAUD, SUPPRESS_TIMEOUT_MESSAGES,
    AVG_SAMPLES_DEFAULT, AVG_MODE_DEFAULT, AVG_FRACTION_PCT_DEFAULT,
    PRESETTLE_MS_DEFAULT,
)
import app_config
from protocol_faulhaber import ser_ok, safe_stop, init_motor
from motion import reference_run_action
from scan_engine import (
    goto_wavelength_action, scan_action, pause_action, resume_action,
    stop_action, jog_minus_action, jog_plus_action, mark_point_action,
    add_scan_to_queue, remove_selected_scan, run_scan_queue_action,
    clear_scan_queue, clear_plot_action, export_active_scan_csv,
)


class PlotManager:
    """
    Minimal, self-contained plotting helper.
    - Maintains multiple scan series (lines) and one jog layer (markers only).
    - Decimates redraws to ~15 Hz using root.after; thread-safe appends.
    - No effect on FSM/serial; only called from existing hooks.
    """
    def __init__(self, root, ax, canvas):
        import threading
        self.root = root
        self.ax = ax
        self.canvas = canvas
        self._lock = threading.Lock()
        self._plots = {}           # plot_id -> {"x": [], "y": [], "line": Line2D, "label": str, "status": str}
        self._active_id = None
        # Dedicated jog layer (markers only)
        (jog_line,) = ax.plot([], [], linestyle="None", marker="o", label="Jog")
        self._jog = {"x": [], "y": [], "line": jog_line}
        # Axes labels (keep existing style if already set elsewhere)
        if not ax.get_xlabel(): ax.set_xlabel("Wavelength (nm)")
        if not ax.get_ylabel(): ax.set_ylabel("Signal (V)")
        # Start redraw loop
        self._schedule_redraw()

    def _schedule_redraw(self):
        self.root.after(66, self._redraw)  # ~15 Hz

    def _redraw(self):
        with self._lock:
            # Update all series
            for meta in self._plots.values():
                meta["line"].set_data(meta["x"], meta["y"])
            self._jog["line"].set_data(self._jog["x"], self._jog["y"])
            # Auto-scale smartly
            self.ax.relim()
            self.ax.autoscale_view()
            # Legend: show one entry per scan + Jog
            try:
                self.ax.legend(loc="best", frameon=False)
            except Exception:
                pass
            self.canvas.draw_idle()
        self._schedule_redraw()

    def start_new_scan(self, label: str, meta: dict) -> str:
        # Create a new line with automatic color
        (line,) = self.ax.plot([], [], label=label)
        pid = f"scan_{len(self._plots)+1}"
        with self._lock:
            self._plots[pid] = {"x": [], "y": [], "line": line, "label": label, "status": "active", "meta": dict(meta or {})}
            self._active_id = pid
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

    def finalize_scan(self, plot_id: str, status: str):
        with self._lock:
            meta = self._plots.get(plot_id)
            if meta:
                meta["status"] = status

    def append_jog_point(self, x_nm: float, y_val: float):
        with self._lock:
            self._jog["x"].append(float(x_nm))
            self._jog["y"].append(float(y_val))

    def clear_all(self):
        with self._lock:
            # Remove lines from axes
            for meta in self._plots.values():
                try:
                    meta["line"].remove()
                except Exception:
                    pass
            try:
                self._jog["line"].remove()
            except Exception:
                pass
            self._plots.clear()
            # Recreate jog layer
            (jog_line,) = self.ax.plot([], [], linestyle="None", marker="o", label="Jog")
            self._jog = {"x": [], "y": [], "line": jog_line}
            self._active_id = None
        # Force a redraw
        self.canvas.draw_idle()

    def get_points(self, plot_id):
        """Return xs, ys for a scan series from the internal store."""
        try:
            # Primary: our _plots dict used by this PlotManager
            with self._lock:
                rec = self._plots.get(plot_id)
                if rec:
                    xs = list(rec.get("x") or [])
                    ys = list(rec.get("y") or [])
                    return xs, ys
        except Exception:
            pass
        # Fallbacks for legacy structures (will be empty on this build)
        try:
            d = getattr(self, "_store", {}).get(plot_id)
            if d:
                xs = d.get("xs") or d.get("x") or []
                ys = d.get("ys") or d.get("y") or []
                return list(xs), list(ys)
        except Exception:
            pass
        try:
            rec = getattr(self, "series", {}).get(plot_id)
            if rec:
                return list(rec.get("xs", [])), list(rec.get("ys", []))
        except Exception:
            pass
        return [], []


def _console_log_sink(msg, tag="info"):
    # Suppress timeout messages if desired
    try:
        if SUPPRESS_TIMEOUT_MESSAGES and (('[TIMEOUT]' in str(msg)) or ('timeout' in str(msg).lower())):
            return
    except Exception:
        pass
    console.configure(state="normal")
    console.insert("end", msg + "\n", tag)
    console.see("end")
    console.configure(state="disabled")

def set_buttons_connected(connected: bool):
    btn_connect.configure(state=("disabled" if connected else "normal"))
    btn_disconnect.configure(state=("normal" if connected else "disabled"))
    btn_goto.configure(state=("normal" if connected else "disabled"))
    btn_scan.configure(state=("normal" if connected else "disabled"))
    btn_pause.configure(state=("disabled"))
    btn_resume.configure(state=("disabled"))
    btn_stop.configure(state=("normal" if connected else "disabled"))

    # Jog & Queue availability
    try:
        btn_jog_minus.configure(state=("normal" if connected else "disabled"))
        btn_jog_plus.configure(state=("normal" if connected else "disabled"))
        btn_mark_point.configure(state=("normal" if connected else "disabled"))
        btn_add_queue.configure(state=("normal" if connected else "disabled"))
        btn_remove_queue.configure(state=("normal" if connected else "disabled"))
        btn_run_queue.configure(state=("normal" if connected else "disabled"))
        btn_clear_queue.configure(state=("normal" if connected else "disabled"))
    except Exception:
        pass

def set_buttons_scanning(scanning: bool):
    """Disable conflicting controls while scanning and toggle pause/resume."""
    if scanning:
        btn_goto.configure(state="disabled")
        btn_disconnect.configure(state="disabled")
        btn_scan.configure(state="disabled")
        btn_pause.configure(state="normal")
        btn_resume.configure(state="disabled")
        btn_stop.configure(state="normal")
    else:
        set_buttons_connected(state["connected"])

    # Toggle jog/queue while scanning
    try:
        if scanning:
            btn_jog_minus.configure(state="disabled")
            btn_jog_plus.configure(state="disabled")
            btn_mark_point.configure(state="disabled")
            btn_run_queue.configure(state="disabled")
        else:
            btn_jog_minus.configure(state=("normal" if state["connected"] else "disabled"))
            btn_jog_plus.configure(state=("normal" if state["connected"] else "disabled"))
            btn_mark_point.configure(state=("normal" if state["connected"] else "disabled"))
            btn_run_queue.configure(state=("normal" if state["connected"] else "disabled"))
    except Exception:
        pass

def on_connect():
    port = port_var.get().strip()
    try:
        baud = int(baud_var.get())
    except Exception as e:
        baud = DEFAULT_BAUD
    try:
        ser = serial.Serial(port, baud, timeout=0.8)
        state["ser"] = ser
        state["connected"] = True
        set_buttons_connected(True)
        set_fsm("IDLE")
        log(f"[OK] Connected {port} @ {baud} baud")
        init_motor()
        log("[OK] Motor initialized (EN, HP0, V0)")
    except Exception as e:
        log(f"[ERROR] Could not open {port}: {e}", "error")
        state["ser"] = None
        state["connected"] = False
        set_buttons_connected(False)

        set_fsm("IDLE")

def on_disconnect():
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

# ------------------ GUI Build ------------------
root = tk.Tk()
root.title("Monochromator Controller – OST/POS")
root.geometry("980x640")

main = ttk.Frame(root, padding=8)
main.pack(fill="both", expand=True)

left = ttk.Frame(main)



# ---- Software # [soft-limit UI removed]


# ---- DAQ Channel selection (ai0–ai10) ----
try:
    ttk.Separator(left).grid(row=38, column=0, columnspan=2, sticky="ew", pady=(8,4))
    ttk.Label(left, text="DAQ device").grid(row=39, column=0, sticky="w")
    if 'daq_dev_var' not in globals():
        daq_dev_var = tk.StringVar(value=state.get('daq_dev', 'Dev1'))
        globals()['daq_dev_var'] = daq_dev_var
    else:
        daq_dev_var = globals()['daq_dev_var']
    entry_daq_dev = ttk.Entry(left, textvariable=daq_dev_var, width=12)
    entry_daq_dev.grid(row=39, column=1, sticky="ew")

    def _on_dev_change(*_):
        try:
            state['daq_dev'] = daq_dev_var.get()
        except Exception:
            pass
    try:
        daq_dev_var.trace_add('write', lambda *_: _on_dev_change())
    except Exception:
        # Tk on older versions: trace method name is 'trace'
        daq_dev_var.trace('w', lambda *_: _on_dev_change())

    ttk.Label(left, text="Analog input").grid(row=40, column=0, sticky="w")
    channels = [f"ai{i}" for i in range(0, 11)]
    if 'daq_ai_var' not in globals():
        daq_ai_var = tk.StringVar(value=state.get('daq_ai', 'ai1'))
        globals()['daq_ai_var'] = daq_ai_var
    else:
        daq_ai_var = globals()['daq_ai_var']
    combo_ai = ttk.Combobox(left, values=channels, textvariable=daq_ai_var, state="readonly", width=12)
    combo_ai.grid(row=40, column=1, sticky="ew")

    def _on_ai_change(*_):
        try:
            state['daq_ai'] = daq_ai_var.get()
        except Exception:
            pass
    try:
        daq_ai_var.trace_add('write', lambda *_: _on_ai_change())
    except Exception:
        daq_ai_var.trace('w', lambda *_: _on_ai_change())
except Exception as e:
    try:
        log(f"[UI] DAQ channel UI init failed: {e}", "warn")
    except Exception:
        print(f"[UI] DAQ channel UI init failed: {e}")

# ---- CSV Export controls ----
try:
    ttk.Separator(left).grid(row=40, column=0, columnspan=2, sticky="ew", pady=(8,4))
    ttk.Label(left, text="Auto-save CSV after scan").grid(row=41, column=0, sticky="w")
    autosave_csv_var = tk.BooleanVar(value=False)
    ttk.Checkbutton(left, variable=autosave_csv_var).grid(row=41, column=1, sticky="w")

    ttk.Label(left, text="Folder").grid(row=42, column=0, sticky="w")
    export_dir_var = tk.StringVar(value=os.path.expanduser("~/scans"))
    entry_export_dir = ttk.Entry(left, textvariable=export_dir_var, width=20)
    entry_export_dir.grid(row=42, column=1, sticky="ew")
    def browse_export_dir():
        d = fd.askdirectory() or ""
        if d: export_dir_var.set(d)
    ttk.Button(left, text="Browse…", command=browse_export_dir).grid(row=42, column=1, sticky="e", padx=(0,0))

    ttk.Label(left, text="Filename pattern").grid(row=43, column=0, sticky="w")
    pattern_var = tk.StringVar(value="scan_{date}_{time}_{start}-{end}nm_{mode}.csv")
    entry_pattern = ttk.Entry(left, textvariable=pattern_var, width=20)
    entry_pattern.grid(row=43, column=1, sticky="ew")

    ttk.Button(left, text="Save CSV now", command=lambda: export_active_scan_csv()).grid(row=44, column=0, columnspan=2, sticky="ew", pady=(6,0))
except Exception:
    pass
left.pack(side="left", fill="y", padx=(0,8))

right = ttk.Frame(main)
right.pack(side="right", fill="both", expand=True)

# Connection
ttk.Label(left, text="Port").grid(row=0, column=0, sticky="w")
port_var = tk.StringVar(value=DEFAULT_PORT)
entry_port = ttk.Entry(left, textvariable=port_var, width=12); entry_port.grid(row=0, column=1, sticky="ew")

ttk.Label(left, text="Baud").grid(row=1, column=0, sticky="w")
baud_var = tk.StringVar(value=str(DEFAULT_BAUD))
entry_baud = ttk.Entry(left, textvariable=baud_var, width=12); entry_baud.grid(row=1, column=1, sticky="ew")

btn_connect = ttk.Button(left, text="Connect", command=on_connect); btn_connect.grid(row=2, column=0, sticky="ew", pady=(6,0))
btn_disconnect = ttk.Button(left, text="Disconnect", command=on_disconnect); btn_disconnect.grid(row=2, column=1, sticky="ew", pady=(6,0))

# Go To
ttk.Label(left, text="Go To (nm)").grid(row=3, column=0, sticky="w", pady=(12,0))
entry_goto = ttk.Entry(left, width=12); entry_goto.grid(row=3, column=1, sticky="ew", pady=(12,0))
btn_goto = ttk.Button(left, text="Go", command=goto_wavelength_action); btn_goto.grid(row=4, column=0, columnspan=2, sticky="ew")

# Scan params
ttk.Label(left, text="Start (nm)").grid(row=5, column=0, sticky="w", pady=(12,0))
entry_start = ttk.Entry(left, width=12); entry_start.grid(row=5, column=1, sticky="ew", pady=(12,0))

ttk.Label(left, text="End (nm)").grid(row=6, column=0, sticky="w")
entry_end = ttk.Entry(left, width=12); entry_end.grid(row=6, column=1, sticky="ew")

ttk.Label(left, text="Step (nm)").grid(row=7, column=0, sticky="w")
entry_step = ttk.Entry(left, width=12); entry_step.grid(row=7, column=1, sticky="ew")

ttk.Label(left, text="Wait (s)").grid(row=8, column=0, sticky="w")
entry_wait = ttk.Entry(left, width=12); entry_wait.grid(row=8, column=1, sticky="ew")


# Averaging controls
ttk.Label(left, text="Avg samples").grid(row=20, column=0, sticky="w")
avg_samples_var = tk.StringVar(value=AVG_SAMPLES_DEFAULT)  # fixed-count averaging
entry_avg_samples = ttk.Entry(left, textvariable=avg_samples_var, width=12); entry_avg_samples.grid(row=20, column=1, sticky="ew")

# Averaging mode: by Samples (N) or by Time (= Wait)
avg_mode_var = tk.StringVar(value=AVG_MODE_DEFAULT)  # 'samples' or 'time' (see acquire_measurement)
frm_avg_mode = ttk.Frame(left); frm_avg_mode.grid(row=21, column=0, columnspan=2, sticky="w")
ttk.Label(frm_avg_mode, text="Avg mode:").grid(row=0, column=0, sticky="w")
ttk.Radiobutton(frm_avg_mode, text="Samples", value="samples", variable=avg_mode_var).grid(row=0, column=1, sticky="w")
ttk.Radiobutton(frm_avg_mode, text="Time = Wait", value="time", variable=avg_mode_var).grid(row=0, column=2, sticky="w")

# Avg fraction (%) and Pre-settle (ms)
ttk.Label(left, text="Avg fraction (%)").grid(row=22, column=0, sticky="w")
avg_fraction_var = tk.StringVar(value=AVG_FRACTION_PCT_DEFAULT)  # % of dwell in 'time' mode
entry_avg_fraction = ttk.Entry(left, textvariable=avg_fraction_var, width=12); entry_avg_fraction.grid(row=22, column=1, sticky="ew")

ttk.Label(left, text="Pre-settle (ms)").grid(row=23, column=0, sticky="w")
presettle_ms_var = tk.StringVar(value=PRESETTLE_MS_DEFAULT)  # settle delay after move (ms)
entry_presettle_ms = ttk.Entry(left, textvariable=presettle_ms_var, width=12); entry_presettle_ms.grid(row=23, column=1, sticky="ew")
ttk.Label(left, text="Current λ (nm)").grid(row=9, column=0, sticky="w")
entry_current = ttk.Entry(left, width=12); entry_current.grid(row=9, column=1, sticky="ew")
entry_current.insert(0, f"{state.get('current_nm', 500.0):.3f}")

# Scan controls
btn_scan = ttk.Button(left, text="Start Scan", command=scan_action); btn_scan.grid(row=10, column=0, columnspan=2, sticky="ew", pady=(8,0))
btn_pause = ttk.Button(left, text="Pause", command=pause_action); btn_pause.grid(row=11, column=0, sticky="ew")
btn_resume = ttk.Button(left, text="Resume", command=resume_action); btn_resume.grid(row=11, column=1, sticky="ew")
btn_stop = ttk.Button(left, text="Stop", command=stop_action); btn_stop.grid(row=12, column=0, columnspan=2, sticky="ew", pady=(0,8))

# Jog controls
ttk.Label(left, text="Jog step (nm)").grid(row=13, column=0, sticky="w", pady=(8,0))
jog_step_var = tk.StringVar(value="0.5")
entry_jog_step = ttk.Entry(left, textvariable=jog_step_var, width=12); entry_jog_step.grid(row=13, column=1, sticky="ew", pady=(8,0))
btn_jog_minus = ttk.Button(left, text="Jog −", command=jog_minus_action); btn_jog_minus.grid(row=14, column=0, sticky="ew")
btn_jog_plus  = ttk.Button(left, text="Jog +", command=jog_plus_action);  btn_jog_plus.grid(row=14, column=1, sticky="ew")
btn_mark_point = ttk.Button(left, text="Mark Point", command=mark_point_action); btn_mark_point.grid(row=15, column=0, columnspan=2, sticky="ew")

# Multi-scan queue
ttk.Label(left, text="Scan Queue").grid(row=16, column=0, columnspan=2, sticky="w", pady=(12,0))
queue_listbox = tk.Listbox(left, height=5); queue_listbox.grid(row=17, column=0, columnspan=2, sticky="nsew")
btn_add_queue = ttk.Button(left, text="Add from fields", command=add_scan_to_queue); btn_add_queue.grid(row=18, column=0, sticky="ew", pady=(4,0))
btn_remove_queue = ttk.Button(left, text="Remove selected", command=remove_selected_scan); btn_remove_queue.grid(row=18, column=1, sticky="ew", pady=(4,0))
btn_run_queue = ttk.Button(left, text="Run queue", command=run_scan_queue_action); btn_run_queue.grid(row=19, column=0, sticky="ew")
btn_clear_queue = ttk.Button(left, text="Clear queue", command=clear_scan_queue); btn_clear_queue.grid(row=19, column=1, sticky="ew")


# Clear plot
btn_clear_plot = ttk.Button(left, text="Clear Plot", command=clear_plot_action); btn_clear_plot.grid(row=24, column=0, columnspan=2, sticky="ew", pady=(8,0))

# ---- Backlash / Slip compensation controls ----
# Workflow: click "Referenzfahrt" once per session (after Connect, before the
# first Goto/Scan) to establish a known direction; from then on every
# direction reversal automatically gets the nm value below added. See the
# tunables block at the top of the file and reversal_compensation_steps().
# The nm VALUE persists across restarts (settings JSON); the direction
# state does NOT (see SETTINGS PERSISTENCE comment at the top of the file)
# -- that's why Referenzfahrt still has to be repeated every session.
ttk.Separator(left).grid(row=25, column=0, columnspan=2, sticky="ew", pady=(8,4))
ttk.Label(left, text="Slip / Backlash (nm)").grid(row=26, column=0, sticky="w")
_saved_settings = app_config.load()
slip_nm_var = tk.StringVar(value=str(_saved_settings.get("backlash_slip_nm", 0.30)))
entry_slip_nm = ttk.Entry(left, textvariable=slip_nm_var, width=12); entry_slip_nm.grid(row=26, column=1, sticky="ew")

def _on_slip_nm_edited(_event=None):
    """Persist the slip value as soon as the field is confirmed (Enter) or
    loses focus, so it survives a program restart."""
    try:
        val = max(0.0, float(slip_nm_var.get()))
        app_config.save(backlash_slip_nm=val)
        log(f"[SETTINGS] Slip saved: {val:.3f} nm")
    except Exception:
        pass  # invalid entry (e.g. mid-edit) -- don't save, don't crash

entry_slip_nm.bind("<FocusOut>", _on_slip_nm_edited)
entry_slip_nm.bind("<Return>", _on_slip_nm_edited)

btn_reference_run = ttk.Button(left, text="Referenzfahrt", command=reference_run_action)
btn_reference_run.grid(row=27, column=0, columnspan=2, sticky="ew", pady=(4,0))
lbl_direction_state = tk.Label(left, text="Richtung: unbekannt \u2013 Referenzfahrt n\u00f6tig", anchor="w")
lbl_direction_state.grid(row=28, column=0, columnspan=2, sticky="ew", pady=(2,0))

from matplotlib.figure import Figure
fig = Figure(figsize=(5,4), dpi=100)
ax = fig.add_subplot(111)
ax.set_xlabel("Wavelength (nm)"); ax.set_ylabel("PMT Voltage (V)")
line, = ax.plot([], [])
canvas = FigureCanvasTkAgg(fig, master=right)
canvas.get_tk_widget().pack(fill="both", expand=True)
# Plot manager for multi-scan + jog
plot_mgr = PlotManager(root, ax, canvas)

# === MOVED: CSV helper + export functions placed before mainloop ===
# ---- CSV Export helpers ----







# Console
console = scrolledtext.ScrolledText(right, height=10, state="disabled")
console.pack(fill="x", side="bottom")
console.tag_config("info", foreground="#111")
console.tag_config("warn", foreground="#b58900")
console.tag_config("error", foreground="#dc322f")

# Register the console as the log sink IMMEDIATELY (before the initial state
# calls below), so even the first FSM/button message lands in the widget
# instead of on stdout. The rest of the wiring (vars/refs/hooks) follows in
# the block at the end of the file; a second set_log_sink there would be
# harmless but has been removed.
set_log_sink(_console_log_sink)

# Initial button states
set_buttons_connected(False)
set_fsm("IDLE")


def on_close():
    try:
        state["stop_flag"] = True
        if ser_ok():
            safe_stop()
            state["ser"].close()
    except Exception:
        print("[WARN] on_close: suppressed exception during cleanup")
    root.destroy()


# =====================================================================
# WIRING: modules <-> GUI (log sink, ui vars, refs, hooks)
# =====================================================================

# 1) The console was already registered as the log sink further up (right
#    after it was created), so the initial state logs land in the widget too.
#    Nothing more to do here.

# 2) Expose StringVars for motion/daq (ui_get).
for _n, _v in {
    'avg_mode_var': avg_mode_var, 'avg_samples_var': avg_samples_var,
    'avg_fraction_var': avg_fraction_var, 'presettle_ms_var': presettle_ms_var,
    'slip_nm_var': slip_nm_var, 'entry_wait': entry_wait,
}.items():
    register_var(_n, _v)

# 3) Expose widget objects for scan_engine (refs[...]).
refs.update({
    'entry_current': entry_current, 'entry_goto': entry_goto,
    'entry_start': entry_start, 'entry_end': entry_end,
    'entry_step': entry_step, 'entry_wait': entry_wait,
    'avg_mode_var': avg_mode_var, 'avg_samples_var': avg_samples_var,
    'jog_step_var': jog_step_var, 'autosave_csv_var': autosave_csv_var,
    'export_dir_var': export_dir_var, 'pattern_var': pattern_var,
    'slip_nm_var': slip_nm_var, 'queue_listbox': queue_listbox,
    'btn_add_queue': btn_add_queue, 'btn_clear_queue': btn_clear_queue,
    'btn_disconnect': btn_disconnect, 'btn_goto': btn_goto,
    'btn_pause': btn_pause, 'btn_remove_queue': btn_remove_queue,
    'btn_resume': btn_resume, 'btn_run_queue': btn_run_queue,
    'btn_scan': btn_scan, 'btn_stop': btn_stop,
    'messagebox': messagebox, 'plot_mgr': plot_mgr,
})

# 4) Register GUI hooks (called by motion/scan_engine via ui_hook).
register_hook('set_buttons_scanning', set_buttons_scanning)
register_hook('set_buttons_connected', set_buttons_connected)
register_hook('set_direction_label', lambda txt: lbl_direction_state.configure(text=txt))
register_hook('set_reference_button_enabled',
              lambda en: btn_reference_run.configure(state=("normal" if en else "disabled")))
register_hook('show_warning', lambda title, msg: messagebox.showwarning(title, msg))
register_hook('show_error', lambda title, msg: messagebox.showerror(title, msg))
register_hook('show_info', lambda title, msg: messagebox.showinfo(title, msg))
register_hook('ui_after', lambda fn: root.after(0, fn))
