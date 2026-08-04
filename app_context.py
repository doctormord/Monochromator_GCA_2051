"""
app_context.py — Shared runtime hub for the VRS41 controller.

WHY THIS FILE EXISTS
----------------------------
When splitting the former monolith into individual modules, there are three
things that practically ALL modules need and which therefore must NOT live in
a single specialized module (otherwise circular-import chaos):

  1. state   — the one, shared, mutable runtime dict (serial handle, FSM
               state, scan flags, backlash direction, ...).
  2. log     — logging. The hardware/science modules want to log but are NOT
               ALLOWED to import tkinter. Solution: they call log() here; the
               GUI registers a sink via set_log_sink() that writes into the
               console widget. Without a registered sink, output goes to
               stdout (handy for tests without a GUI).
  3. ui      — a registry the GUI populates with its tk.StringVar objects and
               selected widgets/callbacks. Logic modules read GUI values via
               ui_get()/ui_var() or trigger GUI actions via ui_hook(),
               WITHOUT knowing the widgets directly. This is the decoupling
               mechanism that lets motion.py/daq.py stay free of a tkinter
               import.

In the later Qt6 lift (Stage C) the ui registry will be replaced by Qt
signals; state/log remain usable. Until then this hub is the deliberately
thin bridge.

IMPORTANT (doc policy, see HANDOVER.md): these docstrings are preserved.
"""

import threading


# =====================================================================
# 1) SHARED STATE
# =====================================================================
# The one, project-wide shared runtime dict. Every module imports EXACTLY
# this object (from app_context import state) — there is only one instance.
# Keys/meaning as in the former monolith, plus 'last_move_direction' for the
# backlash compensation.
state = {
    'limit_blocked': False,
    "scan_queue": [],

    "ser": None,               # pyserial handle or None
    "connected": False,
    "is_scanning": False,
    # True for the duration of a single GoTo (goto_wavelength_action /
    # goto_worker), including its auto-recover retry after a Stop. Mirrors
    # is_scanning's role: stop_action()'s _stop_worker waits on
    # (is_scanning OR is_moving) before calling recover_after_stop(), so a
    # Stop pressed during a GoTo no longer re-arms the drive (clearing
    # stop_flag) while goto_worker's own wait_until_position() poll is still
    # running -- that race meant stop_flag could be cleared before
    # goto_worker's poll ever saw it True, so the GoTo just sat there until
    # its full move timeout (up to MAX_MOVE_TIMEOUT = 900 s) elapsed instead
    # of aborting promptly. Confirmed via a live thread-stack dump in SIM
    # (sim_defect_probe.py): goto_worker was still parked inside
    # wait_until_position() 18+ s after Stop was pressed and "recovered"
    # had already been logged.
    "is_moving": False,
    "is_paused": False,
    "stop_flag": False,
    "current_nm": 500.0,       # tracked by the user; the device reports no nm
    "fsm": "IDLE",
    "paused_mid_move": False,
    "pause_requested": False,
    "current_step_target_abs": None,

    # Backlash/slip bookkeeping (see device_constants.py + motion.py).
    # None = unknown -> run "Reference Run" before relying on the
    # compensation. +1/-1 once known -> the direction the mechanism is
    # currently seated in.
    "last_move_direction": None,
}

# Thread-safe access to the serial interface. Lives in state so that
# protocol_faulhaber and motion use the same lock.
state['lock'] = threading.Lock()


# =====================================================================
# 2) LOGGING SINK
# =====================================================================
_log_sink = None  # set by the GUI via set_log_sink()


def set_log_sink(fn):
    """Called by the GUI: registers a function fn(msg, tag) that writes log
    lines into the console widget. Before that (and in tests without a GUI),
    logging goes to stdout."""
    global _log_sink
    _log_sink = fn


def log(msg, tag="info"):
    """Project-wide log function. All modules call THIS instead of writing to
    a widget directly. Delegates to the sink registered by the GUI; without a
    sink -> stdout (so hardware/science modules stay runnable and testable
    without a GUI)."""
    if _log_sink is not None:
        try:
            _log_sink(msg, tag)
            return
        except Exception:
            pass  # sink is broken -> fall back to stdout, never crash
    print(f"[{tag}] {msg}")


# =====================================================================
# 3) UI REGISTRY (decoupling logic <-> GUI)
# =====================================================================
# The GUI registers here:
#   - its tk.StringVar objects (under names like 'avg_mode_var'), so logic
#     modules can read their current value without knowing the widget ->
#     ui_var()/ui_get().
#   - named callback functions (under names like 'set_current_nm'), so logic
#     modules can trigger GUI actions -> ui_hook().
ui = {
    "vars": {},    # name -> tk.StringVar (or any object with .get())
    "hooks": {},   # name -> callable
}

# Shared object references the GUI registers during setup, needed directly by
# the orchestration (scan_engine): the concrete widget objects (entry_*,
# btn_*, queue_listbox), the PlotManager instance (under 'plot_mgr') and the
# messagebox module (under 'messagebox'). scan_engine calls duck-typed
# .get()/.configure()/.insert()/... on them WITHOUT importing tkinter -- the
# concrete objects come exclusively from gui_main. In the Qt6 lift (Stage C)
# this will be replaced by signals/real methods.
refs = {}


def register_var(name, var):
    """GUI: make a tk.StringVar (or similar) available under 'name'."""
    ui["vars"][name] = var


def register_hook(name, fn):
    """GUI: make a named callback available under 'name'."""
    ui["hooks"][name] = fn


def ui_var(name):
    """Return the registered var object for 'name', or None."""
    return ui["vars"].get(name)


def ui_get(name, default=None):
    """Read the current value of the registered var 'name' as a string; if
    not registered or reading fails -> default. This keeps logic modules
    runnable even when no GUI is (yet) registered (tests)."""
    v = ui["vars"].get(name)
    if v is None:
        return default
    try:
        return v.get()
    except Exception:
        return default


def ui_hook(name, *args, **kwargs):
    """Call the registered GUI callback 'name' if present. If it's missing
    (e.g. in a test without a GUI) or raises, it is silently ignored — a GUI
    update must never crash a measurement. Returns: the callback's result, or
    None."""
    fn = ui["hooks"].get(name)
    if fn is None:
        return None
    try:
        return fn(*args, **kwargs)
    except Exception:
        return None


# =====================================================================
# 4) FSM (Finite State Machine) — central state management
# =====================================================================
# Lives here (not in motion.py) because the FSM is pure state+log logic and
# is needed both by protocol_faulhaber (recover_after_stop) and by motion and
# scan_engine -- this avoids a circular import.
FSM_STATES = {"IDLE", "MOVING", "SCAN", "PAUSED", "ERROR"}


def set_fsm(next_state: str):
    """Switch the FSM state and log the transition. Invalid states are
    rejected (logged, no switch). Called from all move/scan/recover paths."""
    if next_state not in FSM_STATES:
        log(f"[FSM] Invalid state: {next_state}", "warn")
        return
    prev = state.get("fsm", "IDLE")
    state["fsm"] = next_state
    log(f"[FSM] {prev} -> {next_state}")
