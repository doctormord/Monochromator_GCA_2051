"""
motion.py — Motion logic: nm<->steps, timeouts, waiting for the target,
post-move audits, FSM-adjacent move orchestration, and backlash compensation.

WHAT THIS MODULE DOES
---------------------
Everything between "the controller can do LR/M/POS" (protocol_faulhaber) and
"the GUI starts a scan" (scan_engine):

  - nm_to_steps / steps_for_delta_nm : wavelength <-> motor-steps conversion
                                       (including the rig's INVERT_DIRECTION).
  - timeout_for_nm / _target_steps   : dynamic move timeouts.
  - wait_until_position (+ 2 impls)  : blocks until the target is reached,
                                       via statusword (primary) or POS
                                       tolerance (fallback). Respects pause/stop.
  - postmove_*_audit (3 of them)     : passive fault/limit checks after
                                       a move (see note below).
  - Backlash: _get_slip_nm, reversal_compensation_steps, reference_run_action
                                       -- compensation for the mechanical
                                       gear play (model: device_constants).

Knows NOTHING about the GUI directly: GUI values via ui_get, GUI actions via
ui_hook (from app_context). Imports protocol_faulhaber for hardware access.

NOTE on the three audit functions: they come from different stages of
development, have different enable flags (device_constants), and overlapping
purposes. They are deliberately NOT merged -- a merge could silently disable
a safety check and needs sign-off + rig verification (BACKLOG P3).

Origin: extracted from the monolith (function bodies unchanged; only the GUI
coupling was switched to ui_get/ui_hook, constants/deps imported).

IMPORTANT (doc policy, see HANDOVER.md): docstrings/comments are preserved.
"""

import time
import threading
from typing import Optional

from app_context import state, log, set_fsm, ui_get, ui_hook, refs
from device_constants import (
    STEPS_PER_NM, INVERT_DIRECTION, SAFE_AFTER_MOVE,
    POLL_INTERVAL, POS_TOL_STEPS, POS_STABLE_COUNT,
    SEC_PER_NM, TIMEOUT_OVERHEAD_S, MIN_MOVE_TIMEOUT, MAX_MOVE_TIMEOUT,
    REFERENCE_MOVE_NM, REFERENCE_DIRECTION,
    USE_STATUSWORD, STATUS_ACK_ARM_TIMEOUT_S, STATUS_POLL_S,
    STATUS_TR_DEBOUNCE_POLLS,
    STATUS_FAULT_BIT, STATUS_TR_BIT, STATUS_ACK_BIT,
    STATUS_INTERNAL_LIMIT_BIT, STATUS_INT_LIMIT_BIT,
    USE_POSTMOVE_STATUSWORD_AUDIT, USE_POSTMOVE_LIMIT_AUDIT, POST_MOVE_STATUS_AUDIT,
    OST_ERROR_MASK, OST_INPUT_MASK,
    OST_STATUS_INPUT1, OST_STATUS_INPUT2, OST_STATUS_INPUT3,
    OST_FAULT_MASK, OST_HARD_LIMIT_MASK, OST_INTERNAL_LIMIT_MASK,
    SOFT_LIMIT_MIN_NM, SOFT_LIMIT_MAX_NM, SOFT_LIMIT_MARGIN_NM,
    FREE_RUN_SP_STEPS_PER_POLL,
)
from protocol_faulhaber import (
    send_cmd, read_position, read_position_retry, read_ost, safe_stop,
    init_motor, _read_statusword, _decode_ost_flags,
)


def nm_to_steps(nm: float) -> int:
    # Only meaningful as a relative conversion unless you have an absolute offset.
    return int(round(nm * STEPS_PER_NM))

def steps_for_delta_nm(delta_nm: float) -> int:
    steps = int(round(delta_nm * STEPS_PER_NM))
    if INVERT_DIRECTION:
        steps = -steps
    return steps

def steps_to_delta_nm(delta_steps) -> float:
    """Inverse of steps_for_delta_nm(): raw motor-step delta -> wavelength
    delta (nm), same INVERT_DIRECTION convention.

    WHERE/WHEN: everything else in this codebase tracks state['current_nm']
    from the COMMANDED delta (trust the move that was sent), never reads
    it back from POS. A free-run/continuous scan is the first consumer that
    polls POS WHILE the motor is moving and needs the REAL measured position
    converted to nm at each poll -- this is that conversion, kept here next
    to its forward counterpart rather than duplicated at the call site."""
    dnm = float(delta_steps) / float(STEPS_PER_NM)
    if INVERT_DIRECTION:
        dnm = -dnm
    return dnm

def estimate_free_run_steps_per_poll(sp_rpm) -> float:
    """Rough linear interpolation over FREE_RUN_SP_STEPS_PER_POLL
    (device_constants.py -- measured via probe_move_timing.py, see that
    table's docstring for the caveats: NOT a physical model, up to ~2x
    run-to-run variance in the source data).

    Below/above the measured range, this returns the nearest endpoint
    rather than extrapolating further -- outside [1000, 10000] rpm there
    is zero measured data for this rig, and pretending to extrapolate a
    number we have no basis for would be worse than saying 'same as the
    closest thing we actually measured'."""
    table = sorted(FREE_RUN_SP_STEPS_PER_POLL)
    sp_rpm = float(sp_rpm)
    if sp_rpm <= table[0][0]:
        return float(table[0][1])
    if sp_rpm >= table[-1][0]:
        return float(table[-1][1])
    for (sp_lo, spp_lo), (sp_hi, spp_hi) in zip(table, table[1:]):
        if sp_lo <= sp_rpm <= sp_hi:
            frac = (sp_rpm - sp_lo) / float(sp_hi - sp_lo)
            return spp_lo + frac * (spp_hi - spp_lo)
    return float(table[-1][1])  # unreachable, defensive fallback

def estimate_free_run_resolution_nm(sp_rpm) -> float:
    """Rough estimate of nm between consecutive Free Run samples at the
    given SP (rpm). See estimate_free_run_steps_per_poll()/
    FREE_RUN_SP_STEPS_PER_POLL for what this is (and isn't) based on --
    an order-of-magnitude GUI estimate, not a guarantee. Used by
    gui_main.py's live info label next to the Free Run speed field."""
    steps_per_poll = estimate_free_run_steps_per_poll(sp_rpm)
    return steps_per_poll / float(STEPS_PER_NM)

def set_current_wavelength(known_nm: float) -> bool:
    """OFFSET calibration: declare that the monochromator is RIGHT NOW sitting
    at a known wavelength (e.g. a reference lamp line the user has centred),
    and re-anchor the software's position tracking to it -- WITHOUT moving the
    motor.

    WHAT IT DOES / WHY: this whole system is purely relative -- there is no
    absolute position sensor, state['current_nm'] is only ever carried forward
    by commanded deltas from wherever it was last anchored (restored config or
    the 500.0 fallback). So if the reference line shows up at the wrong place,
    the ANCHOR is off, not necessarily the steps/nm scale. This corrects the
    anchor: it sets state['current_nm'] (and the GUI 'Current λ' field, the
    source of truth scan_engine reads for the next move's delta) to the known
    value. Pure re-labelling of the current position -- verifiably correct for
    a constant offset, and harmless (no motion, nothing else changes).

    WHAT IT IS NOT: this does NOT touch STEPS_PER_NM. If the error GROWS with
    distance from the anchor (a scale/slope error, not a constant offset), an
    offset reset alone won't fix it -- that needs a real two-point calibration
    (measure two known lines, derive steps/nm). That is deliberately NOT done
    here; see BACKLOG. Do a reset here, then check a second known line: if it's
    now correct everywhere, it was an offset; if it still drifts, it's scale.

    Returns True on success, False on invalid input.
    """
    try:
        nm = float(known_nm)
    except Exception:
        log("[CAL] Set-λ ignored: invalid wavelength.", "error")
        return False
    old = state.get('current_nm')
    state['current_nm'] = nm
    try:
        refs['entry_current'].delete(0, "end")
        refs['entry_current'].insert(0, f"{nm:.3f}")
    except Exception:
        pass
    try:
        old_txt = f"{float(old):.3f}" if old is not None else "unknown"
    except Exception:
        old_txt = "unknown"
    log(f"[CAL] Current position re-anchored: {old_txt} -> {nm:.3f} nm "
        f"(offset calibration; motor NOT moved, STEPS_PER_NM unchanged). "
        f"If a second known line still drifts, that's a scale error -- see BACKLOG.")
    return True

def timeout_for_nm(delta_nm: float) -> float:
    try:
        dist = abs(float(delta_nm))
    except Exception:
        return max(MIN_MOVE_TIMEOUT, TIMEOUT_OVERHEAD_S + 1.0)
    try:
        _dwell = float(ui_get('entry_wait', '0'))
    except Exception:
        _dwell = 0.0
    settle_overhead = min(5.0, 0.25 * _dwell)
    t = SEC_PER_NM * dist + TIMEOUT_OVERHEAD_S + settle_overhead
    if t < MIN_MOVE_TIMEOUT: t = MIN_MOVE_TIMEOUT
    if t > MAX_MOVE_TIMEOUT: t = MAX_MOVE_TIMEOUT
    return t

def timeout_for_target_steps(target_steps: int, fallback_nm) -> float:
    """
    Compute timeout based on *remaining* distance to target from current position.
    If anything fails, fall back to the provided nm estimate.
    """
    try:
        pos_now = read_position()
        dist_nm = abs((target_steps - pos_now) / float(STEPS_PER_NM))
        return timeout_for_nm(dist_nm)
    except Exception:
        try:
            return timeout_for_nm(abs(float(fallback_nm)))
        except Exception:
            return timeout_for_nm(0.0)

def check_soft_limits_nm(target_nm: float) -> bool:
    """Return True if target is inside optional software limits (with margin)."""
    try:
        mn = SOFT_LIMIT_MIN_NM
        mx = SOFT_LIMIT_MAX_NM
        margin = float(SOFT_LIMIT_MARGIN_NM)
        if mn is not None and target_nm < float(mn) + margin:
            log(f"[ERROR] Target λ {target_nm:.3f} nm < min limit {float(mn)+margin:.3f} nm. Move aborted.", "error")
            return False
        if mx is not None and target_nm > float(mx) - margin:
            log(f"[ERROR] Target λ {target_nm:.3f} nm > max limit {float(mx)-margin:.3f} nm. Move aborted.", "error")
            return False
    except Exception:
        pass
    return True

def postmove_status_audit(where: str = "") -> bool:
    """
    Read 0x6041 once after a *POS-confirmed* move.
    If FAULT or INTERNAL_LIMIT is set, stop scanning and return False.
    Never blocks motion timing; POS remains the source of truth.
    """
    if not USE_POSTMOVE_STATUSWORD_AUDIT:
        return True
    try:
        sw = _read_statusword()  # your existing SDO read; if it raises, we just continue
    except Exception as e:
        try:
            log(f"[AUDIT] 0x6041 read skipped: {e}")
        except Exception:
            pass
        return True

    bad = []
    if (sw & STATUS_FAULT_BIT):          bad.append("FAULT")
    if (sw & STATUS_INTERNAL_LIMIT_BIT): bad.append("INTERNAL_LIMIT")

    if bad:
        try:
            log(f"[ERROR] Post-move audit{(' ['+where+']') if where else ''}: {'+'.join(bad)} (SW=0x{sw:04X}). Stopping.", "error")
        except Exception:
            pass
        try:
            state["stop_flag"] = True
        except Exception:
            pass
        return False

    # Informational only — do NOT gate motion on TR
    if not (sw & STATUS_TR_BIT):
        try:
            log(f"[WARN] Post-move audit{(' ['+where+']') if where else ''}: TR=0 (SW=0x{sw:04X}) — POS ok; continuing.", "warn")
        except Exception:
            pass

    return True

def postmove_limit_audit(where: str = "") -> bool:
    """
    Use OST-only to detect FAULT or LIMIT conditions after a motion step.
    - Returns True if OK (no limit/fault), False if a limit/fault occurred.
    - Does NOT use bit-10 or any 0x6041 SDO reads.
    """
    if not USE_POSTMOVE_LIMIT_AUDIT:
        return True
    ost = read_ost()
    if ost is None:
        try:
            log("[AUDIT] OST read skipped: no response.")
        except Exception:
            pass
        return True  # don't fail the move if we can't audit
    if ost & (OST_FAULT_MASK | OST_HARD_LIMIT_MASK | OST_INTERNAL_LIMIT_MASK):
        try:
            log(f"[ERROR] Post-move limit/fault via OST{(' ['+where+']') if where else ''}: 0x{ost:04X}.", "error")
        except Exception:
            pass
        return False
    try:
        log(f"[AUDIT] Post-move OST OK: 0x{ost:04X}{(' ['+where+']') if where else ''}.")
    except Exception:
        pass
    return True

def post_move_statusword_audit(tag: str = "post-move") -> bool:
    """Passive 0x6041 audit after POS-confirmed success.
    Never used to decide success of the move itself; only logs and sets stop flag if severe bits are set.
    """
    if not POST_MOVE_STATUS_AUDIT:
        return True
    try:
        sw = _read_statusword()
    except Exception:
        # SDO not available is not an error; skip audit quietly
        log("[INFO] Statusword audit skipped (SDO unavailable).", "info")
        return True

    faults = []
    if sw & STATUS_FAULT_BIT: faults.append("FAULT")
    if sw & STATUS_INT_LIMIT_BIT: faults.append("INTERNAL_LIMIT")

    if faults:
        log(f"[ERROR] {tag}: drive reports {'+'.join(faults)}. Stopping queue/scan.", "error")
        try:
            state["stop_flag"] = True
        except Exception:
            pass
        return False
    return True

def _pos_wait_impl_fallback(target_steps: int, tol_steps: int, stable_polls: int,
                            total_timeout_s: float, t_start: Optional[float] = None,
                            paused_accum_init: float = 0.0) -> bool:
    """Existing POS-tolerance wait semantics, refactored as helper."""
    start = time.time() if t_start is None else t_start
    paused_accum = paused_accum_init
    stable = 0
    while (time.time() - start - paused_accum) < total_timeout_s:
        # Pause handling (freeze timeout while paused)
        if state.get("is_paused"):
            t_pause = time.time()
            while state.get("is_paused") and not state.get("stop_flag"):
                time.sleep(POLL_INTERVAL)
            paused_accum += (time.time() - t_pause)
            continue
        if state.get("stop_flag"):
            return False
        
        # OST guard (errors immediate; input changes debounced; baseline honoured)
        # LATENT BUG (carried over from the monolith, behaviour preserved 1:1):
        # last_ost / ost_baseline / ost_debounce are NOT initialized here.
        # The try block therefore raises NameError immediately and it is
        # swallowed by the except below -> this OST guard EFFECTIVELY NEVER
        # RUNS. The POS-tolerance part further below works normally. NOT
        # blindly "fixed", because that would activate a never-tested safety
        # check on untested hardware -- a deliberate fix with rig
        # verification, see BACKLOG.
        #
        # !! SECOND, INDEPENDENT REASON NOT TO JUST INITIALIZE THE VARIABLES !!
        # The input logic below is INVERTED for this rig. It computes
        #     new_edges = (status_bits ^ baseline) & status_bits
        # i.e. it reacts to an input bit going 0 -> 1 ("became active").
        # Measured on the rig (three read_faulhaber_config.py runs while
        # pressing each endstop, cross-checked against the manual's HP
        # definition, see device_constants.OST_ENDSTOP_ACTIVE_LOW):
        #     both endstops FREE  -> OST 0x0500 (bit8 AND bit10 SET)
        #     Input 1 PRESSED     -> OST 0x0400 (bit8 CLEARED)
        #     Input 3 PRESSED     -> OST 0x0100 (bit10 CLEARED)
        # An endstop being hit CLEARS its bit. So merely initializing the three
        # variables would produce a guard that ignores every real endstop hit
        # and instead aborts the move when a switch is RELEASED. Whoever
        # activates this must flip the comparison to detect 1 -> 0 first, and
        # then verify it on the rig by pressing a switch during a slow move.
        try:
            if (time.time() - last_ost) >= 0.10:
                last_ost = time.time()
                ost = read_ost(timeout=0.2)
                if ost is not None:
                    # 1) error bits -> immediate abort
                    if ost & OST_ERROR_MASK:
                        safe_stop()
                        log(f"[ERROR] OST abort: {_decode_ost_flags(ost)} (OST=0x{ost:04X})", "error")
                        return False
                    # 2) status inputs: only abort if NEW inputs became active vs baseline (debounced 3x)
                    status_bits = ost & OST_INPUT_MASK
                    new_edges = (status_bits ^ (ost_baseline & OST_INPUT_MASK)) & status_bits
                    if new_edges:
                        ost_debounce += 1
                        if ost_debounce >= 3:
                            safe_stop()
                            state['limit_blocked'] = True
                            names = []
                            if new_edges & OST_STATUS_INPUT1: names.append("IN1")
                            if new_edges & OST_STATUS_INPUT2: names.append("IN2")
                            if new_edges & OST_STATUS_INPUT3: names.append("IN3")
                            log(f"[LIMIT] Controller blocked: input change ({'+'.join(names)}) (OST=0x{ost:04X})", "info")
                            return False
                    else:
                        ost_debounce = 0
        except Exception:
            pass

        try:
            pos = read_position_retry()
            if abs(pos - int(target_steps)) <= int(tol_steps):
                stable += 1
                if stable >= int(stable_polls):
                    return True
            else:
                stable = 0
        except Exception:
            stable = 0
        time.sleep(POLL_INTERVAL)
    log(f"[TIMEOUT] Did not reach target {int(target_steps)} in {float(total_timeout_s):.1f}s", "warn")
    return False

def _wait_via_statusword_then_pos(target_steps: int,
                                  tol_steps: int,
                                  stable_polls: int,
                                  total_timeout_s: float) -> bool:
    """
    Robust CiA-402 wait:
      1) ARM: expect bit-12 ACK and bit-10 drop (or tolerate if already in window by POS).
      2) RUN: wait for bit-10 to rise and hold (debounce), optionally cross-check POS.
    Falls back to POS if SDO fails at any point.
    """
    start = time.time()
    paused_accum = 0.0

    # ARM phase — see ACK and TR drop
    saw_ack = False
    saw_tr_drop = False
    t0 = time.time()
    while time.time() - t0 < float(STATUS_ACK_ARM_TIMEOUT_S):
        if state.get("is_paused"):
            t_pause = time.time()
            while state.get("is_paused") and not state.get("stop_flag"):
                time.sleep(STATUS_POLL_S)
            paused_accum += (time.time() - t_pause)
        if state.get("stop_flag"):
            return False

        try:
            sw = _read_statusword()
        except Exception:
            # SDO not available → delegate to POS immediately
            return _pos_wait_impl_fallback(target_steps, tol_steps, stable_polls,
                                           total_timeout_s, start, paused_accum)

        if sw & STATUS_ACK_BIT:
            saw_ack = True
        if not (sw & STATUS_TR_BIT):
            saw_tr_drop = True
        if saw_ack and saw_tr_drop:
            break
        time.sleep(STATUS_POLL_S)

    # If TR never dropped, maybe we were already “in window”
    if not saw_tr_drop:
        try:
            if abs(read_position_retry() - int(target_steps)) <= int(tol_steps):
                return True
        except Exception:
            pass  # continue anyway

    # RUN phase — wait for TR high with debounce; cross-check POS
    tr_hold = 0
    while (time.time() - start - paused_accum) < float(total_timeout_s):
        if state.get("is_paused"):
            t_pause = time.time()
            while state.get("is_paused") and not state.get("stop_flag"):
                time.sleep(STATUS_POLL_S)
            paused_accum += (time.time() - t_pause)
        if state.get("stop_flag"):
            return False

        try:
            sw = _read_statusword()
        except Exception:
            # SDO flaked mid-run → fall back to POS
            return _pos_wait_impl_fallback(target_steps, tol_steps, stable_polls,
                                           total_timeout_s, start, paused_accum)

        if sw & STATUS_FAULT_BIT:
            log("[FAULT] Statusword shows FAULT during motion.", "warn")
            return False

        if sw & STATUS_TR_BIT:
            tr_hold += 1
            if tr_hold >= max(1, int(STATUS_TR_DEBOUNCE_POLLS)):
                # Optional POS sanity cross-check
                try:
                    if abs(read_position_retry() - int(target_steps)) <= int(tol_steps):
                        return True
                except Exception:
                    pass
                return True
        else:
            tr_hold = 0

        time.sleep(STATUS_POLL_S)

    log(f"[TIMEOUT] No TR within {float(total_timeout_s):.1f}s (Statusword path).", "warn")
    return False

def wait_until_position(target_steps: int, tol=POS_TOL_STEPS, stable_req=POS_STABLE_COUNT, timeout: Optional[float] = None) -> bool:
    """
    Pause-aware wait for target position.
    - Freezes timeout while paused (so Pause won't cause a timeout).
    - If `timeout` is None, compute it dynamically from remaining distance.
    """
    # Dynamic timeout if not provided
    if timeout is None:
        try:
            pos0 = read_position()
            remaining_nm = abs((int(target_steps) - pos0) / float(STEPS_PER_NM))
            timeout = timeout_for_nm(remaining_nm)
        except Exception:
            timeout = max(MIN_MOVE_TIMEOUT, TIMEOUT_OVERHEAD_S + 1.0)

    # Route: Statusword primary (if enabled), else POS fallback. Always pause-aware.
    if USE_STATUSWORD:
        try:
            return _wait_via_statusword_then_pos(
                target_steps=int(target_steps),
                tol_steps=int(tol),
                stable_polls=int(stable_req),
                total_timeout_s=float(timeout),
            )
        except Exception as e:
            log(f"[WARN] Statusword path failed ({e}); falling back to POS.", "warn")

    return _pos_wait_impl_fallback(
        target_steps=int(target_steps),
        tol_steps=int(tol),
        stable_polls=int(stable_req),
        total_timeout_s=float(timeout),
    )

def _get_slip_nm() -> float:
    """Manually entered backlash magnitude (nm) from the 'Slip / Backlash'
    GUI field. Mechanically measured, entered by the user. The VALUE is
    persisted across restarts via app_config (the GUI writes it there on
    edit and pre-fills the field from it); read here live from the field so
    an in-session change takes effect immediately."""
    try:
        return max(0.0, float(ui_get('slip_nm_var', '0')))
    except Exception:
        return 0.0

def _update_direction_label():
    """Refresh the small GUI status label showing the currently known
    'seated' direction, so it's obvious at a glance whether a Reference Run
    is needed before trusting compensation."""
    try:
        d = state.get('last_move_direction')
        if d is None:
            txt = "unknown \u2013 run Reference Run first"
        elif d < 0:
            txt = "\u2190 (negative)"
        else:
            txt = "\u2192 (positive)"
        ui_hook('set_direction_label', f"Direction: {txt}")
    except Exception:
        pass

def reversal_compensation_steps(new_direction: int) -> int:
    """
    Return EXTRA raw steps (same sign as the upcoming move) to fold into a
    real move's LR command, to take up mechanical backlash -- but ONLY if the
    upcoming move direction differs from the last known seated direction
    (state['last_move_direction']). Same-direction moves add nothing, because
    once the slack is taken up in a direction, it stays taken up until the
    direction reverses again.

    Call this once per move, right before building the LR command, passing
    the direction (+1/-1) that move is about to travel in. It ALWAYS updates
    state['last_move_direction'] = new_direction as a side effect, so it is
    safe (and expected) to call on every single step of a scan -- it is a
    no-op (extra=0) on every step except the one where direction actually
    flips.

    If no Reference Run has been run yet this session (last_move_direction is
    still None), this logs a warning and returns 0 -- the move proceeds
    UNCOMPENSATED rather than guessing a direction.
    """
    new_direction = 1 if new_direction >= 0 else -1
    last = state.get('last_move_direction')
    extra = 0
    if last is None:
        log("[SLIP] Direction unknown -- run 'Reference Run' first for correct backlash compensation.", "warn")
    elif last != new_direction:
        slip_nm = _get_slip_nm()
        if slip_nm > 0:
            extra = steps_for_delta_nm(slip_nm * new_direction)
            log(f"[SLIP] Reversal {last:+d}->{new_direction:+d}: adding {slip_nm:.3f} nm ({extra:+d} steps) backlash compensation.")
    state['last_move_direction'] = new_direction
    _update_direction_label()
    return extra

def reference_run_action(on_done=None):
    """
    One-shot 'take up the backlash' move, meant to be run once per session
    (after Connect, before the first Goto/Scan). Drives REFERENCE_MOVE_NM nm
    in the fixed REFERENCE_DIRECTION -- a short move, NOT a full hardware
    limit-switch home (that takes ~10 min on this rig and is impractical for
    routine use). After it completes, state['last_move_direction'] is set to
    REFERENCE_DIRECTION, so every later move that reverses relative to it
    automatically gets the slip compensation from reversal_compensation_steps().

    Safe to repeat any time during a session if you want to re-establish a
    known direction state (e.g. after an interrupted move).

    on_done, if given, is called EXACTLY ONCE after the move finishes
    (success or failure/interruption) as on_done(reached: bool). It is
    marshalled onto the GUI thread via ui_hook('ui_after', ...) -- same
    mechanism the rest of the GUI-facing code uses -- so it's safe to open a
    dialog or touch widgets directly from it. Used by gui_main.py's
    post-connect "Reference Run? -> confirm current λ" workflow.
    """
    if state["is_scanning"]:
        ui_hook('show_warning', "Busy", "Cannot run reference move while scanning.")
        log("[REF] Ignored: scan running.", "warn")
        return
    # A reference run is a real motor move, so it must not overlap a Goto/Jog
    # either -- state['is_moving'] is the flag those set (see
    # scan_engine.goto_wavelength_action and the app_context.py comment).
    if state.get("is_moving"):
        ui_hook('show_warning', "Busy", "A move is already in progress.")
        log("[REF] Ignored: move already in progress.", "warn")
        return
    if state["ser"] is None or not getattr(state["ser"], "is_open", False):
        ui_hook('show_error', "Error", "Not connected.")
        log("[REF] Ignored: not connected.", "warn")
        return

    def _worker():
        set_fsm("MOVING")
        reached = False
        try:
            ui_hook('set_reference_button_enabled', False)
            init_motor()
            current_steps = read_position()
            delta_nm = REFERENCE_MOVE_NM * REFERENCE_DIRECTION
            rel_steps = steps_for_delta_nm(delta_nm)
            target_steps = current_steps + rel_steps
            state['current_step_target_abs'] = target_steps
            log(f"[REF] Reference run: {delta_nm:+.3f} nm ({rel_steps:+d} steps) to seat backlash.")
            send_cmd(f"LR{rel_steps}"); send_cmd("M")
            reached = wait_until_position(target_steps, timeout=timeout_for_target_steps(target_steps, delta_nm))
            if SAFE_AFTER_MOVE:
                safe_stop()
            if reached:
                # This move DEFINES the known direction state for the session.
                state['last_move_direction'] = REFERENCE_DIRECTION
                _update_direction_label()
                # The reference move is a REAL motor move (REFERENCE_MOVE_NM in
                # REFERENCE_DIRECTION), not a no-op -- state['current_nm'] and
                # the GUI 'Current λ' field must move with it, same as every
                # other successful move in scan_engine.py. Without this update,
                # state['current_nm']/entry_current would silently drift by
                # REFERENCE_MOVE_NM off the real position on every Reference
                # Run, corrupting the next Goto/Scan target.
                state['current_nm'] = float(state.get('current_nm', 0.0)) + delta_nm
                try:
                    refs['entry_current'].delete(0, "end")
                    refs['entry_current'].insert(0, f"{state['current_nm']:.3f}")
                except Exception:
                    pass
                log(f"[REF] Reference run complete. Direction seated at {REFERENCE_DIRECTION:+d}. Ready.")
            else:
                log("[REF] Reference run interrupted -- direction state NOT set, repeat before relying on compensation.", "warn")
        except Exception as e:
            log(f"[REF] Reference run failed: {e}", "error")
        finally:
            # Clear the busy flag first: stop_action()'s background worker
            # waits on it before re-arming the drive, so it must drop as soon
            # as this move is really over (see where it is set, below).
            state['is_moving'] = False
            try:
                ui_hook('set_reference_button_enabled', True)
            except Exception:
                pass
            if state.get("fsm") == "MOVING":
                set_fsm("IDLE")
            # callable() rather than `is not None`: connecting this function
            # straight to a Qt clicked signal hands on_done the button's
            # `checked` bool, which is not None but is not callable either.
            # The call site is fixed, but a wrong value must not be able to
            # take down the end of a completed move.
            if on_done is not None and not callable(on_done):
                log(f"[REF] on_done is not callable ({type(on_done).__name__}); "
                    f"ignoring it. (Connecting reference_run_action directly to a "
                    f"Qt signal does this -- use a no-arg lambda.)", "warn")
            elif on_done is not None:
                try:
                    ui_hook('ui_after', lambda: on_done(reached))
                except Exception as e:
                    log(f"[REF] on_done callback failed: {e}", "warn")

    # Without setting a busy flag here, stop_action()'s was_running check
    # would see neither is_scanning nor is_moving during a Reference Run, so
    # it would skip its wait entirely and re-arm the drive (clearing
    # stop_flag) before _worker's wait_until_position() has necessarily
    # noticed the Stop -- the same race that state['is_moving'] closes for
    # Goto (see scan_engine.stop_action's docstring).
    # Set on the caller's (GUI) thread, before the thread starts, so the
    # busy check above and this assignment cannot be interleaved by a second
    # click. Cleared in _worker's finally block on every exit path.
    state['is_moving'] = True
    threading.Thread(target=_worker, daemon=True).start()
