"""
scan_engine.py — Orchestration: goto, scan, pause/resume, jog, queue, CSV.

WHAT THIS MODULE DOES
---------------------
The actual user actions and their worker threads: driving to a wavelength
(goto), starting/pausing/resuming/stopping a scan, jog steps, marking a single
point, working through the scan queue, and CSV export. Calls the foundation
(protocol/motion/daq) for the hardware and computation work, and threads the
backlash compensation into every move (motion.reversal_compensation_steps).

GUI COUPLING (Tkinter interim state)
------------------------------------
This module imports NO tkinter. GUI values (entry fields, StringVars) and GUI
objects (buttons, listbox, refs['messagebox'], the PlotManager instance) come
exclusively from app_context.refs -- a dict that gui_main populates during
setup. Access happens as refs['name'].get()/.configure()/... (duck-typed).
Button batch states and the queue-finished callback go through ui_hook.

This is deliberately the Tkinter interim state: the widget names are still
known here (via refs). In the Qt6 lift (Stage C), refs will be replaced by
real signals/methods; the worker logic here then stays largely the same.

Origin: extracted from the monolith (function bodies unchanged; only widget
access switched to refs[...], "end" -> "end", set_buttons_* and root.after ->
ui_hook). Dead import-time code (the old, never-triggered queue-save block)
was NOT carried over -- it ran into a no-op once on import
(_queue_finished_prompt always returns False).

IMPORTANT (doc policy, see HANDOVER.md): docstrings/comments are preserved.
"""

import os
import csv
import time
import uuid
import datetime
import threading

from app_context import state, log, set_fsm, refs, ui_hook, ui_get
from device_constants import (
    STEPS_PER_NM, SAFE_AFTER_MOVE, SCAN_EPSILON_NM,
    POS_TOL_STEPS, POS_STABLE_COUNT, RAMP_SP, MAX_MOVE_TIMEOUT,
    REANCHOR_LOG_TOL_NM,
    STOP_JOIN_TIMEOUT_S,
)
from protocol_faulhaber import (
    ser_ok, send_cmd, read_position, read_position_or_none, read_position_raw_or_none,
    safe_stop, init_motor,
    recover_after_stop, configure_ramp,
)
from motion import (
    nm_to_steps, steps_for_delta_nm, steps_to_delta_nm, wait_until_position,
    timeout_for_target_steps, reversal_compensation_steps, postmove_limit_audit,
)
from daq import (
    acquire_measurement, read_pmt_voltage, read_pmt_voltage_avg, sleep_with_pause,
    _get_avg_fraction, _get_presettle_sec, daq_source_label,
)


def _float_or_none(val):
    """Small parsing helper: float(val), or None on invalid input.
    (From the monolith; used by the validate_* functions.)"""
    try:
        return float(val)
    except Exception:
        return None

# Short helper: the PlotManager instance (registered into refs by gui_main).
# As a function instead of a module global, so a value set later is visible.
def _plot():
    return refs.get('plot_mgr')


def _encoder_nm_or_none(anchor_steps, anchor_nm, slack_steps=0):
    """Convert the CURRENT encoder position into a wavelength, or return None
    if the POS read failed.

    WHAT/WHY: every re-anchor site in this file (goto success, goto retry,
    interrupted scan step, normal scan end, interrupted resume step) writes
    its result into state['current_nm'] / refs['entry_current'] -- the origin
    every later move derives its delta from, and the value app_config
    persists as last_current_nm. read_position() must NOT be used for that:
    it returns a silent 0 on a failed or garbled reply (see
    protocol_faulhaber.read_position, and the read_position_or_none docstring
    right next to it, which spells out exactly this hazard for callers that
    use the raw value directly). A fabricated 0 becomes a wavelength off by
    steps_to_delta_nm(anchor) -- tens of nm once POS has run up during a
    session, because POS is a free-running absolute counter -- and that
    poisoned origin is then inherited by every subsequent move.
    Returning None instead lets each caller keep the estimate it already had.

    WHEN: called from the re-anchor paths listed above, always right after the
    motor has come to rest.

    anchor_steps/anchor_nm: a (steps, nm) pair known to be consistent with
    each other. slack_steps: backlash-compensation steps to EXCLUDE from the
    result -- they turn the motor without moving the grating, so they are not
    wavelength travel (same reasoning as free_run_worker's nm_anchor_steps).
    """
    pos = read_position_or_none()
    if pos is None:
        return None
    return anchor_nm + steps_to_delta_nm(pos - anchor_steps - slack_steps)


def _slack_consumed_steps(travelled_steps, comp_steps):
    """How much of a move's backlash compensation was actually taken up, given
    that the move stopped after `travelled_steps` instead of completing.

    THE MODEL, which the rest of this codebase already assumes: on a direction
    reversal the drive train has play, and the FIRST comp_steps of travel turn
    the motor without moving the grating -- that is the entire reason
    reversal_compensation_steps() adds them to the commanded move in the first
    place, and it is exactly what sim_hardware._apply_move() implements
    (encoder advances by the full delta, optics lag by up to one backlash
    width, slack re-taken-up from zero after each reversal).

    WHY THIS EXISTS: the partial-move re-anchors (interrupted Goto, interrupted
    scan step, interrupted resume step) convert a raw encoder delta into a
    wavelength. Treating ALL of that delta as optical travel overstates the
    wavelength change by up to one full slip (~0.09 nm here) whenever the
    interrupted move happened to be a reversal. The worst case is a Stop very
    early in such a move: the grating has not moved at all, yet the label
    would jump by the whole compensation. Slack is consumed FIRST and
    deterministically, so the split is known, not guessable.

    RESIDUAL UNCERTAINTY, stated plainly: comp_steps reflects the CONFIGURED
    slip, not the true mechanical backlash. If the slip is miscalibrated this
    correction is wrong by (configured - real) -- but that is strictly smaller
    than the (configured - 0) error of not correcting at all, which is the
    premise the whole compensation scheme already rests on.

    Returns steps with the same sign as comp_steps (0 if nothing applies).
    """
    if not comp_steps or not travelled_steps:
        return 0
    # Travel opposing the compensation means the model does not apply (the
    # drive did not go where this compensation was aimed) -- claim nothing.
    if (travelled_steps >= 0) != (comp_steps >= 0):
        return 0
    if abs(travelled_steps) >= abs(comp_steps):
        return comp_steps          # slack fully taken up, rest was optical
    return travelled_steps         # stopped inside the play: nothing optical yet


def validate_goto_input():
    try:
        tgt = _float_or_none(refs['entry_goto'].get())
    except Exception:
        tgt = None
    if tgt is None:
        log("[ERROR] GoTo: invalid target wavelength.", "error")
        return None
    # Optional: software limits
    try:
        min_nm = float(state.get("soft_min_nm", "-inf"))
        max_nm = float(state.get("soft_max_nm", "inf"))
    except Exception:
        min_nm, max_nm = -float("inf"), float("inf")
    if not (min_nm <= tgt <= max_nm):
        log(f"[ERROR] GoTo: target {tgt} nm outside allowed range [{min_nm}, {max_nm}] nm.", "error")
        return None
    return tgt

def validate_scan_inputs():
    # Collect entry values
    try:
        s = _float_or_none(refs['entry_start'].get())
        e = _float_or_none(refs['entry_end'].get())
        stp = _float_or_none(refs['entry_step'].get())
        dwell = _float_or_none(refs['entry_wait'].get())
    except Exception:
        s = e = stp = dwell = None

    # Basic checks
    if s is None or e is None or stp is None:
        log("[ERROR] Scan: start/end/step must be numeric.", "error")
        return None
    if stp <= 0:
        log("[ERROR] Scan: step must be > 0.", "error")
        return None
    # Allow descending scans (end < start); direction handled later.
    if dwell is None or dwell < 0:
        log("[WARN] Scan: dwell invalid; using 0 s.", "warn")
        dwell = 0.0

    # Optional: software limits
    try:
        min_nm = float(state.get("soft_min_nm", "-inf"))
        max_nm = float(state.get("soft_max_nm", "inf"))
    except Exception:
        min_nm, max_nm = -float("inf"), float("inf")
    if not (min_nm <= s <= max_nm) or not (min_nm <= e <= max_nm):
        log(f"[ERROR] Scan: range [{s}, {e}] nm outside allowed [{min_nm}, {max_nm}] nm.", "error")
        return None

    return {"start": s, "end": e, "step": stp, "dwell": dwell}

def validate_free_run_inputs():
    """Same range-checking as validate_scan_inputs(), but only start/end are
    meaningful for a free-run (continuous) scan -- step/dwell don't apply,
    there is no per-point loop to space or dwell on."""
    try:
        s = _float_or_none(refs['entry_start'].get())
        e = _float_or_none(refs['entry_end'].get())
    except Exception:
        s = e = None
    if s is None or e is None:
        log("[ERROR] Free Run: start/end must be numeric.", "error")
        return None
    if abs(e - s) < 1e-9:
        log("[ERROR] Free Run: start and end are identical.", "error")
        return None
    try:
        min_nm = float(state.get("soft_min_nm", "-inf"))
        max_nm = float(state.get("soft_max_nm", "inf"))
    except Exception:
        min_nm, max_nm = -float("inf"), float("inf")
    if not (min_nm <= s <= max_nm) or not (min_nm <= e <= max_nm):
        log(f"[ERROR] Free Run: range [{s}, {e}] nm outside allowed [{min_nm}, {max_nm}] nm.", "error")
        return None

    # Speed override (SP, rpm). Hard-capped at RAMP_SP here too -- NOT just
    # trusting the GUI's own clamp (gui_main._on_free_run_sp_edited) -- this
    # is the actual enforcement point closest to where the value gets sent
    # to the drive. RAMP_SP is the rig-confirmed normal speed
    # (device_constants.py, tune_ramp.py-verified); this can only reduce it,
    # never exceed it, no matter what ends up in the field.
    try:
        sp = int(float(refs['free_run_sp_var'].get()))
    except Exception:
        sp = RAMP_SP
    sp_clamped = max(1, min(sp, RAMP_SP))
    if sp_clamped != sp:
        log(f"[FREE RUN] Speed {sp} rpm exceeds RAMP_SP ({RAMP_SP}); clamped to {sp_clamped}.", "warn")

    return {"start": s, "end": e, "sp": sp_clamped}

def _queue_finished_prompt(*args, **kwargs):
    # Disabled at import-time; real prompt is scheduled after the queue ends.
    return False

def _maybe_prompt_save_all_scans_after_queue():
    """Show the 'Queue finished' save prompt only after the queue has actually finished."""
    # NOTE (carried over from the monolith): 'graph_data' is not defined
    # anywhere -> this line raises NameError, which is caught by the except,
    # have_scans stays False, and the function returns early. The save
    # prompt at queue-end is therefore EFFECTIVELY INACTIVE -- behaviour
    # preserved 1:1 as before, not blindly "fixed". See BACKLOG.
    try:
        have_scans = bool(graph_data.get("scans")) and len(graph_data.get("scans") or []) > 0
    except Exception:
        have_scans = False
    if not have_scans:
        return
    try:
        mb = refs.get('messagebox')
        if mb is not None and mb.askyesno("Queue finished", "Save all scans and queue summary?"):
            try:
                export_all_scans_and_summary()  # existing function in your file
            except Exception as e:
                log(f"[ERROR] Export failed: {e}", "error")
    except Exception as e:
        try:
            log(f"[WARN] Could not show save prompt: {e}", "warn")
        except Exception:
            pass

def goto_wavelength_action():
    if state["is_scanning"]:
        refs['messagebox'].showwarning("Busy", "Cannot GOTO while scanning.")
        return
    # Re-entrancy guard: a GoTo is not covered by is_scanning, so without this
    # check a double-click on Go To, or on a Jog button (jog_*_action IS a
    # GoTo, see CONTEXT.md), could spawn two goto_workers driving the same
    # axis from two different origins, and each worker clearing
    # state['is_moving'] independently would make that flag -- the signal
    # stop_action() waits on -- unreliable.
    if state.get("is_moving"):
        refs['messagebox'].showwarning("Busy", "A move is already in progress.")
        return
    if not ser_ok():
        refs['messagebox'].showerror("Error", "Not connected.")
        return
    try:
        tgt = validate_goto_input()
        if tgt is None: return
        target_nm = float(tgt)
    except Exception:
        return
        return

    def goto_worker():
        set_fsm("MOVING")
        try:
            # Lock controls during goto
            refs['btn_goto'].configure(state="disabled")
            refs['btn_disconnect'].configure(state="disabled")
            refs['btn_scan'].configure(state="disabled")
            refs['btn_free_run'].configure(state="disabled")
            refs['btn_pause'].configure(state="disabled")
            refs['btn_resume'].configure(state="disabled")
            refs['btn_stop'].configure(state="normal")

            init_motor()
            current_steps = read_position()
            current_nm_ui = float(refs['entry_current'].get() or state["current_nm"])
            # Anchor pair (steps, nm) captured ONCE, right here, before the
            # first move of this GoTo -- used both by the success re-anchor
            # just below and by the interrupted branch further down to
            # recover the TRUE current wavelength after a Stop instead of
            # re-reading the (never updated mid-move) entry_current field.
            # See the note in the `else:` branch below.
            anchor_steps = current_steps
            anchor_nm = current_nm_ui
            delta_nm = target_nm - current_nm_ui
            # Backlash compensation is slack take-up (assumed non-optical),
            # kept as its own variable (not folded silently into rel_steps)
            # so the post-move re-anchor below can shift the anchor by
            # EXACTLY this many steps and exclude it from the nm result --
            # same pattern as free_run_worker's nm_anchor_steps = start +
            # comp_steps (see the note there).
            comp = 0
            if abs(delta_nm) > 0:
                comp = reversal_compensation_steps(1 if delta_nm >= 0 else -1)
            rel_steps = steps_for_delta_nm(delta_nm) + comp
            # target absolute steps estimate:
            target_steps = current_steps + rel_steps
            state['current_step_target_abs'] = target_steps

            send_cmd(f"LR{rel_steps}")
            send_cmd("M")
            reached = wait_until_position(target_steps, timeout=timeout_for_target_steps(target_steps, delta_nm))
            if SAFE_AFTER_MOVE:
                safe_stop()
            if reached:
                # Re-anchor to the REAL position instead of blindly trusting
                # target_nm, mirroring free_run_worker's post-sweep
                # re-anchor. The anchor is shifted by `comp` first so the
                # slack-take-up steps are EXCLUDED from the nm result --
                # reading POS back without that shift would fold the
                # compensation in as if it were optical travel, which is not
                # what those steps are (see _slack_consumed_steps()). This is
                # deliberately NOT "add comp to the label": if the configured
                # slip exactly matches the real backlash, real_nm comes out
                # equal to target_nm (verified in SIM: sim_defect_probe.py);
                # if the slip is miscalibrated, this catches the mismatch on
                # every move instead of letting it accumulate silently.
                real_nm = _encoder_nm_or_none(anchor_steps, anchor_nm, slack_steps=comp)
                if real_nm is None:
                    # POS unreadable -- fall back to the commanded target
                    # instead of writing a fabricated position into the
                    # origin every later move derives from. See
                    # _encoder_nm_or_none().
                    log("[WARN] Could not read POS after move; keeping commanded "
                        f"target {target_nm:.3f} nm as the position estimate.", "warn")
                    real_nm = target_nm
                elif abs(real_nm - target_nm) > REANCHOR_LOG_TOL_NM:
                    log(f"[DONE] Re-anchoring to real position: target was "
                        f"{target_nm:.3f} nm, encoder-frame actual "
                        f"{real_nm:.3f} nm (comp-adjusted).")
                state["current_nm"] = real_nm
                refs['entry_current'].delete(0, "end"); refs['entry_current'].insert(0, f"{state['current_nm']:.3f}")
                if not postmove_limit_audit('goto'):
                    log("[STOP] Drive reported fault/limit after move; halting.", "warn")
                else:
                    log(f"[DONE] Reached {target_nm:.3f} nm")


            else:
                log("[STOP] Motion interrupted")
                # Capture WHY the move ended, BEFORE recover_after_stop()
                # below clears state['stop_flag'] as part of re-arming.
                # wait_until_position() returns False both for a user Stop
                # and for a timeout/fault, and the auto-recover retry below
                # cannot tell them apart on its own -- without capturing this
                # flag first, a Stop during a GoTo would abort the move and
                # then immediately drive to the original target anyway
                # (confirmed reproducible in SIM, sim_defect_probe.py).
                # Retrying is right after a transient fault; it is never
                # right after the operator asked for a stop.
                user_stop = bool(state.get("stop_flag"))
                if user_stop:
                    # Do NOT re-arm here: stop_action()'s own background worker
                    # owns recover_after_stop() in this case and is already
                    # waiting for state['is_moving'] to clear (see there).
                    # Calling it a second time from here would clear stop_flag
                    # underneath that worker. Reading POS needs no re-arm --
                    # safe_stop() above already left the link usable.
                    current_steps = read_position_or_none()
                else:
                    # Auto-recover once and retry the goto
                    recover_after_stop()
                    # recover_after_stop() unconditionally sets fsm to IDLE as
                    # part of its normal re-arm sequence (protocol_faulhaber.py).
                    # That's correct when IT is what ends a move, but here
                    # goto_worker calls it mid-retry and then immediately
                    # starts a SECOND real move -- without setting fsm back to
                    # MOVING, fsm would report IDLE while the retry is still
                    # silently in progress (state['is_moving'] stays True
                    # throughout -- it's only cleared in this worker's own
                    # finally block -- but fsm is the signal both the GUI and
                    # test/probe code actually poll).
                    set_fsm("MOVING")
                    init_motor()
                    current_steps = read_position_or_none()
                # current_nm_ui must NOT be re-read from refs['entry_current']
                # here: that field is never updated while a GoTo is in flight
                # (only on success, in the `if reached:` branch above), so
                # after an interrupted move it would still hold the
                # wavelength from BEFORE this GoTo started. Computing delta_nm
                # from that stale value against the freshly read
                # current_steps (which DOES reflect the partial move) would
                # size the retry's relative move for the pre-move position
                # and overshoot the target by exactly the distance already
                # travelled before the Stop.
                # Instead, derive the TRUE current wavelength from the
                # freshly read encoder position via the anchor captured at
                # the top of this worker (anchor_steps/anchor_nm) -- the same
                # anchored-delta pattern free_run_worker and scan_worker's
                # [DIAG] logging use elsewhere in this file. Re-anchor the
                # label immediately (not just on eventual retry success), so
                # it reflects reality even if the retry itself fails too.
                # Comp-adjusted by the part of the compensation that was
                # actually consumed: slack is taken up first, so an
                # interrupted reversal has NOT moved the grating by the full
                # encoder delta (see _slack_consumed_steps()).
                # current_steps is None if the POS read failed -- keep the
                # label untouched rather than writing a fabricated position
                # (see _encoder_nm_or_none() for why that matters here).
                if current_steps is None:
                    log("[WARN] Could not read POS after interrupted move; "
                        "position estimate left unchanged and possibly stale.", "warn")
                    current_nm_ui = float(state.get("current_nm", anchor_nm))
                else:
                    _travelled = current_steps - anchor_steps
                    _consumed = _slack_consumed_steps(_travelled, comp)
                    current_nm_ui = anchor_nm + steps_to_delta_nm(_travelled - _consumed)
                    if abs(current_nm_ui - state.get("current_nm", current_nm_ui)) > 1e-6:
                        log(f"[STOP] Re-anchoring to real position: "
                            f"{state.get('current_nm', current_nm_ui):.3f} -> "
                            f"{current_nm_ui:.3f} nm (read back from POS after interrupted move).")
                    state["current_nm"] = current_nm_ui
                    try:
                        refs['entry_current'].delete(0, "end"); refs['entry_current'].insert(0, f"{current_nm_ui:.3f}")
                    except Exception:
                        pass
                if user_stop:
                    # Operator asked for a stop: the label now reflects where
                    # the drive actually is, and that is all this worker does.
                    # No retry -- see the note where user_stop is captured.
                    log("[STOP] Goto aborted by operator -- not retrying.")
                    return
                if current_steps is None:
                    # A retry needs a trustworthy origin; without POS there is
                    # none, and a relative move from a guessed position is
                    # exactly the class of error this whole section fixes.
                    log("[ERROR] Goto: no usable POS after interrupted move; "
                        "not retrying. Re-anchor with 'Known λ here' before "
                        "the next move.", "error")
                    return
                # Fresh anchor for the RETRY's own success re-anchor below --
                # current_steps/current_nm_ui are consistent with each other
                # at this exact point (just established above).
                retry_anchor_steps = current_steps
                retry_anchor_nm = current_nm_ui
                delta_nm = target_nm - current_nm_ui
                comp = 0
                if abs(delta_nm) > 0:
                    comp = reversal_compensation_steps(1 if delta_nm >= 0 else -1)
                rel_steps = steps_for_delta_nm(delta_nm) + comp
                target_steps = current_steps + rel_steps
                state['current_step_target_abs'] = target_steps
                send_cmd(f"LR{rel_steps}")
                send_cmd("M")
                reached2 = wait_until_position(target_steps, timeout=timeout_for_target_steps(target_steps, abs(delta_nm)))
                if SAFE_AFTER_MOVE:
                    safe_stop()
                if reached2:
                    # Same comp-adjusted re-anchor as the first attempt's
                    # success path -- see the comment there.
                    real_nm = _encoder_nm_or_none(retry_anchor_steps, retry_anchor_nm,
                                                  slack_steps=comp)
                    if real_nm is None:
                        log("[WARN] Could not read POS after move; keeping commanded "
                            f"target {target_nm:.3f} nm as the position estimate.", "warn")
                        real_nm = target_nm
                    elif abs(real_nm - target_nm) > REANCHOR_LOG_TOL_NM:
                        log(f"[DONE] Re-anchoring to real position: target was "
                            f"{target_nm:.3f} nm, encoder-frame actual "
                            f"{real_nm:.3f} nm (comp-adjusted).")
                    state["current_nm"] = real_nm
                    try:
                        refs['entry_current'].delete(0, "end"); refs['entry_current'].insert(0, f"{state['current_nm']:.3f}")
                    except Exception:
                        pass
                    log(f"[DONE] Reached {target_nm:.3f} nm (after auto-recover)")
                else:
                    log("[ERROR] Goto failed after auto-recover.", "error")
        except Exception as e:
            log(f"[ERROR] goto_wavelength: {e}", "error")
            set_fsm("ERROR")
        finally:
            # Restore controls and FSM back to IDLE when done
            state['is_moving'] = False
            ui_hook('set_buttons_connected', state["connected"])
            if state.get("fsm") == "MOVING":
                set_fsm("IDLE")

    # Tells stop_action() a GoTo is in flight, the same way is_scanning tells
    # it a scan is in flight. See the state['is_moving'] comment in
    # app_context.py for the race this closes.
    # Set HERE, on the GUI thread, immediately before the thread is started --
    # NOT inside goto_worker. Two reasons: (1) the re-entrancy guard at the
    # top of this function then does its check-and-set without a window in
    # between (Qt serialises GUI callbacks on one thread, so no other GoTo can
    # slip through); (2) setting it inside the worker would leave a gap of one
    # thread start-up during which a Stop pressed in that gap would see
    # is_moving still False, so stop_action() would skip its wait and re-arm
    # the drive underneath a GoTo that was about to begin. Cleared in
    # goto_worker's finally block, which runs on every exit path.
    state['is_moving'] = True
    threading.Thread(target=goto_worker, daemon=True).start()

def scan_action():
    
    if state["is_scanning"]:
        refs['messagebox'].showwarning("Busy", "Scan already running.")
        return
    if not ser_ok():
        refs['messagebox'].showerror("Error", "Not connected.")
        return
    _cfg = validate_scan_inputs()
    if _cfg is None:
        return
    s_nm = _cfg["start"]; e_nm = _cfg["end"]; st_nm = _cfg["step"]; dwell = _cfg["dwell"]
    state['planned_params'] = {'s_nm': s_nm, 'e_nm': e_nm, 'st_nm': st_nm, 'dwell': dwell}


    direction = 1 if e_nm >= s_nm else -1
    state['planned_params']['direction'] = (1 if e_nm >= s_nm else -1)
    state["is_scanning"] = True
    set_fsm("SCAN")
    ui_hook('set_buttons_scanning', True)
    state["is_paused"] = False
    state["stop_flag"] = False

    refs['btn_pause'].configure(state="normal")
    refs['btn_stop'].configure(state="normal")
    refs['btn_resume'].configure(state="disabled")

    def scan_worker():
        try:
            init_motor()
            # Move to start
            current_steps = read_position()
            current_nm_ui = float(refs['entry_current'].get() or state["current_nm"])
            # First jump: from current UI nm to start nm
            delta_first = s_nm - current_nm_ui
            rel_first = steps_for_delta_nm(delta_first)
            if abs(delta_first) > 0:
                rel_first += reversal_compensation_steps(1 if delta_first >= 0 else -1)
            target_first = current_steps + rel_first
            state['current_step_target_abs'] = target_first
            send_cmd(f"LR{rel_first}"); send_cmd("M")
            if not wait_until_position(target_first, timeout=timeout_for_target_steps(target_first, delta_first)):
                safe_stop(); raise RuntimeError("Stopped before reaching start.")
            state["current_nm"] = s_nm
            refs['entry_current'].delete(0, "end"); refs['entry_current'].insert(0, f"{state['current_nm']:.3f}")
            if SAFE_AFTER_MOVE: safe_stop()

            # Prepare plotting via PlotManager
            # Legend/label text: same shape as the queue list, so a curve in
            # the plot can be matched to the queue entry that produced it.
            # Compact averaging token for the legend: "x8" = 8 samples,
            # "60%" = time mode at 60 % of the available window.
            _mode = ui_get('avg_mode_var', 'samples')
            if _mode == 'time':
                _avg = f"{_get_avg_fraction():g}%"
            else:
                try:
                    _avg = f"x{float(ui_get('avg_samples_var', '8')):g}"
                except Exception:
                    _avg = "x?"
            # SHORT. This string is the plot legend, which sits on top of the
            # data -- the full queue-style line (timebase, pre-settle, spelled
            # out averaging) was so wide it covered the traces it was meant to
            # identify. Everything omitted here is still in the log and in the
            # CSV header, which is where you look it up; the legend only has to
            # tell two curves apart.
            label = (f"{time.strftime('%H:%M:%S')} "
                     f"{s_nm:g}→{e_nm:g} Δ{st_nm:g} {_avg}")
            state['active_plot_id'] = _plot().start_new_scan(label, {'start': s_nm, 'end': e_nm, 'step': st_nm})

            pos_nm = s_nm
            rel_step_steps = steps_for_delta_nm(st_nm) * direction

            # ABSOLUTE STEP GRID: every target below is derived from a single
            # anchor position taken once at the scan start (grid_anchor_steps),
            # not from wherever the previous move happened to stop. This
            # matters because wait_until_position() accepts arrival within
            # POS_TOL_STEPS, and on this rig POS_TOL_STEPS (0.01 nm = 3617
            # steps) is exactly ONE 0.01 nm scan step wide -- a move may
            # legitimately stop a long way short and still count as arrived.
            # A pure relative chain (each step commanded from the position
            # measured right before it) would carry that shortfall forward
            # into every subsequent step instead of correcting it, producing
            # a cumulative drift that grows with step count rather than with
            # the number of reversals -- rig measurements: +22.5 pm/cycle over
            # 200 steps (~1.1 %) and +2.7 pm/cycle over a 20-step series, both
            # strictly monotonic and scaling with step count rather than the
            # (constant, 2-per-cycle) number of reversals, which rules out
            # backlash as the cause. Deriving every target from a fixed
            # anchor means a shortfall on one step is simply corrected by the
            # next move instead of accumulating.
            # Backlash compensation is accumulated separately
            # (grid_slack_steps): those steps do turn the motor and DO show up
            # in POS, but they take up slack instead of moving the grating, so
            # they shift the grid without being wavelength travel.
            grid_anchor_steps = read_position()
            grid_slack_steps = 0
            grid_index = 0
            # [DIAG] Scan-start snapshot -- diagnostic logging only, no
            # behaviour change. Records the state that decides whether THIS
            # scan's jump-to-start received backlash compensation, which is
            # what a rigid scan-to-scan wavelength offset would come from.
            # last_move_direction is mutated by every move in the program
            # (Goto, Jog -- which IS a Goto --, Reference Run), so a single
            # jog between two scans can flip it and give one scan a
            # compensation the previous one did not get.
            # Remove once the offset investigation is closed.
            try:
                log(f"[DIAG] scan-start anchor_pos={grid_anchor_steps} "
                    f"start_nm={s_nm:.4f} dir={direction:+d} "
                    f"last_move_direction={state.get('last_move_direction')} "
                    f"slip_nm={ui_get('slip_nm_var', '0')} "
                    f"src={daq_source_label()}")
            except Exception:
                pass
            while (direction > 0 and pos_nm <= e_nm + SCAN_EPSILON_NM) or (direction < 0 and pos_nm >= e_nm - SCAN_EPSILON_NM):
                if state["stop_flag"]:
                    safe_stop(); log("[STOP] Scan aborted"); break

                # Measure ONE averaged point at the current wavelength.
                # Pre-settle + averaging + residual dwell all happen inside
                # acquire_measurement(), which is the single source of truth
                # for how AVG/dwell settings apply to a point.
                v = acquire_measurement(dwell)
                if state.get('active_plot_id'):
                    _plot().append_scan_point(state['active_plot_id'], pos_nm, v)
                # [DIAG] POS read here, ahead of the move further down, and
                # reused below -- same value (nothing moves between the two
                # points) and the same number of serial round-trips, so this
                # costs nothing. It lets the wavelength LABEL and the ENCODER
                # be logged together for the point that was actually measured.
                # POS is a free-running absolute counter (init_motor and
                # recover_after_stop never home the drive), so pos= is directly
                # comparable ACROSS scans in one session -- that is what makes
                # a rigid group offset visible.
                curr_abs = read_position()
                log(f"{pos_nm:.3f} nm  |  {v:.5f} V")
                try:
                    _enc_nm = s_nm + steps_to_delta_nm(curr_abs - grid_anchor_steps)
                    log(f"[DIAG] i={grid_index} label_nm={pos_nm:.4f} "
                        f"pos={curr_abs} enc_nm={_enc_nm:.4f} "
                        f"lbl_minus_enc_nm={pos_nm - _enc_nm:+.4f} "
                        f"anchor={grid_anchor_steps} slack={grid_slack_steps} "
                        f"dir={state.get('last_move_direction')} "
                        f"reads={state.get('last_avg_reads')} "
                        f"win_ms={(state.get('last_avg_window_s') or 0)*1000:.1f} "
                        f"V={v:.5f}")
                except Exception:
                    pass

                # Next step
                # Stop checkpoint BEFORE issuing the move: acquire_measurement()
                # above honours stop_flag and can return early, but without
                # this check the loop would fall straight through into LR/M
                # anyway -- commanding the drive to make ANOTHER step right
                # after a Stop, which wait_until_position() would then abort,
                # producing a small unintended move after every Stop pressed
                # during a dwell.
                if state["stop_flag"]:
                    safe_stop(); log("[STOP] Scan aborted"); break
                pos_nm_next = pos_nm + st_nm*direction
                # curr_abs was read right after the measurement above (see
                # [DIAG] note) and is reused here -- nothing has moved since.
                # Snapshot of all CONFIRMED (already-completed) slack before
                # this iteration's own comp is added -- used by the
                # interrupted-branch re-anchor below, which can only exclude
                # slack it knows for certain was taken up (see the NOTE
                # there for why this iteration's own comp is NOT included).
                grid_slack_steps_before = grid_slack_steps
                # Backlash compensation on the iteration where the direction
                # actually reverses; a no-op every other iteration. It shifts
                # the grid (the steps show up in POS) without being optical
                # travel, so it is accumulated into grid_slack_steps.
                comp = reversal_compensation_steps(direction)
                grid_slack_steps += comp
                grid_index += 1
                # Target from the ABSOLUTE grid, not from wherever the previous
                # move happened to stop -- see the note at the loop head.
                target_abs = (grid_anchor_steps
                              + rel_step_steps * grid_index
                              + grid_slack_steps)
                step_steps_this_move = target_abs - curr_abs
                send_cmd(f"LR{step_steps_this_move}")
                send_cmd("M")

                # Pause handling during move
                while state["is_paused"] and not state["stop_flag"]:
                    time.sleep(0.05)

                if not wait_until_position(target_abs, timeout=timeout_for_target_steps(target_abs, st_nm)):
                    safe_stop(); log("[STOP] Scan interrupted")
                    # Re-anchor the label to the REAL position instead of
                    # leaving pos_nm/state['current_nm'] frozen at the last
                    # successfully COMPLETED step while the drive coasted
                    # somewhere else during the DEC ramp. Mirrors
                    # free_run_worker's post-sweep re-anchor ("Re-anchor to
                    # the REAL position after the sweep, however it ended").
                    # grid_anchor_steps/s_nm is the SAME anchor pair the
                    # [DIAG] logging above already uses for enc_nm, so this is
                    # the identical conversion, applied here on the abort
                    # path.
                    # Excludes grid_slack_steps_before (all reversals CONFIRMED
                    # complete before this step) plus however much of THIS
                    # step's own `comp` the partial travel actually consumed --
                    # slack is taken up first, so an interrupted reversal has
                    # not moved the grating by the full encoder delta (see
                    # _slack_consumed_steps()).
                    try:
                        _pos_now = read_position_or_none()
                        if _pos_now is None:
                            real_nm = None
                        else:
                            _consumed = _slack_consumed_steps(_pos_now - curr_abs, comp)
                            real_nm = s_nm + steps_to_delta_nm(
                                _pos_now - grid_anchor_steps
                                - grid_slack_steps_before - _consumed)
                        if real_nm is None:
                            # POS unreadable -- keep the last completed step as
                            # the estimate instead of writing a fabricated
                            # position (see _encoder_nm_or_none()).
                            log("[STOP] Could not read POS after interrupted step; "
                                "position estimate left at the last completed "
                                "step and possibly stale.", "warn")
                        else:
                            if abs(real_nm - pos_nm) > 1e-6:
                                log(f"[STOP] Re-anchoring to real position: "
                                    f"{pos_nm:.3f} -> {real_nm:.3f} nm "
                                    f"(read back from POS after interrupted step).")
                            pos_nm = real_nm
                            state["current_nm"] = pos_nm
                            state['planned_params']['last_nm'] = pos_nm
                            refs['entry_current'].delete(0, "end")
                            refs['entry_current'].insert(0, f"{pos_nm:.3f}")
                    except Exception as e:
                        log(f"[STOP] Could not re-anchor after interrupted step: {e}", "warn")
                    break
                # Label/state are updated HERE, right after a successful
                # wait_until_position(), rather than after the post-step audit
                # below. Reached target_abs == pos_nm_next is now a fact; if
                # the audit then halts the scan on a fault/limit, the label
                # must already reflect the step that WAS completed, not the
                # previous one.
                pos_nm = pos_nm_next
                state["current_nm"] = pos_nm
                state['planned_params']['last_nm'] = pos_nm
                refs['entry_current'].delete(0, "end"); refs['entry_current'].insert(0, f"{state['current_nm']:.3f}")

                # Post-step limit audit (passive; stops only on FAULT/INTERNAL_LIMIT)
                try:
                    if not postmove_limit_audit('scan-step'):
                        safe_stop()
                        log("[STOP] Scan halted due to drive limit/fault (post-step audit).", "warn")
                        break
                except Exception:
                    pass

                if SAFE_AFTER_MOVE: safe_stop()

                # Pause checkpoint: after completing this step
                if state.get('pause_requested'):
                    state['pause_requested'] = False
                    state['is_paused'] = True
                    set_fsm('PAUSED')
                    try:
                        refs['btn_pause'].configure(state='disabled'); refs['btn_resume'].configure(state='normal')
                    except Exception:
                        pass
                    if ser_ok():
                        safe_stop()
                    log('[PAUSE] Paused after finishing current step.')
                    return
                # NOTE: dwell is now consumed inside acquire_measurement() as
                # pre-settle + averaging + residual, so we must NOT sleep(dwell)
                # again here or the per-point time would be doubled.
            else:
                # while/else: runs ONLY if the loop finished by its condition
                # going False (the scan covered its full range), NOT via any
                # of the `break`s above -- so this never re-does or conflicts
                # with the interrupted-step re-anchor further up.
                #
                # Verify the label against the real (encoder-frame) position
                # once the scan finishes normally, mirroring free_run_worker's
                # post-sweep re-anchor. Every reversal in this scan is now
                # CONFIRMED complete, so grid_slack_steps can be fully
                # excluded here (unlike the partial-move re-anchor above,
                # which only excludes CONFIRMED prior slack via
                # grid_slack_steps_before -- see the NOTE there). This is
                # what catches a slip-nm miscalibration or backlash-model
                # mismatch -- a whole scan silently shifting by up to one
                # slip -- before it carries into the next scan/goto: with a
                # correctly matched slip value this converges to the pure
                # step-arithmetic pos_nm already in use (verified in SIM,
                # sim_defect_probe.py); with a mismatch it corrects it instead
                # of letting it silently accumulate.
                try:
                    real_nm = _encoder_nm_or_none(grid_anchor_steps, s_nm,
                                                  slack_steps=grid_slack_steps)
                    if real_nm is None:
                        # POS unreadable -- keep the commanded grid arithmetic
                        # (pos_nm) instead of writing a fabricated position
                        # (see _encoder_nm_or_none()).
                        log("[WARN] Could not read POS at scan end; final position "
                            "not verified against the encoder.", "warn")
                    elif abs(real_nm - pos_nm) > REANCHOR_LOG_TOL_NM:
                        log(f"[DONE] Re-anchoring to real position: {pos_nm:.3f} -> "
                            f"{real_nm:.3f} nm (read back from POS, slack-adjusted).")
                        pos_nm = real_nm
                        state["current_nm"] = pos_nm
                        state['planned_params']['last_nm'] = pos_nm
                        refs['entry_current'].delete(0, "end")
                        refs['entry_current'].insert(0, f"{pos_nm:.3f}")
                except Exception as e:
                    log(f"[WARN] Could not verify final position: {e}", "warn")

            state["is_scanning"] = False
            refs['btn_pause'].configure(state="disabled"); refs['btn_resume'].configure(state="disabled")
            # Without this call, Goto/Scan/Jog/Queue stay locked after a scan
            # finishes on its own -- only pressing Stop unlocks them, because
            # stop_action() happens to call recover_after_stop(), which calls
            # this same hook as a side effect. do_resume always calls this on
            # its own completion path; scan_worker must too.
            ui_hook('set_buttons_scanning', False)
            # The loop is left by `break` on Stop as well as on normal
            # completion, so the log message must distinguish the two --
            # otherwise "[DONE] Scan complete." would print directly after
            # "[STOP] Scan aborted", two contradictory lines about the same
            # scan. The CSV status already distinguishes them ("interrupted");
            # the log must match.
            _was_stopped = bool(state.get("stop_flag"))
            if _was_stopped:
                log("[DONE] Scan stopped early -- partial data kept and saved.")
            else:
                log("[DONE] Scan complete.")
            try:
                if state.get('active_plot_id'):
                    _plot().finalize_scan(
                        state['active_plot_id'],
                        status=("interrupted" if _was_stopped else "completed"))

                try:
                    if refs['autosave_csv_var'].get():
                        export_active_scan_csv()
                except Exception:
                    pass

            except Exception:
                pass
            set_fsm("IDLE")
        except Exception as e:
            state["is_scanning"] = False
            refs['btn_pause'].configure(state="disabled"); refs['btn_resume'].configure(state="disabled")
            ui_hook('set_buttons_scanning', False)  # unlock controls on the error path too, same as normal completion above
            log(f"[ERROR] scan: {e}", "error")
            try:
                if state.get('active_plot_id'):
                    _plot().finalize_scan(state['active_plot_id'], status="error")
            except Exception:
                pass

    threading.Thread(target=scan_worker, daemon=True).start()

def free_run_scan_action():
    """Continuous ("free run") scan: ONE motion command from Start to End
    (nm), with POS + a raw DAQ read polled together in a tight loop WHILE
    the motor is actually moving -- instead of the usual per-point
    move -> settle -> average -> next-point cycle. Uses the SCAN section's
    Start/End fields; Step/Wait do NOT apply here (no per-point spacing, no
    dwell/averaging -- a single raw read_pmt_voltage() per poll, same as
    probe_move_timing.py measured it).

    WHY/WHAT THIS TRADES: real-position readback (steps_to_delta_nm) instead
    of the commanded-delta tracking every other move in this file uses, so
    the wavelength axis reflects where the motor ACTUALLY was at each DAQ
    read, not where it was told to go. In exchange: no averaging (SNR is
    whatever a single raw read gives), and spectral spacing is UNEVEN,
    governed by real motor speed (ramp-limited early/late in the move,
    cruise-speed in between) and the measured ~19-20 Hz POS+DAQ poll rate on
    this rig (see BACKLOG.md / probe_move_timing.py -- not a guessed number).

    NO PAUSE/RESUME in this mode: it is one continuous controller-side move,
    there is no clean per-point checkpoint to pause on the way it works for
    scan_worker/do_resume. Stop still works (aborts the move immediately).

    Not yet rig-verified end-to-end (SIM/rig smoke test still open, see
    BACKLOG P1) -- the underlying POS/DAQ polling pattern itself IS
    rig-measured (probe_move_timing.py), only this specific worker wiring
    is new."""
    if state["is_scanning"]:
        refs['messagebox'].showwarning("Busy", "Scan already running.")
        return
    if not ser_ok():
        refs['messagebox'].showerror("Error", "Not connected.")
        return
    _cfg = validate_free_run_inputs()
    if _cfg is None:
        return
    s_nm = _cfg["start"]; e_nm = _cfg["end"]; sp_rpm = _cfg["sp"]
    # st_nm/dwell kept as 0.0 (not None) so the existing CSV filename-pattern
    # builder (_build_filename, which does f"{planned.get('st_nm', 0):.3f}")
    # doesn't hit a formatting TypeError on a stored None -- same dict shape
    # as a normal scan's planned_params, just with a 'mode' marker added.
    state['planned_params'] = {'s_nm': s_nm, 'e_nm': e_nm, 'st_nm': 0.0, 'dwell': 0.0,
                               'mode': 'free_run', 'sp_rpm': sp_rpm}

    state["is_scanning"] = True
    set_fsm("SCAN")
    ui_hook('set_buttons_scanning', True)
    state["is_paused"] = False
    state["stop_flag"] = False
    # set_buttons_scanning(True) just enabled Pause -- free-run doesn't
    # support it (see docstring), so immediately disable it again.
    try:
        refs['btn_pause'].configure(state="disabled")
        refs['btn_resume'].configure(state="disabled")
        refs['btn_stop'].configure(state="normal")
    except Exception:
        pass

    def free_run_worker():
        try:
            init_motor()

            # Seat at start_nm -- identical pattern to scan_worker's first
            # move (same backlash-reversal handling via
            # reversal_compensation_steps).
            current_steps = read_position()
            current_nm_ui = float(refs['entry_current'].get() or state["current_nm"])
            delta_first = s_nm - current_nm_ui
            rel_first = steps_for_delta_nm(delta_first)
            if abs(delta_first) > 0:
                rel_first += reversal_compensation_steps(1 if delta_first >= 0 else -1)
            target_first = current_steps + rel_first
            state['current_step_target_abs'] = target_first
            send_cmd(f"LR{rel_first}"); send_cmd("M")
            if not wait_until_position(target_first, timeout=timeout_for_target_steps(target_first, delta_first)):
                safe_stop()
                raise RuntimeError("Stopped before reaching start.")
            state["current_nm"] = s_nm
            refs['entry_current'].delete(0, "end"); refs['entry_current'].insert(0, f"{state['current_nm']:.3f}")
            if SAFE_AFTER_MOVE: safe_stop()

            if state["stop_flag"]:
                raise RuntimeError("Stopped before sweep.")

            # ONE continuous move from start to end (the actual "free run").
            start_steps_abs = read_position()
            direction = 1 if e_nm >= s_nm else -1
            delta_sweep = e_nm - s_nm
            # Backlash compensation is SLACK TAKE-UP, not optical travel: those
            # steps turn the motor without moving the grating, so they must be
            # driven (hence added to the commanded move) but must NOT count as
            # wavelength change.
            #
            # The nm axis must be computed relative to nm_anchor_steps
            # (post-slack), NOT (pos - start_steps_abs): the latter counts the
            # compensation steps as real wavelength travel, so the sweep runs
            # the full distance PLUS the compensation and the end point comes
            # out short by exactly the compensation (0.082 nm on this rig).
            # The stepped scan does not have this problem because it tracks
            # pos_nm arithmetically instead of from raw POS -- which is why a
            # free run and a stepped scan of the same line would otherwise
            # disagree by exactly one compensation.
            comp_steps = reversal_compensation_steps(direction)
            rel_sweep = steps_for_delta_nm(delta_sweep) + comp_steps
            target_steps = start_steps_abs + rel_sweep
            # Anchor for the nm axis: the position AFTER the slack has been
            # taken up. While the first comp_steps are being driven the
            # grating is not yet moving, so nm correctly stays at s_nm.
            nm_anchor_steps = start_steps_abs + comp_steps
            # Sign of travel in COMMANDED-STEP space -- this is NOT the same as
            # `direction` (nm space): steps_for_delta_nm applies INVERT_DIRECTION,
            # so a +nm sweep can be a -step move on this rig. The overshoot guard
            # in the poll loop compares raw POS against target_steps, so it must
            # use the step-space sign. Derive it from the actual rel_sweep we're
            # about to send, not from the nm direction.
            direction_steps = 1 if rel_sweep >= 0 else -1
            timeout_s = timeout_for_target_steps(target_steps, delta_sweep)
            # timeout_for_target_steps() -> timeout_for_nm() budgets purely on
            # DISTANCE via the fixed SEC_PER_NM constant (device_constants.py)
            # -- it has NO idea a Free Run can run at a slowed-down sp_rpm
            # instead of the normal RAMP_SP. At RAMP_SP that budget already
            # has margin; at a manual SP override BELOW RAMP_SP the move
            # genuinely takes proportionally longer, so the same budget can
            # run out before the sweep physically gets there.
            #
            # timeout_for_target_steps() budgets purely on distance and does
            # not know about a slowed-down sp_rpm; at RAMP_SP that budget has
            # margin, but a sweep running at a fraction of RAMP_SP genuinely
            # takes proportionally longer and can exceed the unscaled budget
            # before it physically arrives (rig-confirmed: a 5 nm sweep at
            # SP=500 rpm = RAMP_SP/20 timed out at 3.8 of 5 nm against a
            # ~55 s unscaled budget, while every other tested speed
            # (1000-10000 rpm) finished normally within it).
            # If this sweep is slower than RAMP_SP, stretch the budget by the
            # same ratio the drive itself is slowed down by. Capped by
            # MAX_MOVE_TIMEOUT so a very slow override still can't hang the UI
            # forever if something else is genuinely wrong.
            if sp_rpm and sp_rpm < RAMP_SP:
                speed_ratio = RAMP_SP / float(sp_rpm)
                timeout_s = min(MAX_MOVE_TIMEOUT, timeout_s * speed_ratio)

            # Same short legend format as the stepped scan (see there), with
            # SECONDS: free runs are quick and typically fired back to back
            # (often within the same minute), so a timestamp without seconds
            # would produce byte-identical legend entries for two different
            # sweeps and make them indistinguishable. Step/averaging are
            # omitted because a free run has neither; the sweep speed is shown
            # instead, since that is the one setting that distinguishes two
            # otherwise identical free runs.
            label = (f"{time.strftime('%H:%M:%S')} "
                     f"{s_nm:g}\u2192{e_nm:g} free {sp_rpm:g}rpm")
            state['active_plot_id'] = _plot().start_new_scan(
                label, {'start': s_nm, 'end': e_nm, 'step': None, 'mode': 'free_run'})

            # Apply the (already clamped, <= RAMP_SP) speed override for
            # JUST this sweep move -- RAM-only (same as tune_ramp.py), never
            # SAVE/EEPSAV. Only SP changes; AC/DEC ramp shape stays at
            # RAMP_AC/RAMP_DEC from init_motor() above.
            if sp_rpm != RAMP_SP:
                configure_ramp(sp=sp_rpm)
                log(f"[FREE RUN] Speed override: SP={sp_rpm} rpm for this sweep "
                    f"(normal RAMP_SP={RAMP_SP}, restored after).")
            else:
                # Log the speed even when it is NOT an override: the speed a
                # sweep ran at must always be in the log, or two runs logged
                # without it are indistinguishable even though one may have
                # run at a very different rate.
                log(f"[FREE RUN] Speed: SP={sp_rpm} rpm (no override, this is "
                    f"the configured RAMP_SP).")

            log(f"[FREE RUN] {s_nm:.3f} -> {e_nm:.3f} nm, one continuous move, "
                f"polling POS+DAQ (~19-20 Hz measured on this rig, see "
                f"probe_move_timing.py).")
            send_cmd(f"LR{rel_sweep}"); send_cmd("M")

            last_pos = None
            stable = 0
            t_start = time.time()
            n_points = 0
            n_failed_reads = 0
            n_outliers = 0
            t_last_fail_log = 0.0
            t_last_poll = time.time()
            # Max plausible step change between two polls. The motor cannot
            # physically move further than SP (rpm) allows in one poll gap.
            # We don't know the exact rpm->steps/s here, so use a generous
            # bound from the sweep itself: the whole sweep is |rel_sweep|
            # steps; a single poll gap (~50 ms at ~20 Hz) can only be a small
            # fraction of that. Even at max speed the probe measured <=~3000
            # steps/poll -- allow 50x margin (150k steps) so a real fast move
            # never trips it, but a jump of MILLIONS of steps (the observed
            # ~587 nm spike = ~millions of steps off) is caught. Guard is
            # per-elapsed-time so a slow poll doesn't false-trip.
            _MAX_STEPS_PER_SEC = 3000 * 20 * 50  # steps/poll * polls/s * margin
            while True:
                if state["stop_flag"]:
                    safe_stop(); log("[STOP] Free run aborted"); break
                if (time.time() - t_start) > timeout_s:
                    safe_stop(); log(f"[TIMEOUT] Free run exceeded {timeout_s:.0f}s.", "warn"); break

                # read_position_raw_or_none(), NOT read_position(): the latter
                # silently returns 0 on a failed/garbled POS reply, which
                # would plot straight to whatever nm corresponds to step 0
                # -- a wild, wrong jump. Skip the sample instead and keep
                # polling; a transient serial glitch shouldn't corrupt the
                # trace. The _raw string is kept so an OUTLIER (below) can be
                # logged with the exact bytes the drive sent -- needed to
                # tell a garbled/partial reply apart from a stale-but-valid
                # OLD reply, see BACKLOG's ~587 nm repeating-spike entry.
                pos_steps, pos_raw = read_position_raw_or_none()
                now = time.time()
                if pos_steps is None:
                    n_failed_reads += 1
                    if now - t_last_fail_log >= 2.0:
                        t_last_fail_log = now
                        log(f"[FREE RUN] POS read failed/timed out ({n_failed_reads} so far) "
                            f"-- sample skipped, not plotted.", "warn")
                    continue

                # Outlier reject: a POS reply that PARSES as a valid int but is
                # physically impossible (jumps millions of steps in one poll
                # gap) is a corrupted/misaligned serial frame OR a stale old
                # reply, NOT the real current position. Plotting it is exactly
                # the ~587 nm spike-and-return zigzag the user reported. Reject
                # it and keep polling; the next (correct) read continues the
                # trace. read_position_raw_or_none already handles empty
                # replies (pos_steps is None, above); this handles
                # wrong-but-parseable-value ones. ALWAYS log the raw string for
                # every outlier (not rate-limited like the failed-read case
                # above) -- these are rare enough that we want every one on
                # record verbatim, to compare against a future occurrence.
                if last_pos is not None:
                    dt = max(1e-3, now - t_last_poll)
                    max_jump = _MAX_STEPS_PER_SEC * dt
                    if abs(pos_steps - last_pos) > max_jump:
                        n_outliers += 1
                        log(f"[FREE RUN] POS outlier #{n_outliers} rejected: raw reply "
                            f"{pos_raw!r} -> parsed {pos_steps} steps, jumped "
                            f"{abs(pos_steps - last_pos)} steps in {dt*1000:.0f} ms from "
                            f"last good {last_pos} (impossible -- sample skipped, not "
                            f"plotted).", "warn")
                        # do NOT update last_pos/t_last_poll from a bad read
                        continue
                t_last_poll = now

                v = read_pmt_voltage()
                # Measured from the post-slack anchor, so the compensation
                # steps do not masquerade as wavelength change.
                nm_now = s_nm + steps_to_delta_nm(pos_steps - nm_anchor_steps)

                if state.get('active_plot_id'):
                    _plot().append_scan_point(state['active_plot_id'], nm_now, v)
                n_points += 1

                state["current_nm"] = nm_now
                refs['entry_current'].delete(0, "end"); refs['entry_current'].insert(0, f"{nm_now:.3f}")

                # HARD target-overshoot guard. The "standing still AND
                # near-target" test below never fires if the motor RUNS PAST
                # the target (e.g. the move command travelled further than
                # intended, or the drive was in a bad state) -- without this
                # guard the loop would keep polling and plotting far beyond
                # End. Stop the instant the real position reaches or passes
                # target_steps in the direction of travel, regardless of
                # whether it has settled. `direction_steps` is +1/-1 in
                # commanded-step space (already includes INVERT_DIRECTION via
                # steps_for_delta_nm that built rel_sweep), so compare in that
                # same step space.
                reached_target = ((direction_steps > 0 and pos_steps >= target_steps - POS_TOL_STEPS) or
                                  (direction_steps < 0 and pos_steps <= target_steps + POS_TOL_STEPS))
                if reached_target:
                    safe_stop()
                    log(f"[FREE RUN] Reached/passed target ({nm_now:.3f} nm); stopping sweep.")
                    break

                if pos_steps == last_pos:
                    stable += 1
                    if abs(pos_steps - target_steps) <= POS_TOL_STEPS and stable >= POS_STABLE_COUNT:
                        break
                else:
                    stable = 0
                last_pos = pos_steps

            if n_failed_reads:
                log(f"[FREE RUN] {n_failed_reads} POS read(s) failed/timed out during this run "
                    f"and were skipped (not plotted).", "warn")
            if n_outliers:
                log(f"[FREE RUN] {n_outliers} POS outlier(s) (impossible jumps, likely garbled "
                    f"replies) rejected during this run and were NOT plotted.", "warn")
            if sp_rpm != RAMP_SP:
                # Restore the normal speed regardless of how the sweep ended
                # (completed/stopped/timed out) -- don't leave a slowed-down
                # SP silently active for whatever move happens next. (Belt
                # and suspenders: the NEXT worker's own init_motor() would
                # reapply RAMP_SP anyway, but don't rely on that implicitly.)
                configure_ramp(sp=RAMP_SP)
            if SAFE_AFTER_MOVE: safe_stop()

            # Re-anchor to the REAL position after the sweep, however it ended.
            # Critical after a Stop/fault mid-move: the loop stops plotting, but
            # the motor may coast/settle somewhere else, and state['current_nm']
            # would otherwise stay frozen at the last PLOTTED value -- exactly
            # the "app says 586, mechanical dial says 318" decoupling seen at
            # the rig. Read POS once more and convert to nm relative to the
            # sweep start, so the software estimate matches the hardware as
            # closely as this relative system allows. (Still a software
            # estimate, not an absolute readback -- but anchored to where the
            # motor actually is now, not where the plot stopped.)
            try:
                final_pos = read_position_or_none()
                if final_pos is not None:
                    # Same anchor as the live nm axis above -- measuring from
                    # start_steps_abs here would re-introduce the compensation
                    # offset into the final position, same failure mode as the
                    # live axis above.
                    real_nm = s_nm + steps_to_delta_nm(final_pos - nm_anchor_steps)
                    if abs(real_nm - float(state.get('current_nm', real_nm))) > 1e-6:
                        log(f"[FREE RUN] Re-anchoring to real position: "
                            f"{state.get('current_nm'):.3f} -> {real_nm:.3f} nm "
                            f"(read back from POS after sweep).")
                    state['current_nm'] = real_nm
                    refs['entry_current'].delete(0, "end")
                    refs['entry_current'].insert(0, f"{real_nm:.3f}")
            except Exception as e:
                log(f"[FREE RUN] Could not re-anchor to real position: {e}", "warn")

            try:
                if not postmove_limit_audit('free-run'):
                    log("[STOP] Drive reported fault/limit after free run.", "warn")
            except Exception:
                pass

            log(f"[DONE] Free run complete: {n_points} points, "
                f"{s_nm:.3f} -> {state['current_nm']:.3f} nm.")

            state["is_scanning"] = False
            refs['btn_pause'].configure(state="disabled"); refs['btn_resume'].configure(state="disabled")
            ui_hook('set_buttons_scanning', False)
            try:
                if state.get('active_plot_id'):
                    _plot().finalize_scan(state['active_plot_id'],
                                          status=("interrupted" if state.get("stop_flag") else "completed"))
                try:
                    if refs['autosave_csv_var'].get():
                        export_active_scan_csv()
                except Exception:
                    pass
            except Exception:
                pass
            set_fsm("IDLE")
        except Exception as e:
            state["is_scanning"] = False
            refs['btn_pause'].configure(state="disabled"); refs['btn_resume'].configure(state="disabled")
            ui_hook('set_buttons_scanning', False)
            log(f"[ERROR] free_run: {e}", "error")
            try:
                # Belt and suspenders: if the exception happened after the
                # SP override was applied but before the normal restore
                # point, don't leave the drive at a slowed-down speed.
                if sp_rpm != RAMP_SP:
                    configure_ramp(sp=RAMP_SP)
            except Exception:
                pass
            try:
                if state.get('active_plot_id'):
                    _plot().finalize_scan(state['active_plot_id'], status="error")
            except Exception:
                pass
            set_fsm("IDLE")

    threading.Thread(target=free_run_worker, daemon=True).start()

def pause_action():
    """Pause after current motion step completes (no immediate stop)."""
    if not state["is_scanning"]:
        return
    try:
        state['pause_requested'] = True
        try:
            refs['btn_pause'].configure(state="disabled"); refs['btn_resume'].configure(state="disabled")
        except Exception:
            pass
        log("[PAUSE] Will pause after the current step finishes.")
    except Exception as e:
        log(f"[ERROR] pause: {e}", "error")

def resume_action():
    """Resume scan without re-reading GUI. Uses stored planned_params to finish remaining points."""
    if not state.get('is_scanning'):
        log('[RESUME] Ignored: no active scan.', 'warn')
        return
    if not state.get('is_paused'):
        log('[RESUME] Ignored: not paused.', 'warn')
        return
    threading.Thread(target=do_resume, daemon=True).start()

def do_resume():
    """Resume a paused scan using stored planned_params only (no GUI re-read)."""
    try:
        if not state.get("is_scanning"):
            return
        state['pause_requested'] = False
        if not ser_ok():
            refs['messagebox'].showerror("Error", "Not connected.")
            return
        state["is_paused"] = False
        set_fsm("SCAN")
        ui_hook('set_buttons_scanning', True)
        try:
            refs['btn_pause'].configure(state="normal"); refs['btn_resume'].configure(state="disabled"); refs['btn_stop'].configure(state="normal")
        except Exception:
            pass

        p = state.get('planned_params', {}) or {}
        s_nm = float(p.get('s_nm')); e_nm = float(p.get('e_nm')); st_nm = float(p.get('st_nm')); dwell = float(p.get('dwell')); direction = int(p.get('direction', 1))

        init_motor()

        pos_nm = float((p.get('last_nm') if p.get('last_nm') is not None else state.get('current_nm', s_nm)))
        tgt_abs = state.get("current_step_target_abs", None)
        if ser_ok() and tgt_abs is not None:
            # Do NOT finish a pending target on resume; clear it to avoid an
            # unwanted pre-move before the resume loop's own first step.
            state["current_step_target_abs"] = None
            log("[RESUME] Ignoring pending target; continuing from current position.", "warn")
            try:
                # Keep UI current wavelength as-is (no pre-move)
                refs['entry_current'].delete(0, "end"); refs['entry_current'].insert(0, f"{pos_nm:.3f}")
            except Exception:
                pass
        # Uses SCAN_EPSILON_NM (device_constants), the same float-rounding
        # guard scan_worker uses for its loop condition -- both are nm-scale,
        # far below step resolution, so do_resume and scan_worker must agree
        # on the same tolerance.
        if direction > 0 and pos_nm < s_nm - SCAN_EPSILON_NM: pos_nm = s_nm
        if direction < 0 and pos_nm > s_nm + SCAN_EPSILON_NM: pos_nm = s_nm

        rel_step_steps = steps_for_delta_nm(st_nm) * direction

        while (direction > 0 and pos_nm <= e_nm + SCAN_EPSILON_NM) or (direction < 0 and pos_nm >= e_nm - SCAN_EPSILON_NM):
            if state.get("stop_flag"):
                safe_stop(); log("[STOP] Resume aborted", "warn"); break

            # Same averaged acquisition as the fresh scan (single source of truth).
            v = acquire_measurement(dwell)

            try:
                if state.get('active_plot_id'):
                    _plot().append_scan_point(state['active_plot_id'], pos_nm, v)
            except Exception:
                pass
            log(f"{pos_nm:.3f} nm  |  {v:.5f} V")

            pos_nm_next = pos_nm + st_nm*direction
            # read_position_or_none(), not read_position(): this value is both
            # the base of the relative move commanded just below AND the anchor
            # the interrupted-branch re-anchor converts against. read_position()
            # returns a silent 0 on a failed reply, which would command a move
            # from a fabricated origin and then write an equally fabricated
            # wavelength into the label (see _encoder_nm_or_none()).
            curr_abs = read_position_or_none()
            if curr_abs is None:
                safe_stop()
                log("[STOP] Resume: POS unreadable; refusing to command a move "
                    "from an unknown position. Re-anchor with 'Known λ here' "
                    "before resuming.", "error")
                break
            # Fold in backlash compensation on a direction reversal. Usually a
            # no-op across a pause/resume (direction is unchanged), but NOT
            # always: a jog while paused flips state['last_move_direction'],
            # so the first resumed step then does get a compensation. Kept in
            # its own variable so the interrupted-branch re-anchor below can
            # exclude the part of it that was actually consumed.
            comp = reversal_compensation_steps(direction)
            step_steps_this_move = rel_step_steps + comp
            target_abs = curr_abs + step_steps_this_move
            state["current_step_target_abs"] = target_abs
            send_cmd(f"LR{step_steps_this_move}"); send_cmd("M")

            while state.get("is_paused") and not state.get("stop_flag"):
                time.sleep(0.05)

            reached = wait_until_position(target_abs, timeout=timeout_for_target_steps(target_abs, abs(st_nm)))
            if not reached:
                safe_stop(); log("[STOP] Resume interrupted", "warn")
                # Re-anchor pos_nm/current_nm to the REAL position instead of
                # leaving them at the last successfully completed step (same
                # rationale as scan_worker's interrupted-step re-anchor).
                # do_resume reads curr_abs fresh every iteration (no fixed
                # grid_anchor_steps like scan_worker's absolute grid), so the
                # anchor for this conversion is simply THIS iteration's
                # pre-move position (curr_abs, already read above), paired
                # with pos_nm (the label before this failed step).
                try:
                    # Same slack-first correction as scan_worker's interrupted
                    # step (see _slack_consumed_steps()); comp is normally 0
                    # here, but not after a jog during the pause.
                    _pos_now = read_position_or_none()
                    if _pos_now is None:
                        real_nm = None
                    else:
                        _consumed = _slack_consumed_steps(_pos_now - curr_abs, comp)
                        real_nm = pos_nm + steps_to_delta_nm(
                            _pos_now - curr_abs - _consumed)
                    if real_nm is None:
                        # POS unreadable -- keep the last completed step as the
                        # estimate (see _encoder_nm_or_none()).
                        log("[STOP] Could not read POS after interrupted step; "
                            "position estimate left at the last completed step "
                            "and possibly stale.", "warn")
                    else:
                        if abs(real_nm - pos_nm) > 1e-6:
                            log(f"[STOP] Re-anchoring to real position: "
                                f"{pos_nm:.3f} -> {real_nm:.3f} nm "
                                f"(read back from POS after interrupted step).")
                        pos_nm = real_nm
                        state['planned_params']['last_nm'] = pos_nm
                        state["current_nm"] = pos_nm
                        try:
                            refs['entry_current'].delete(0, "end"); refs['entry_current'].insert(0, f"{pos_nm:.3f}")
                        except Exception:
                            pass
                except Exception as e:
                    log(f"[STOP] Could not re-anchor after interrupted step: {e}", "warn")
                break

            if SAFE_AFTER_MOVE: safe_stop()

            pos_nm = pos_nm_next
            state['planned_params']['last_nm'] = pos_nm
            state["current_nm"] = pos_nm
            try:
                refs['entry_current'].delete(0, "end"); refs['entry_current'].insert(0, f"{state['current_nm']:.3f}")
            except Exception:
                pass

            # Pause checkpoint in resume loop: after finishing this step
            if state.get('pause_requested'):
                state['pause_requested'] = False
                state['is_paused'] = True
                set_fsm('PAUSED')
                try:
                    refs['btn_pause'].configure(state='disabled'); refs['btn_resume'].configure(state='normal')
                except Exception:
                    pass
                if ser_ok():
                    safe_stop()
                log('[PAUSE] Paused after finishing current step.')
                return
        state["is_scanning"] = False
        ui_hook('set_buttons_scanning', False)
        try:
            if state.get('active_plot_id'):
                _plot().finalize_scan(state['active_plot_id'], status=("interrupted" if state.get("stop_flag") else "completed"))
        except Exception:
            pass
        set_fsm("IDLE")
    except Exception as e:
        state["is_scanning"] = False
        ui_hook('set_buttons_scanning', False)
        log(f"[RESUME ERROR] {e}", "error")

def stop_action():
    """Request an abort of whatever is running, from the GUI thread.

    THE RACE THIS PREVENTS: stop_flag is set here and NEVER cleared in this
    function. Re-arming (recover_after_stop(), which clears stop_flag) is
    deferred to a background thread that first waits for the running worker
    to actually leave its loop (is_scanning / is_moving going False). If
    stop_flag were cleared immediately -- e.g. by running recover_after_stop()
    synchronously on the GUI thread right after safe_stop() -- there would be
    only a brief window (bounded by however long safe_stop() takes) during
    which the worker thread could observe the flag; a worker whose sleep loop
    happens to check stop_flag outside that window would simply carry on
    running, unaware Stop was ever pressed. safe_stop()'s ST/V0 are
    fire-and-forget (no acknowledgment wait in silent answer mode), so that
    window is only ~20 ms -- SHORTER than the 50 ms PAUSE_POLL_S granularity
    at which the worker's sleep loops check the flag -- making the race easy
    to hit rather than a rare fluke.

    Doing the serial work (safe_stop/recover_after_stop) off the GUI thread
    also keeps the Stop button responsive: both take the shared serial lock,
    which the scan worker holds for stretches at a time, so running them
    synchronously on the GUI thread would block the whole UI while a worker
    holds the lock.

    The wait covers BOTH state['is_scanning'] (set by scan_worker/
    free_run_worker/do_resume) AND state['is_moving'] (set by goto_worker,
    see app_context.py) -- a GoTo is not a scan, so without also waiting on
    is_moving, a Stop pressed during a GoTo would skip the wait, re-arm the
    drive immediately, and appear to have worked ("[OK] Recovered after
    STOP; ready." logs right away) while goto_worker's own
    wait_until_position() polling loop kept running underneath, unaware
    stop_flag had already been cleared, until its full move timeout (up to
    MAX_MOVE_TIMEOUT = 900 s) elapsed."""
    # 1) Signal the abort IMMEDIATELY and leave the flag set. Everything that
    #    could clear it again happens later, after the worker has exited.
    state["stop_flag"] = True
    was_running = bool(state.get("is_scanning")) or bool(state.get("is_moving"))
    log("[STOP] Stop requested; motor set safe.")
    # Immediate UI feedback on the calling thread (no serial involved).
    try:
        refs['btn_pause'].configure(state="disabled")
        refs['btn_resume'].configure(state="disabled")
    except Exception:
        pass

    def _stop_worker():
        try:
            safe_stop()
        except Exception:
            pass
        # Wait for the running scan/free-run/goto worker to actually notice
        # the flag and leave its loop. Bounded, so a wedged worker can't
        # block re-arming forever -- but long enough that the normal case
        # (worker notices within a poll interval, finishes its cleanup)
        # always wins. Covers GoTo too (is_moving) -- see the docstring.
        if was_running:
            t0 = time.time()
            while (state.get("is_scanning") or state.get("is_moving")) \
                    and (time.time() - t0) < STOP_JOIN_TIMEOUT_S:
                time.sleep(0.02)
            if state.get("is_scanning") or state.get("is_moving"):
                log(f"[STOP] Worker did not exit within {STOP_JOIN_TIMEOUT_S:.0f}s; "
                    f"re-arming anyway.", "warn")
        state["is_paused"] = False
        try:
            if state.get('active_plot_id'):
                _plot().finalize_scan(state['active_plot_id'], status="stopped")
        except Exception:
            pass
        # recover_after_stop() clears stop_flag / is_scanning / is_paused and
        # re-arms the drive (ST/EN/V0). Safe to do now: the worker is out.
        recover_after_stop()

    threading.Thread(target=_stop_worker, daemon=True).start()

def jog_minus_action():
    try:
        step = float(refs['jog_step_var'].get())
    except Exception:
        step = 0.5
    try:
        cur_nm = float(refs['entry_current'].get() or state.get("current_nm", 0.0))
    except Exception:
        cur_nm = state.get("current_nm", 0.0)
    target = cur_nm - step
    try:
        refs['entry_goto'].delete(0, "end")
        refs['entry_goto'].insert(0, f"{target:.3f}")
    except Exception:
        pass
    goto_wavelength_action()

def jog_plus_action():
    try:
        step = float(refs['jog_step_var'].get())
    except Exception:
        step = 0.5
    try:
        cur_nm = float(refs['entry_current'].get() or state.get("current_nm", 0.0))
    except Exception:
        cur_nm = state.get("current_nm", 0.0)
    target = cur_nm + step
    try:
        refs['entry_goto'].delete(0, "end")
        refs['entry_goto'].insert(0, f"{target:.3f}")
    except Exception:
        pass
    goto_wavelength_action()

def mark_point_action():
    try:
        x_nm = float(refs['entry_current'].get() or state.get("current_nm", 0.0))
    except Exception:
        x_nm = state.get("current_nm", 0.0)
    try:
        y_val = (read_pmt_voltage_avg(window_sec=float(refs['entry_wait'].get())) if refs['avg_mode_var'].get()=='time' else read_pmt_voltage_avg())
    except Exception:
        y_val = None
    if y_val is None:
        log("[WARN] No DAQ reading available for Mark Point.", "warn")
        return
    try:
        _plot().append_jog_point(x_nm, y_val)
        _plot()._schedule_redraw()
        log(f"[JOG] Marked point λ={x_nm:.3f} nm, V={y_val:.5f}")
    except Exception as e:
        log(f"[WARN] Could not append jog point: {e}", "warn")

def _capture_scan_settings():
    """Snapshot ALL per-scan acquisition settings from the input fields.

    WHY THIS EXISTS: a queue entry must be able to hold acquisition settings
    (timebase, pre-settle, averaging) that differ from what is currently in
    the fields, and the queue list must be able to show them per entry.
    Reading these settings LIVE from the fields at the moment each queued
    scan runs would mean every entry in the queue silently uses whatever
    happens to be in the fields at that moment, rather than what was intended
    for that entry -- so each entry carries its own snapshot instead.

    Returns None (after showing an error) if start/end/step are unusable;
    the acquisition fields fall back to safe defaults individually rather than
    failing the whole capture."""
    try:
        s_nm = float(refs['entry_start'].get())
        e_nm = float(refs['entry_end'].get())
        st_nm = float(refs['entry_step'].get())
    except Exception:
        refs['messagebox'].showerror("Invalid", "Please enter valid Start/End/Step.")
        return None

    # NOTE ON UNITS/SOURCES: read through ui_get(), not refs[].
    #  * refs['entry_wait'] is an _EntryAdapterMsToSec -- it returns SECONDS,
    #    while the field (and everything shown to the user) is in ms. Reading
    #    it via refs[] here would show "0.1ms tb" for a field containing 100.
    #  * 'entry_presettle' and 'avg_fraction_var' are not in refs at all (only
    #    registered as vars), so refs[...] would silently fall through to
    #    defaults and capture pre-settle as 0 regardless of the field.
    # ui_get() is the registered-var path and returns the raw field strings.
    def _f(var_name, default):
        try:
            return float(ui_get(var_name, default))
        except Exception:
            return float(default)

    return {
        "start": s_nm, "end": e_nm, "step": st_nm,
        # ms, as displayed -- ui_get('entry_wait') would give seconds, so read
        # the widget text directly for the timebase.
        "timebase_ms": _f_raw_ms(),
        "presettle_ms": _f('presettle_ms_var', 0.0),
        "avg_mode": str(ui_get('avg_mode_var', 'samples') or 'samples'),
        "avg_samples": _f('avg_samples_var', 8.0),
        "avg_fraction": _f('avg_fraction_var', 100.0),
    }


def _f_raw_ms():
    """Timebase in MILLISECONDS, straight from the widget text.

    refs['entry_wait'] / ui_get('entry_wait') both go through
    _EntryAdapterMsToSec and hand back seconds; the queue stores and displays
    ms, so it must not use that path."""
    try:
        return float(refs['entry_wait_raw'].get())
    except Exception:
        pass
    try:
        return float(ui_get('entry_wait', 0.0)) * 1000.0
    except Exception:
        return 0.0


def _queue_item_label(item, idx):
    """One-line queue display INCLUDING the acquisition settings.

    The averaging settings are what actually determine the noise floor of a
    queued measurement, so they belong in the list -- without them, two
    entries with the same wavelength range but different averaging would be
    indistinguishable in the queue display."""
    base = (f"{idx:02d}: {_fmt_nm(item['start'])} → {_fmt_nm(item['end'])}, "
            f"Δ {_fmt_nm(item['step'])} nm")
    tb = item.get('timebase_ms', 0.0)
    ps = item.get('presettle_ms', 0.0)
    mode = item.get('avg_mode', 'samples')
    if mode == 'time':
        avg = f"time {item.get('avg_fraction', 100.0):g}%"
    else:
        avg = f"{item.get('avg_samples', 8.0):g} samples"
    return f"{base}  |  {tb:g}ms tb, {ps:g}ms pre, avg {avg}"


def _refresh_queue_listbox():
    """Rebuild the whole list from state['scan_queue'] (indices stay correct
    after a remove/update)."""
    try:
        lb = refs['queue_listbox']
        lb.delete(0, "end")
        for i, it in enumerate(state["scan_queue"]):
            lb.insert("end", _queue_item_label(it, i + 1))
    except Exception:
        pass


def add_scan_to_queue():
    item = _capture_scan_settings()
    if item is None:
        return
    state["scan_queue"].append(item)
    _refresh_queue_listbox()
    log(f"[QUEUE] Added scan: {item}")


def update_selected_scan():
    """Overwrite the selected queue entry with the CURRENT field values.

    Without this, changing an acquisition setting after queueing meant
    deleting the entry and re-adding it (and re-entering the wavelength range),
    because each entry now carries its own settings snapshot by design."""
    try:
        sel = refs['queue_listbox'].curselection()
    except Exception:
        sel = None
    if not sel:
        try:
            refs['messagebox'].showinfo("Queue", "Select a queue entry to update.")
        except Exception:
            pass
        return
    idx = int(sel[0])
    if not (0 <= idx < len(state["scan_queue"])):
        return
    item = _capture_scan_settings()
    if item is None:
        return
    state["scan_queue"][idx] = item
    _refresh_queue_listbox()
    try:
        refs['queue_listbox'].selection_set(idx)
    except Exception:
        pass
    log(f"[QUEUE] Updated entry {idx+1}: {item}")

def remove_selected_scan():
    try:
        sel = refs['queue_listbox'].curselection()
        if not sel:
            return
        idx = sel[0]
        try:
            del state["scan_queue"][idx]
        except Exception:
            pass
        # Rebuild from the data instead of re-splitting the displayed strings:
        # the label contains ':' inside the settings part, so parsing it back
        # out with something like split(":", 1) would mangle the renumbering.
        _refresh_queue_listbox()
        log("[QUEUE] Removed selected scan.")
    except Exception as e:
        log(f"[WARN] Remove failed: {e}", "warn")

def clear_scan_queue():
    state["scan_queue"].clear()
    try:
        refs['queue_listbox'].delete(0, "end")
    except Exception:
        pass
    log("[QUEUE] Cleared.")

def _fmt_nm(v):
    """Format a wavelength/step for display and for writing back into an entry
    field, WITHOUT silently destroying precision.

    A fixed "%.3f" would turn a 0.0001 nm step into "0.000" in the scan queue
    display -- and, far worse, run_scan_queue_worker() writes these strings
    BACK into the Step field before running each queued scan, so the queued
    scan would then execute with a step of literally zero. Uses as many
    decimals as the value actually needs, up to 6, and never fewer than 3 so
    ordinary values still read as "589.000"."""
    try:
        v = float(v)
    except Exception:
        return str(v)
    for nd in range(3, 7):
        t = f"{v:.{nd}f}"
        if abs(float(t) - v) < 1e-12:
            return t
    return f"{v:.6f}"


def run_scan_queue_action():
    if state.get("is_scanning"):
        refs['messagebox'].showwarning("Busy", "Already scanning.")
        return
    if not state["scan_queue"]:
        refs['messagebox'].showinfo("Queue", "Queue is empty.")
        return
    threading.Thread(target=run_scan_queue_worker, daemon=True).start()

def run_scan_queue_worker():
    try:
        refs['btn_run_queue'].configure(state="disabled")
        refs['btn_add_queue'].configure(state="disabled")
        refs['btn_remove_queue'].configure(state="disabled")
        refs['btn_clear_queue'].configure(state="disabled")
    except Exception:
        pass
    for i, item in enumerate(list(state["scan_queue"])):
        if not ser_ok():
            log("[QUEUE] Stopped: not connected.", "warn")
            break
        try:
            refs['entry_start'].delete(0, "end"); refs['entry_start'].insert(0, _fmt_nm(item['start']))
            refs['entry_end'].delete(0, "end"); refs['entry_end'].insert(0, _fmt_nm(item['end']))
            refs['entry_step'].delete(0, "end"); refs['entry_step'].insert(0, _fmt_nm(item['step']))
            # Apply this entry's OWN acquisition settings. Older queue entries
            # (saved before settings were captured per item) simply have no
            # such keys and are left at whatever is currently in the fields,
            # which is exactly the old behaviour.
            def _apply(ref_name, key, fmt=lambda v: f"{v:g}"):
                if key not in item:
                    return
                try:
                    refs[ref_name].delete(0, "end")
                    refs[ref_name].insert(0, fmt(item[key]))
                except Exception:
                    pass
            _apply('entry_wait', 'timebase_ms')
            _apply('entry_presettle', 'presettle_ms')
            _apply('avg_samples_var', 'avg_samples')
            _apply('avg_fraction_var', 'avg_fraction')
            if 'avg_mode' in item:
                try:
                    refs['avg_mode_var'].set(item['avg_mode'])
                except Exception:
                    pass
        except Exception:
            pass
        log(f"[QUEUE] Running {i+1}/{len(state['scan_queue'])}: {item}")
        scan_action()
        while state.get("is_scanning"):
            if state.get("stop_flag"):
                log("[QUEUE] Aborted by Stop.", "warn")
                break
            time.sleep(0.2)
        if state.get("stop_flag"):
            break
    try:
        refs['btn_run_queue'].configure(state="normal")
        refs['btn_add_queue'].configure(state="normal")
        refs['btn_remove_queue'].configure(state="normal")
        refs['btn_clear_queue'].configure(state="normal")
    except Exception:
        pass
    log("[QUEUE] Done.")
    try:
        ui_hook('ui_after', _maybe_prompt_save_all_scans_after_queue)
    except Exception:
        pass

def clear_plot_action():
    try:
        _plot().clear_all()
    except Exception as e:
        log(f"[ERROR] clear plot: {e}", "error")

def _ensure_dir(path):
    try:
        os.makedirs(path, exist_ok=True)
    except Exception:
        pass

def _safe_filename(dirpath, fname):
    base, ext = os.path.splitext(fname)
    out = os.path.join(dirpath, fname)
    n = 1
    while os.path.exists(out):
        out = os.path.join(dirpath, f"{base}-{n:03d}{ext}")
        n += 1
    return out

def _build_filename(planned, pattern, scanid=None, queueid=None):
    now = datetime.datetime.now()
    tokens = dict(
        date=now.strftime("%Y%m%d"),
        time=now.strftime("%H%M%S"),
        start=f"{planned.get('s_nm', 0):.3f}",
        end=f"{planned.get('e_nm', 0):.3f}",
        step=f"{planned.get('st_nm', 0):.3f}",
        wait=f"{planned.get('dwell', 0):.3f}",
        mode=("time" if refs['avg_mode_var'].get()=="time" else "samples") if 'avg_mode_var' in globals() else "",
        samples=(refs['avg_samples_var'].get() if 'avg_samples_var' in globals() else ""),
        frac=(f"{_get_avg_fraction():.1f}" if '_get_avg_fraction' in globals() else ""),
        presettle=(f"{_get_presettle_sec()*1000:.0f}" if '_get_presettle_sec' in globals() else ""),
        scanid=scanid or "",
        queueid=queueid or "",
    )
    try:
        fname = pattern.format(**tokens)
    except Exception:
        fname = f"scan_{tokens['date']}_{tokens['time']}.csv"
    return "".join(c for c in fname if c not in r'\/\:*?"<>|')

def _scan_metadata(scanid):
    p = state.get('planned_params', {}) if 'state' in globals() else {}
    meta = {
        "scan_id": scanid,
        "started_at": state.get("scan_started_at") if 'state' in globals() else "",
        "finished_at": datetime.datetime.now().isoformat(timespec="seconds"),
        "status": ("interrupted" if (state.get("stop_flag") if 'state' in globals() else False) else "completed"),
        "start_nm": p.get("s_nm"), "end_nm": p.get("e_nm"),
        "step_nm": p.get("st_nm"), "wait_s": p.get("dwell"),
        "avg_mode": ("time" if (refs['avg_mode_var'].get()=="time" if 'avg_mode_var' in globals() else False) else "samples"),
        "avg_samples": (refs['avg_samples_var'].get() if 'avg_samples_var' in globals() else ""),
        "avg_fraction_pct": (_get_avg_fraction() if '_get_avg_fraction' in globals() else ""),
        "pre_settle_ms": (int(_get_presettle_sec()*1000) if '_get_presettle_sec' in globals() else ""),
    }
    return meta

def export_scan_csv_by_id(plot_id, planned, folder, pattern, scanid=None, queueid=None):
    try:
        xs, ys = _plot().get_points(plot_id)
    except Exception:
        xs, ys = [], []
    _ensure_dir(folder)
    fname = _build_filename(planned or {}, pattern or "scan_{date}_{time}.csv", scanid, queueid)
    path = _safe_filename(folder, fname)
    meta = _scan_metadata(scanid)

    with open(path, "w", newline="", encoding="utf-8") as f:
        for k,v in meta.items():
            f.write(f"# {k}: {v}\n")
        # Record where the voltages came from. Without this a CSV recorded
        # with no NI driver present is indistinguishable from a real
        # measurement -- the simulator produces plausible emission lines at
        # 486/589/656 nm. See daq.daq_source_label().
        f.write(f"# signal_source: {daq_source_label()}\n")
        if daq_source_label() != "NI-DAQ":
            f.write("# WARNING: voltages in this file are SIMULATED, not measured.\n")
        f.write("# columns: index,wavelength_nm,voltage_V\n")
        w = csv.writer(f)
        w.writerow(["index","wavelength_nm","voltage_V"])
        for i, (x,y) in enumerate(zip(xs, ys)):
            try:
                w.writerow([i, f"{float(x):.6f}", f"{float(y):.6f}"])
            except Exception:
                w.writerow([i, x, y])
    try:
        log(f"[CSV] Saved {path}")
    except Exception:
        pass
    return path

def export_active_scan_csv():
    try:
        pid = state.get('active_plot_id')
    except Exception:
        pid = None
    if not pid:
        try:
            refs['messagebox'].showwarning("Export", "No active scan to export.")
        except Exception:
            pass
        return
    p = state.get('planned_params', {}) if 'state' in globals() else {}
    scanid = state.get("scan_id") if 'state' in globals() else None
    try:
        saved = export_scan_csv_by_id(pid, p, refs['export_dir_var'].get(), refs['pattern_var'].get(), scanid, state.get("queue_id") if 'state' in globals() else None)
    except Exception as e:
        try:
            refs['messagebox'].showerror("Export failed", str(e))
        except Exception:
            pass
        return
    return saved

def export_all_scans_and_summary():
    """
    Export every scan in the current PlotManager to CSV and write a queue_summary.csv.
    Uses refs['export_dir_var'] for folder and export_scan_csv_by_id for each plot.
    """
    try:
        folder = refs['export_dir_var'].get() if 'export_dir_var' in globals() else os.path.expanduser("~/scans")
    except Exception:
        folder = os.path.expanduser("~/scans")
    try:
        _ensure_dir(folder)
    except Exception:
        pass
    rows = []
    try:
        qid = state.get("queue_id") if 'state' in globals() else ""
    except Exception:
        qid = ""
    try:
        # Gather all plot IDs from PlotManager
        if 'plot_mgr' in globals():
            with _plot()._lock:
                plot_ids = list(_plot()._plots.keys())
        else:
            plot_ids = []
    except Exception:
        plot_ids = []
    for pid in plot_ids:
        try:
            planned = state.get("planned_params", {}) if 'state' in globals() else {}
        except Exception:
            planned = {}
        try:
            scanid = state.get("scan_id") if 'state' in globals() else None
        except Exception:
            scanid = None
        try:
            saved = export_scan_csv_by_id(pid, planned, folder, "scan_{date}_{time}.csv", scanid, qid)
            rows.append({"queue_id": qid, "scan_id": scanid or "", "filepath": saved})
        except Exception as e:
            try:
                log(f"[CSV] Export failed for {pid}: {e}", "error")
            except Exception:
                pass
    # Write simple summary CSV
    try:
        summ = os.path.join(folder, f"queue_{qid or 'summary'}.csv")
        with open(summ, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["queue_id","scan_id","filepath"])
            for r in rows:
                w.writerow([r.get("queue_id",""), r.get("scan_id",""), r.get("filepath","")])
        try:
            log(f"[CSV] Queue summary saved: {summ}")
        except Exception:
            pass
    except Exception as e:
        try:
            log(f"[ERROR] Writing summary failed: {e}", "error")
        except Exception:
            pass
