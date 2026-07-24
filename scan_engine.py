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
            delta_nm = target_nm - current_nm_ui
            rel_steps = steps_for_delta_nm(delta_nm)
            # Fold in backlash compensation if this move reverses direction
            # relative to the last known seated direction (no-op otherwise,
            # or if no Reference Run has been run yet -- see BACKLOG).
            if abs(delta_nm) > 0:
                rel_steps += reversal_compensation_steps(1 if delta_nm >= 0 else -1)
            # target absolute steps estimate:
            target_steps = current_steps + rel_steps
            state['current_step_target_abs'] = target_steps

            send_cmd(f"LR{rel_steps}")
            send_cmd("M")
            reached = wait_until_position(target_steps, timeout=timeout_for_target_steps(target_steps, delta_nm))
            if SAFE_AFTER_MOVE:
                safe_stop()
            if reached:
                state["current_nm"] = target_nm
                refs['entry_current'].delete(0, "end"); refs['entry_current'].insert(0, f"{state['current_nm']:.3f}")
                if not postmove_limit_audit('goto'):
                    log("[STOP] Drive reported fault/limit after move; halting.", "warn")
                else:
                    log(f"[DONE] Reached {target_nm:.3f} nm")


            else:
                log("[STOP] Motion interrupted")
                # Auto-recover once and retry the goto
                recover_after_stop()
                init_motor()
                current_steps = read_position()
                try:
                    current_nm_ui = float(refs['entry_current'].get() or state["current_nm"])
                except Exception:
                    current_nm_ui = state.get("current_nm", 0.0)
                delta_nm = target_nm - current_nm_ui
                rel_steps = steps_for_delta_nm(delta_nm)
                if abs(delta_nm) > 0:
                    rel_steps += reversal_compensation_steps(1 if delta_nm >= 0 else -1)
                target_steps = current_steps + rel_steps
                state['current_step_target_abs'] = target_steps
                send_cmd(f"LR{rel_steps}")
                send_cmd("M")
                reached2 = wait_until_position(target_steps, timeout=timeout_for_target_steps(target_steps, abs(delta_nm)))
                if SAFE_AFTER_MOVE:
                    safe_stop()
                if reached2:
                    state["current_nm"] = target_nm
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
            ui_hook('set_buttons_connected', state["connected"])
            if state.get("fsm") == "MOVING":
                set_fsm("IDLE")

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

            # ABSOLUTE STEP GRID (fixes cumulative scan drift).
            # Each step used to be commanded relative to the position measured
            # right before it: target = read_position() + fixed_step. Any
            # shortfall on one step therefore became the starting point of the
            # next one and was carried forward FOREVER instead of being
            # corrected -- a pure relative chain.
            # It accumulates because wait_until_position() accepts arrival
            # within POS_TOL_STEPS, and on this rig POS_TOL_STEPS
            # (0.01 nm = 3617 steps) is exactly ONE 0.01 nm scan step wide, so
            # a move may legitimately stop a long way short and still count.
            # Measured on the rig (2026-07-23, 10 identical queued scans):
            # +22.5 pm drift per scan cycle over 200 steps = ~40 steps lost per
            # step move (~1.1 %), strictly monotonic. Cross-checked against a
            # 0.2 nm / 20-step series: +2.7 pm per cycle. Drift scaled with the
            # NUMBER OF STEPS (~0.11-0.17 pm each), NOT with the number of
            # reversals (2 per cycle in both) -- which is what rules out
            # backlash as the cause and points at the relative chain.
            # Now every target is derived from an anchor taken once at the
            # scan start, so a shortfall on one step is simply corrected by the
            # next move. Backlash compensation is accumulated separately
            # (grid_slack_steps): those steps do turn the motor and DO show up
            # in POS, but they take up slack instead of moving the grating, so
            # they shift the grid without being wavelength travel.
            grid_anchor_steps = read_position()
            grid_slack_steps = 0
            grid_index = 0
            while (direction > 0 and pos_nm <= e_nm + SCAN_EPSILON_NM) or (direction < 0 and pos_nm >= e_nm - SCAN_EPSILON_NM):
                if state["stop_flag"]:
                    safe_stop(); log("[STOP] Scan aborted"); break

                # Measure ONE averaged point at the current wavelength.
                # (Pre-settle + averaging + residual dwell all happen inside
                #  acquire_measurement; this is the fix for AVG being ignored
                #  on a fresh scan -- see BACKLOG/HANDOVER.)
                v = acquire_measurement(dwell)
                if state.get('active_plot_id'):
                    _plot().append_scan_point(state['active_plot_id'], pos_nm, v)
                log(f"{pos_nm:.3f} nm  |  {v:.5f} V")

                # Next step
                # Stop checkpoint BEFORE issuing the move. acquire_measurement()
                # above honours stop_flag and can return early, but the loop
                # used to fall straight through into LR/M anyway -- i.e. Stop
                # was followed by the drive being told to make ANOTHER step,
                # which wait_until_position() then aborted. Net effect: a small
                # unintended move after every Stop pressed during a dwell.
                if state["stop_flag"]:
                    safe_stop(); log("[STOP] Scan aborted"); break
                pos_nm_next = pos_nm + st_nm*direction
                curr_abs = read_position()
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
                    safe_stop(); log("[STOP] Scan interrupted"); break
                # Post-step limit audit (passive; stops only on FAULT/INTERNAL_LIMIT)
                try:
                    if not postmove_limit_audit('scan-step'):
                        safe_stop()
                        log("[STOP] Scan halted due to drive limit/fault (post-step audit).", "warn")
                        break
                except Exception:
                    pass


                if SAFE_AFTER_MOVE: safe_stop()

                pos_nm = pos_nm_next
                state["current_nm"] = pos_nm
                state['planned_params']['last_nm'] = pos_nm
                refs['entry_current'].delete(0, "end"); refs['entry_current'].insert(0, f"{state['current_nm']:.3f}")

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

            state["is_scanning"] = False
            refs['btn_pause'].configure(state="disabled"); refs['btn_resume'].configure(state="disabled")
            # FIX: scan_worker previously never called this on normal completion
            # (unlike do_resume, which always does). Without it, Goto/Scan/Jog/
            # Queue stayed locked after a scan finished on its own -- only
            # pressing Stop unlocked them, because stop_action() happens to call
            # recover_after_stop(), which calls this same hook as a side effect.
            ui_hook('set_buttons_scanning', False)
            # Say what actually happened. The loop is left by `break` on Stop
            # as well as on normal completion, so this used to print
            # "[DONE] Scan complete." directly after "[STOP] Scan aborted" --
            # two contradictory lines about the same scan. The CSV status was
            # already correct ("interrupted"); only the log lied.
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
            ui_hook('set_buttons_scanning', False)  # same fix as above, error path
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
            # BUG THIS FIXES (rig log 2026-07-23, three identical sweeps):
            # target 587.000 nm, re-anchored to 586.918 nm every single time --
            # off by 0.082 nm, which is EXACTLY the logged slip compensation.
            # The nm axis was computed as (pos - start_steps_abs), i.e. it
            # counted the compensation as real wavelength travel, so the sweep
            # ran the full distance PLUS the compensation and then labelled the
            # end point 0.082 nm short. The stepped scan does not have this
            # problem because it tracks pos_nm arithmetically, which is why
            # free run and stepped scan of the same line came out offset
            # against each other by exactly one compensation.
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
            # CONFIRMED at the rig (2026-07-23): a 5 nm sweep at SP=500 rpm
            # (RAMP_SP/20) stopped at 388.798 nm -- 3.8 of 5 nm, no
            # "[FREE RUN] Reached/passed target" line, no STOP -- after 857
            # points (~44 s at the measured ~19.5 Hz poll rate), matching the
            # unscaled 55 s budget (10.0 s/nm * 5 nm + 5 s overhead) far too
            # closely to be coincidence. Every OTHER speed in the same
            # session (1000-10000 rpm) reached its target normally with the
            # same unscaled budget -- because at those speeds the move
            # actually finishes well inside it. Only the slowest override
            # exposed the missing scaling.
            #
            # Fix: if this sweep is slower than RAMP_SP, stretch the budget by
            # the same ratio the drive itself is slowed down by. Capped by
            # MAX_MOVE_TIMEOUT so a very slow override still can't hang the
            # UI forever if something else is genuinely wrong.
            if sp_rpm and sp_rpm < RAMP_SP:
                speed_ratio = RAMP_SP / float(sp_rpm)
                timeout_s = min(MAX_MOVE_TIMEOUT, timeout_s * speed_ratio)

            # Same short legend format as the stepped scan (see there), with
            # SECONDS. The old label used '%Y-%m-%d %H:%M' -- no seconds -- so
            # two free runs started in the same minute produced byte-identical
            # legend entries and could not be told apart. Free runs are quick
            # and typically fired back to back, which is exactly when that
            # happens: the three sweeps in the 2026-07-23 rig log ran at
            # 16:20:50, 16:21:03 and 16:21:12, i.e. two of them would have
            # collided. Step/averaging are omitted because a free run has
            # neither; the sweep speed is shown instead, since that is the one
            # setting that distinguishes two otherwise identical free runs.
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
                # Log the speed even when it is NOT an override. Previously
                # this line only appeared for overrides, so a sweep running at
                # full RAMP_SP was silent about it -- in the 2026-07-23 rig log
                # that made two consecutive sweeps look identical while the
                # second actually ran 10x faster (and produced all the POS
                # fragments). The speed a sweep ran at must always be in the
                # log, or the log cannot be compared against another run.
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

                # HARD target-overshoot guard. The only "done" test used to be
                # position-standing-still-AND-near-target below; that never
                # fires if the motor RUNS PAST the target (e.g. the move
                # command travelled further than intended, or the drive was in
                # a bad state) -- the loop then keeps polling and plotting far
                # beyond End, exactly the 305->586 nm / 867-point runaway seen
                # at the rig. Stop the instant the real position reaches or
                # passes target_steps in the direction of travel, regardless of
                # whether it has settled. `direction` is +1/-1 in commanded-
                # step space (already includes INVERT_DIRECTION via
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
                    # offset (this is the line that produced the logged
                    # "587.000 -> 586.918 nm" every sweep).
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
            # PATCH: Do NOT finish pending target on resume; clear it to avoid pre-move.
            state["current_step_target_abs"] = None
            log("[RESUME] Ignoring pending target; continuing from current position.", "warn")
            try:
                # Keep UI current wavelength as-is (no pre-move)
                refs['entry_current'].delete(0, "end"); refs['entry_current'].insert(0, f"{pos_nm:.3f}")
            except Exception:
                pass
        # Epsilon unified with scan_worker: use SCAN_EPSILON_NM (device_constants)
        # instead of a hardcoded 1e-12 here. Both are float-rounding guards only
        # (nm-scale, far below step resolution), so the change is behaviourally
        # inert -- it just removes the do_resume/scan_worker inconsistency.
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
            curr_abs = read_position()
            # Fold in backlash compensation on a direction reversal; normally
            # a no-op here since direction is unchanged across a pause/resume.
            step_steps_this_move = rel_step_steps + reversal_compensation_steps(direction)
            target_abs = curr_abs + step_steps_this_move
            state["current_step_target_abs"] = target_abs
            send_cmd(f"LR{step_steps_this_move}"); send_cmd("M")

            while state.get("is_paused") and not state.get("stop_flag"):
                time.sleep(0.05)

            reached = wait_until_position(target_abs, timeout=timeout_for_target_steps(target_abs, abs(st_nm)))
            if not reached:
                safe_stop(); log("[STOP] Resume interrupted", "warn"); break

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

    THE RACE THIS FIXES (reported symptom: "Stop does nothing, you have to
    press it several times"):
    This used to run entirely on the GUI thread and finished by calling
    recover_after_stop() -- which sets state['stop_flag'] = False to re-arm
    the drive for the next command. But the scan worker is a SEPARATE thread
    that was still running its loop at that moment. So stop_flag was only
    True for the duration of safe_stop() in between; if the worker did not
    happen to look at the flag inside that window, it never saw the request
    at all and simply carried on scanning. Pressing Stop repeatedly
    eventually hit the window by luck -- hence "sometimes it works".

    The window used to be ~2.4 s, because safe_stop()'s ST and V0 each waited
    out a full serial timeout for an acknowledgment that never comes in
    silent answer mode. Making those commands fire-and-forget (correct, and a
    large speedup elsewhere) shrank the window to ~20 ms -- SHORTER than the
    50 ms PAUSE_POLL_S granularity at which the worker's sleep loops check
    the flag. So the pre-existing race went from "rarely bites" to "usually
    bites". Same bug, just finally visible.

    Now: stop_flag is set and NEVER cleared here. Re-arming is deferred to a
    background thread that first waits for the worker to actually leave its
    loop (is_scanning going False), so recover_after_stop() can no longer
    pull the flag out from under a worker that hasn't noticed it yet.

    Doing the serial work off the GUI thread also keeps the button responsive:
    safe_stop()/recover_after_stop() take the shared serial lock, which the
    scan worker holds for stretches at a time -- blocking on it from the GUI
    thread froze the whole UI, which is the other half of why Stop "did
    nothing"."""
    # 1) Signal the abort IMMEDIATELY and leave the flag set. Everything that
    #    could clear it again happens later, after the worker has exited.
    state["stop_flag"] = True
    was_running = bool(state.get("is_scanning"))
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
        # Wait for the running scan/free-run worker to actually notice the
        # flag and leave its loop. Bounded, so a wedged worker can't block
        # re-arming forever -- but long enough that the normal case (worker
        # notices within a poll interval, finishes its cleanup) always wins.
        if was_running:
            t0 = time.time()
            while state.get("is_scanning") and (time.time() - t0) < STOP_JOIN_TIMEOUT_S:
                time.sleep(0.02)
            if state.get("is_scanning"):
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

    WHY THIS EXISTS: a queue entry used to store only start/end/step. Timebase,
    pre-settle and every averaging setting were read LIVE from the fields at
    the moment each queued scan ran -- so a queue could not hold different
    acquisition settings per entry, the queue list had nothing to show, and
    whatever happened to be in the fields when the queue was started silently
    applied to every entry in it. Now each entry carries its own snapshot.

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
    #    it here produced "0.1ms tb" for a field containing 100.
    #  * 'entry_presettle' and 'avg_fraction_var' are not in refs at all (only
    #    registered as vars), so refs[...] silently fell through to defaults
    #    and pre-settle was always captured as 0.
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
    queued measurement, so they belong in the list -- previously the entry
    showed only the wavelength range and there was no way to tell two
    otherwise-identical entries apart."""
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
        # the label now contains ':' inside the settings part, so the old
        # split(":", 1) renumbering would have mangled it.
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

    A fixed "%.3f" turned a 0.0001 nm step into "0.000": the scan queue showed
    "Delta 0.000 nm", and -- far worse -- run_scan_queue_worker() writes these
    strings BACK into the Step field before running each queued scan, so the
    queued scan would have been executed with a step of literally zero. Uses as
    many decimals as the value actually needs, up to 6, and never fewer than 3
    so ordinary values still read as "589.000"."""
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
