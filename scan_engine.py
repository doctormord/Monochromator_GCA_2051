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
from device_constants import STEPS_PER_NM, SAFE_AFTER_MOVE, SCAN_EPSILON_NM
from protocol_faulhaber import (
    ser_ok, send_cmd, read_position, safe_stop, init_motor, recover_after_stop,
)
from motion import (
    nm_to_steps, steps_for_delta_nm, wait_until_position,
    timeout_for_target_steps, reversal_compensation_steps, postmove_limit_audit,
)
from daq import (
    acquire_measurement, read_pmt_voltage, read_pmt_voltage_avg, sleep_with_pause,
    _get_avg_fraction, _get_presettle_sec,
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
            # or if no Referenzfahrt has been run yet -- see BACKLOG).
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
            label = time.strftime('%Y-%m-%d %H:%M') + f" λ={s_nm:.3f}→{e_nm:.3f} Δ={st_nm:.3f}nm"
            state['active_plot_id'] = _plot().start_new_scan(label, {'start': s_nm, 'end': e_nm, 'step': st_nm})

            pos_nm = s_nm
            rel_step_steps = steps_for_delta_nm(st_nm) * direction
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
                pos_nm_next = pos_nm + st_nm*direction
                # Predict target steps from current absolute (before issuing move)
                curr_abs = read_position()
                # Fold in backlash compensation on the iteration where the
                # direction actually reverses; a no-op every other iteration.
                step_steps_this_move = rel_step_steps + reversal_compensation_steps(direction)
                target_abs = curr_abs + step_steps_this_move
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
            log("[DONE] Scan complete.")
            try:
                if state.get('active_plot_id'):
                    _plot().finalize_scan(state['active_plot_id'], status="completed")

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
    state["stop_flag"] = True
    safe_stop()
    state["is_paused"] = False
    state["is_scanning"] = False
    refs['btn_pause'].configure(state="disabled"); refs['btn_resume'].configure(state="disabled")
    log("[STOP] Stop requested; motor set safe.")
    try:
        if state.get('is_scanning') and state.get('active_plot_id'):
            _plot().finalize_scan(state['active_plot_id'], status="stopped")
    except Exception:
        pass
    ui_hook('set_buttons_connected', state["connected"])
    set_fsm("IDLE")

    recover_after_stop()

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

def add_scan_to_queue():
    try:
        s_nm = float(refs['entry_start'].get())
        e_nm = float(refs['entry_end'].get())
        st_nm = float(refs['entry_step'].get())
    except Exception:
        refs['messagebox'].showerror("Invalid", "Please enter valid Start/End/Step.")
        return
    item = {"start": s_nm, "end": e_nm, "step": st_nm}
    state["scan_queue"].append(item)
    try:
        refs['queue_listbox'].insert("end", f"{len(state['scan_queue']):02d}: {s_nm:.3f} → {e_nm:.3f}, Δ {st_nm:.3f} nm")
    except Exception:
        pass
    log(f"[QUEUE] Added scan: {item}")

def remove_selected_scan():
    try:
        sel = refs['queue_listbox'].curselection()
        if not sel:
            return
        idx = sel[0]
        refs['queue_listbox'].delete(idx)
        try:
            del state["scan_queue"][idx]
        except Exception:
            pass
        for i in range(refs['queue_listbox'].size()):
            txt = refs['queue_listbox'].get(i)
            rest = txt.split(":", 1)[-1].strip()
            refs['queue_listbox'].delete(i)
            refs['queue_listbox'].insert(i, f"{i+1:02d}: {rest}")
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
            refs['entry_start'].delete(0, "end"); refs['entry_start'].insert(0, f"{item['start']:.3f}")
            refs['entry_end'].delete(0, "end"); refs['entry_end'].insert(0, f"{item['end']:.3f}")
            refs['entry_step'].delete(0, "end"); refs['entry_step'].insert(0, f"{item['step']:.3f}")
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
