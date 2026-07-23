"""
backlash_cal.py — Automatic slip/backlash calibration via a reference line.

WHAT THIS MODULE DOES
---------------------
Measure the monochromator's mechanical backlash without a ruler: with a
reference lamp (a known emission line, e.g. H-alpha 656.3 nm) the line is
crossed twice --

  * once UPWARD   (mechanism seated in the + direction)  -> peak at lambda_up
  * once DOWNWARD (mechanism seated in the - direction)  -> peak at lambda_down

The difference |lambda_down - lambda_up| IS the backlash: exactly the nm value
that reversal_compensation_steps() adds on each direction reversal. The result
can be written straight into the 'Slip / Backlash (nm)' field and persisted.

WHERE/WHEN
----------
Started from the Qt calibration dialog (gui_main): run_calibration(...) spawns a
worker thread and reports progress/result via callbacks. The dialog shows both
sweeps live and offers "Apply & Save".

WHY a separate module
---------------------
Reuses only the existing, tested primitives (protocol_faulhaber: send_cmd/
read_position/safe_stop/init_motor; motion: steps_for_delta_nm/wait_until_
position/timeout_for_target_steps; daq: acquire_measurement) -- the same building
blocks as scan_worker/goto_worker, just wired into a directional mini-scan. No
existing logic module is modified; the GUI only knows run_calibration + callbacks.

IMPORTANT: during the measurement the slip field is forced to 0 (measure raw),
otherwise the ACTIVE compensation would corrupt the result. The original value is
restored at the end (or overwritten by the result via 'Apply & Save').
"""

import threading

from app_context import state, log, set_fsm, refs
from protocol_faulhaber import ser_ok, send_cmd, read_position, safe_stop, init_motor
from motion import steps_for_delta_nm, wait_until_position, timeout_for_target_steps
from daq import acquire_measurement
from device_constants import SAFE_AFTER_MOVE


# =====================================================================
# Slip field: force to 0 (raw measure) and restore
# =====================================================================
def _read_slip_field() -> str:
    try:
        return refs['slip_nm_var'].get()
    except Exception:
        return "0"


def _set_slip_field(value_str: str):
    """Set the slip field (via the GUI's Tk-compatible adapter API)."""
    try:
        v = refs['slip_nm_var']
        # _EntryAdapter: delete/insert; _VarAdapter: set -- support both.
        if hasattr(v, "delete") and hasattr(v, "insert"):
            v.delete(0, "end")
            v.insert(0, value_str)
        elif hasattr(v, "set"):
            v.set(value_str)
    except Exception:
        pass


# =====================================================================
# Motion primitives (raw, no compensation -- slip field is 0)
# =====================================================================
def _move_rel_nm(delta_nm: float) -> bool:
    """Move by delta_nm relative (raw), wait for arrival, track state['current_nm'].
    Uses exactly the building blocks of scan_worker/goto_worker."""
    cur_steps = read_position()
    rel = steps_for_delta_nm(delta_nm)
    target = cur_steps + rel
    send_cmd(f"LR{rel}")
    send_cmd("M")
    ok = wait_until_position(target, timeout=timeout_for_target_steps(target, delta_nm))
    if SAFE_AFTER_MOVE:
        safe_stop()
    if ok:
        state["current_nm"] = float(state.get("current_nm", 0.0)) + float(delta_nm)
        # Keep the GUI 'Current λ' field in sync -- scan_engine reads THIS
        # field (not state['current_nm']) as the source of truth for the next
        # goto/scan delta calculation (refs['entry_current'].get() or
        # state['current_nm']). Without this, every calibration sweep leaves
        # entry_current stale, and the NEXT Goto/Scan after closing the
        # calibration dialog silently targets the wrong absolute wavelength
        # (off by whatever net distance the calibration moved). Same pattern
        # as scan_engine.py's goto/scan/jog moves.
        try:
            refs['entry_current'].delete(0, "end")
            refs['entry_current'].insert(0, f"{state['current_nm']:.3f}")
        except Exception:
            pass
    return ok


def _move_to_nm(target_nm: float) -> bool:
    """Move to an absolute (software-tracked) wavelength."""
    delta = float(target_nm) - float(state.get("current_nm", target_nm))
    if abs(delta) < 1e-9:
        return True
    return _move_rel_nm(delta)


# =====================================================================
# Peak location (centroid of the points above a threshold)
# =====================================================================
def _centroid(points, thresh_frac: float = 0.3):
    """Peak position (nm) as the intensity-weighted centroid of the points whose
    signal is above (base + thresh_frac*(max-base)). More robust than a plain
    argmax (subpixel, less noise-sensitive). None if no peak."""
    if not points:
        return None
    ys = [v for _, v in points]
    base, mx = min(ys), max(ys)
    if mx - base <= 0:
        return None
    thr = base + thresh_frac * (mx - base)
    num = den = 0.0
    for nm, v in points:
        if v >= thr:
            w = v - base
            num += nm * w
            den += w
    if den <= 0:
        return max(points, key=lambda p: p[1])[0]
    return num / den


# =====================================================================
# One directional sweep across the reference line
# =====================================================================
def _sweep(direction: int, center_nm: float, span_nm: float, step_nm: float,
           dwell_s: float, seat_nm: float, on_point, on_progress):
    """Cross the reference line in 'direction' (+1/-1) and collect (nm, V).

    Approach: first travel BEYOND the sweep start in the sweep direction
    (seat_nm), then move onto the start -- so the mechanism is guaranteed to be
    'seated' in the sweep direction before the line is crossed (this is the core
    of the measurement)."""
    d = 1 if direction >= 0 else -1
    start_nm = center_nm - d * span_nm      # +: below, -: above
    approach_nm = start_nm - d * seat_nm     # over-travel in sweep direction
    label = "up (+)" if d > 0 else "down (-)"

    if on_progress:
        on_progress(f"Seating {label}: approach {approach_nm:.3f} -> start {start_nm:.3f} nm")
    if not _move_to_nm(approach_nm):
        return None
    if state.get("stop_flag"):
        return None
    if not _move_to_nm(start_nm):
        return None

    pts = []
    n_steps = max(1, int(round((2.0 * span_nm) / step_nm)))
    nm = start_nm
    for i in range(n_steps + 1):
        if state.get("stop_flag"):
            if on_progress:
                on_progress("Aborted.")
            return None
        v = acquire_measurement(dwell_s)
        pts.append((nm, v))
        if on_point:
            on_point(d, nm, v)
        if on_progress:
            on_progress(f"Sweep {label}: {i+1}/{n_steps+1}  lambda={nm:.3f}  V={v:.4f}")
        if i < n_steps:
            if not _move_rel_nm(d * step_nm):
                return None
            nm = nm + d * step_nm
    return pts


# =====================================================================
# Public API
# =====================================================================
def run_calibration(center_nm, span_nm, step_nm, dwell_s, seat_nm=None,
                    on_point=None, on_progress=None, on_finished=None):
    """Start backlash calibration in the background.

    Parameters:
      center_nm : wavelength of the reference line (e.g. 656.3 for H-alpha)
      span_nm   : half sweep width; the line is crossed over center +/- span
      step_nm   : step size
      dwell_s   : dwell per point (uses the GUI averaging settings)
      seat_nm   : over-travel to 'seat' the mechanism (default: max(1 nm, span))
    Callbacks (called from the WORKER THREAD -- the GUI marshals them onto the
    GUI thread itself):
      on_point(direction, nm, v)      per measured point (live plot)
      on_progress(text)               status line
      on_finished(result | None)      result = dict(up_peak, down_peak, slip_nm,
                                      up_points, down_points); None on abort/error

    Returns: the started threading.Thread (daemon).
    """
    center_nm = float(center_nm)
    span_nm = max(1e-3, float(span_nm))
    step_nm = max(1e-3, float(step_nm))
    dwell_s = max(0.0, float(dwell_s))
    seat = float(seat_nm) if seat_nm is not None else max(1.0, span_nm)

    def worker():
        if not ser_ok():
            log("[CAL] Not connected.", "error")
            if on_finished:
                on_finished(None)
            return

        # Lockout + raw measurement (slip field to 0)
        prev_slip = _read_slip_field()
        prev_scanning = state.get("is_scanning", False)
        state["is_scanning"] = True
        state["stop_flag"] = False
        _set_slip_field("0")
        set_fsm("SCAN")
        log(f"[CAL] Backlash calibration around {center_nm:.3f} nm "
            f"(+/-{span_nm:.3f}, step {step_nm:.3f}, dwell {dwell_s:.2f}s). "
            f"Slip forced to 0 for raw measurement.")

        result = None
        try:
            init_motor()
            up = _sweep(+1, center_nm, span_nm, step_nm, dwell_s, seat, on_point, on_progress)
            if up is None or state.get("stop_flag"):
                raise RuntimeError("aborted during up-sweep")
            down = _sweep(-1, center_nm, span_nm, step_nm, dwell_s, seat, on_point, on_progress)
            if down is None or state.get("stop_flag"):
                raise RuntimeError("aborted during down-sweep")

            up_peak = _centroid(up)
            down_peak = _centroid(down)
            if up_peak is None or down_peak is None:
                raise RuntimeError("no peak found (reference lamp on? correct line?)")

            slip = abs(down_peak - up_peak)
            log(f"[CAL] up-peak {up_peak:.4f} nm | down-peak {down_peak:.4f} nm "
                f"| measured backlash = {slip:.4f} nm")
            result = {
                "up_peak": up_peak, "down_peak": down_peak, "slip_nm": slip,
                "up_points": up, "down_points": down,
            }
        except Exception as e:
            log(f"[CAL] Calibration ended: {e}", "warn")
            result = None
        finally:
            # Restore the slip field (the dialog may overwrite it with the
            # result via 'Apply & Save'); release the lockout.
            _set_slip_field(prev_slip)
            if ser_ok():
                safe_stop()
            state["is_scanning"] = prev_scanning
            set_fsm("IDLE")
            if on_finished:
                on_finished(result)

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    return t


def abort_calibration():
    """Abort a running calibration (sets the shared stop_flag; the sweep stops
    at the next point/move)."""
    state["stop_flag"] = True
