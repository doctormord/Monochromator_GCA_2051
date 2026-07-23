"""
protocol_faulhaber.py — Serial communication with the FAULHABER controller.

WHAT THIS MODULE DOES
---------------------
The complete low-level wire to the motion controller: ASCII commands (EN, HP0,
V0, ST, POS, OST, LR, M) AND the binary SDO micro-client (CRC8-D5 frames) for
reading objects such as the CiA-402 statusword (0x6041). Knows NOTHING about
the GUI and NOTHING about wavelengths/nm -- only steps, bytes, and controller
replies.

Dependencies: app_context (state/log/set_fsm/ui_hook) + device_constants.
Serial access always goes through state['lock'], so ASCII and SDO traffic
never interleave.

Origin: extracted from the monolith monochromator_python_code_vrs41.py
(function bodies unchanged; only the GUI coupling in recover_after_stop was
switched to ui_hook, and the constant imports were adjusted).

IMPORTANT (doc policy, see HANDOVER.md): docstrings/comments are preserved.
"""

import time
import re

from app_context import state, log, set_fsm, ui_hook
from device_constants import (
    RESYNC_ON_TIMEOUT, WRITE_SETTLE_S, POLL_TIMEOUT,
    STATUSWORD_NODE,
    OST_POSITION_ATTAINED, OST_CURRENT_LIMIT_ACTIVE, OST_DEVIATION_ERROR,
    OST_OVERVOLTAGE, OST_OVERTEMPERATURE,
    OST_STATUS_INPUT1, OST_STATUS_INPUT2, OST_STATUS_INPUT3,
    LOG_OST_VERBOSE, OST_LOG_THROTTLE_S,
    USE_MOTION_RAMP, RAMP_AC, RAMP_DEC, RAMP_SP,
    MOTOR_ANSW_MODE, NO_REPLY_COMMANDS, SEND_LEGACY_HP0,
    REQUIRE_CR_TERMINATED_REPLY, SERIAL_RECOVERY_SETTLE_S,
    REPLY_GAP_TOLERANCE_S,
)

# OST log tracking in state (survives across calls, see read_ost).
state.setdefault("last_ost_log_ts", 0.0)
state.setdefault("last_ost_value", None)


def ser_ok():
    return state["ser"] is not None and state["ser"].is_open

_CMD_PREFIX_RE = re.compile(r"^([A-Za-z]+)")


def _is_no_reply_cmd(cmd: str) -> bool:
    """True if `cmd` is a SET command that the drive does not acknowledge in
    silent answer mode (MOTOR_ANSW_MODE == 0), i.e. waiting for a reply after
    it would always run into the full serial timeout.

    HOW: matches the command's leading LETTER prefix against
    device_constants.NO_REPLY_COMMANDS, so "LR-180882" -> "LR", "AC50" -> "AC",
    "HP0" -> "HP". Longest-prefix logic is not needed because the letter run
    IS the command name in this ASCII protocol (the rest is the argument).

    WHEN FALSE: query commands (POS/OST/G*) always take the normal
    write-then-read path -- they DO answer even in silent mode, so a missing
    reply there is a genuine fault and must still trigger RESYNC.

    If the drive is NOT in silent mode (MOTOR_ANSW_MODE != 0), this returns
    False for everything and the old behaviour is used unchanged -- acks are
    then expected and read normally."""
    if MOTOR_ANSW_MODE != 0:
        return False
    m = _CMD_PREFIX_RE.match(cmd.strip())
    if not m:
        return False
    return m.group(1).upper() in NO_REPLY_COMMANDS


def _drain_late_reply(s):
    """Discard a reply that is still arriving from a PREVIOUS command, without
    cutting it in half.

    WHY THIS REPLACED A BARE reset_input_buffer():
    send_cmd used to flush the input buffer unconditionally right before
    writing the next command. If a late reply from the previous query was
    still STREAMING IN at that moment, the flush discarded only the bytes that
    had arrived so far -- the remaining digits landed in the buffer afterwards
    and were then read as if they were the answer to the NEW query. At 9600
    baud an 8-digit POS reply takes ~9 ms to clock in, so the window is real.

    That is what the rig log shows: rejected POS replies of '1' and '103862'
    where the true position was a 7-8 digit number -- fragments of a position,
    NOT stale-but-valid old readings.

    TWO TRIGGERS, because checking the buffer alone is not enough:
      1. Bytes already waiting -> a late reply has STARTED arriving. Wait one
         settle period so the rest lands, then discard the whole thing. A
         complete stale reply thrown away cleanly is harmless; half a reply
         is poison.
      2. state['serial_dirty'] -> the PREVIOUS read timed out or came back
         truncated, so a reply is owed to us but may not have started arriving
         yet (in_waiting == 0 at this instant proves nothing). Wait the longer
         recovery settle, then drain. This is the deterministic half of the
         fix: it targets the exact sequence that produces fragments (read
         times out -> reply arrives late -> next write flushes it mid-stream)
         instead of hoping to catch it in the act."""
    dirty = bool(state.get("serial_dirty"))
    try:
        n = getattr(s, "in_waiting", None)
        if n is None:
            # Port object doesn't expose in_waiting (some stubs/adapters):
            # fall back to the old unconditional flush rather than silently
            # skipping the drain, which would let stale bytes through.
            s.reset_input_buffer()
            state["serial_dirty"] = False
            return
        if dirty:
            time.sleep(max(0.0, float(SERIAL_RECOVERY_SETTLE_S)))
            s.reset_input_buffer()
            state["serial_recovery_drains"] = state.get("serial_recovery_drains", 0) + 1
            state["serial_dirty"] = False
        elif n:
            time.sleep(max(0.0, float(WRITE_SETTLE_S)))
            s.reset_input_buffer()
            state["late_reply_drains"] = state.get("late_reply_drains", 0) + 1
    except Exception:
        # Never let buffer bookkeeping break a command.
        try:
            s.reset_input_buffer()
            state["serial_dirty"] = False
        except Exception:
            pass


def _read_reply_until_cr(s, total_deadline_s, gap_s):
    """Read one CR-terminated reply, tolerating GAPS between bytes.

    Returns (raw_bytes, complete: bool).

    WHY NOT serial.read_until(): pyserial 3.5's read_until() builds ONE
    Timeout for the whole call and -- more importantly -- breaks out of its
    loop the FIRST time read(1) comes back empty:

        c = self.read(1)
        if c: ...
        else: break            # <-- one gap and the reply is abandoned

    So a reply that merely PAUSES mid-transmission is thrown away, and what
    is handed back is the leading fragment that arrived before the pause.
    That is exactly the rig signature: 'POS' replies truncated to '768',
    '752', '734', ... -- the leading digits of the live position, tracking
    the sweep monotonically, 2-5 characters each. At 9600 baud a character
    takes ~1 ms, so these were never bandwidth-limited: the drive simply
    stopped mid-message for longer than the read window.

    This reader instead keeps going until it sees CR or the TOTAL deadline
    expires, allowing individual gaps of up to gap_s. A reply that is only
    slow now completes instead of being discarded, while a reply that is
    genuinely lost still ends (bounded by total_deadline_s)."""
    buf = bytearray()
    t_end = time.monotonic() + max(0.0, float(total_deadline_s))
    try:
        s.timeout = max(0.01, float(gap_s))
    except Exception:
        pass
    while time.monotonic() < t_end:
        try:
            c = s.read(1)
        except Exception:
            break
        if not c:
            continue          # a gap, not the end -- keep waiting
        buf += c
        if buf.endswith(b"\r"):
            return bytes(buf), True
    return bytes(buf), False


def _consume_trailing_lf(s, timeout_s):
    """Consume the LF that FOLLOWS every reply's CR, per the manual
    (DE_7000_00029, 4.2): "Antwort ... CR ... LF" -- confirmed at the rig via
    idle_poll_test.py, where in a 120s/2401-poll idle-only run every single
    poll reported a "late reply drain" (late_reply_drains == polls exactly).
    That was never a real anomaly: _read_reply_until_cr() stops AT the CR and
    never reads the LF that the drive sends immediately after it, so that one
    byte sat in the OS buffer until the NEXT command's _drain_late_reply()
    found it, mistook it for a late/stray reply, and paid WRITE_SETTLE_S to
    clean it up -- on every single command in the app, not just POS polls.

    This reads (and discards) exactly one byte, with a short timeout, right
    after a successful CR-terminated read. If it's LF, gone, silently -- this
    is expected, not a fault, so it does NOT touch truncated_replies/
    late_reply_drains/serial_dirty. If a short wait produces nothing, that's
    fine too (a slow link may not have the LF queued yet; the ordinary
    _drain_late_reply path on the next command still cleans it up, just less
    often now). If something OTHER than LF shows up, that's genuinely
    unexpected -- flag the stream dirty so the next command's drain settles
    and clears it rather than risk it bleeding into a subsequent read."""
    try:
        s.timeout = max(0.0, float(timeout_s))
        c = s.read(1)
    except Exception:
        return
    if not c or c == b"\n":
        return
    try:
        log(f"[SERIAL] Unexpected byte after CR: {c!r} (expected LF per manual "
            f"4.2) -- flagging stream for cleanup.", "warn")
    except Exception:
        pass
    state["serial_dirty"] = True


def send_cmd(cmd: str, timeout: float = 1.0) -> str:
    """Send ASCII cmd with CR and read CR-terminated reply. Clean, quiet.

    Two paths:
      * NO-REPLY commands (see _is_no_reply_cmd): written fire-and-forget,
        return "" immediately. In silent answer mode these are never
        acknowledged, so reading would just block for the full `timeout` and
        then fire a bogus "[RESYNC] Timed out" -- about 1.2 s of dead time per
        command, roughly a dozen per Go To.
      * everything else (queries: POS/OST/G*): write-then-read with RESYNC on
        a genuine timeout.

    REPLY INTEGRITY: a reply is only returned if it was actually terminated by
    CR. pyserial's read_until() returns whatever it managed to read when it
    times out, WITHOUT the terminator -- so a slow/partial reply used to be
    handed upward as a shorter, perfectly parseable number. For POS that means
    a truncated position silently becomes a real-looking position. Such a read
    is now treated as a failure (empty return -> RESYNC), which is what it is.
    """
    if not ser_ok():
        return ""
    if not cmd.endswith("\r"):
        cmd += "\r"
    with state["lock"]:
        s = state["ser"]
        try:
            _drain_late_reply(s)
            s.reset_output_buffer()
            s.write(cmd.encode("ascii"))
            s.flush()
            if _is_no_reply_cmd(cmd):
                # Fire-and-forget: no acknowledgment exists to wait for.
                # Keep the short settle so the drive isn't hit by the next
                # command in the same instant (same purpose WRITE_SETTLE_S
                # already served after the resync write).
                time.sleep(max(0.0, float(WRITE_SETTLE_S)))
                return ""
            raw_bytes, complete = _read_reply_until_cr(
                s, total_deadline_s=timeout, gap_s=REPLY_GAP_TOLERANCE_S)
            if complete:
                # The manual (4.2) guarantees every reply is CR THEN LF; our
                # reader stops at CR, so the LF is still sitting right behind
                # it. Consume it now, as part of THIS read, instead of
                # leaving it for the next command's _drain_late_reply() to
                # misidentify as a stray/late reply (confirmed at the rig:
                # idle_poll_test.py showed late_reply_drains == poll count,
                # i.e. every single command was "cleaning up" this same
                # expected byte).
                _consume_trailing_lf(s, timeout_s=REPLY_GAP_TOLERANCE_S)
            # Completeness check: no trailing CR == the reply never finished.
            # Discard rather than parse a fragment (see docstring).
            if REQUIRE_CR_TERMINATED_REPLY and raw_bytes and not complete:
                try:
                    log(f"[SERIAL] Incomplete reply to {cmd.strip()!r} "
                        f"({raw_bytes!r}) -- no CR within {timeout:.2f}s, discarded.", "warn")
                except Exception:
                    pass
                state["truncated_replies"] = state.get("truncated_replies", 0) + 1
                state["serial_dirty"] = True
                raw_bytes = b""
            raw = raw_bytes.decode(errors="ignore").strip()
            if not raw:
                # Nothing usable came back. The drive may still owe us a reply
                # that arrives at any moment -- flag the stream so the NEXT
                # command settles and drains before writing, instead of
                # cutting that late reply in half (see _drain_late_reply).
                state["serial_dirty"] = True
            # RESYNC on timeout/empty response (optional).
            #
            # DOES NOT RE-QUERY ANY MORE. It used to write an extra "POS" and
            # throw the answer away. On a link that is already failing to
            # deliver complete replies that is the worst possible response:
            # the 2026-07-23 rig log shows one such extra write after EVERY
            # truncation (127 of them in a single slow sweep), i.e. the
            # recovery step doubled the traffic exactly when the link was
            # struggling, and each extra reply became the next command's
            # problem. Draining and letting the caller retry is enough --
            # _drain_late_reply() + serial_dirty already handle the orphan.
            if (not raw) and RESYNC_ON_TIMEOUT:
                try:
                    log(f"[RESYNC] No complete reply to {cmd.strip()!r}; draining "
                        f"(no re-query -- see protocol_faulhaber.send_cmd).")
                except Exception:
                    pass
                try:
                    s.reset_input_buffer(); s.reset_output_buffer()
                except Exception:
                    pass
            return raw
        except Exception as e:
            log(f"[SERIAL ERROR] {e}", "error")
            return ""

def _decode_ost_flags(ost: int) -> str:
    parts = []
    if ost & OST_POSITION_ATTAINED: parts.append("POS_ATTAINED")
    if ost & OST_CURRENT_LIMIT_ACTIVE: parts.append("CUR_LIMIT")
    if ost & OST_DEVIATION_ERROR: parts.append("DEVIATION")
    if ost & OST_OVERVOLTAGE: parts.append("OVERVOLT")
    if ost & OST_OVERTEMPERATURE: parts.append("OVERTEMP")
    if ost & OST_STATUS_INPUT1: parts.append("IN1")
    if ost & OST_STATUS_INPUT2: parts.append("IN2")
    if ost & OST_STATUS_INPUT3: parts.append("IN3")
    return "|".join(parts) if parts else "—"

def read_ost(timeout: float = 0.5):
    """
    Read FAULHABER Operational Status (OST) via ASCII "OST" command.
    Returns an int or None if no response/parse error.
    """
    try:
        raw = send_cmd("OST", timeout=timeout) or ""
        # Extract first integer in the reply (decimal or hex)
        import re as _re
        m = _re.search(r"(0x[0-9A-Fa-f]+|\d+)", raw)
        if not m:
            return None
        token = m.group(1)
        val = int(token, 16) if token.lower().startswith("0x") else int(token)
        # Throttled OST log with decoded flags
        try:
            import time as _t
            now_ts = _t.time()
            last_ts = state.get("last_ost_log_ts", 0.0)
            last_val = state.get("last_ost_val", None)
            if (now_ts - last_ts) >= 0.5 or (last_val is None or int(last_val) != int(val)):
                try:
                    log(f"[OST] 0x{val:04X} ({_decode_ost_flags(val)})")
                except Exception:
                    pass
                state["last_ost_log_ts"] = now_ts
                state["last_ost_val"] = int(val)
        except Exception:
            pass
        return val
    except Exception:
        return None

def configure_ramp(ac=None, dec=None, sp=None):
    """Apply acceleration/deceleration ramp and max speed via the FAULHABER
    AC/DEC/SP ASCII commands (RS232 manual 7000.00029, Kap. 3.8.1 + 7.1.3).
    The ramp generator applies to position mode (LA/LR/M) as well as
    velocity mode -- it is NOT specific to V/V0.

    Any of ac/dec/sp left as None is skipped (not sent), so a partial call
    only touches the values given. Safe to call with all-None (no-op).
    Values are NOT range-checked here beyond what the device itself rejects
    -- pick them at the rig, start conservative (see device_constants.py:
    USE_MOTION_RAMP / RAMP_AC / RAMP_DEC / RAMP_SP)."""
    if ac is not None:
        send_cmd(f"AC{int(ac)}")
    if dec is not None:
        send_cmd(f"DEC{int(dec)}")
    if sp is not None:
        send_cmd(f"SP{int(sp)}")

def drain_stale_serial(ser, settle_s=0.3, max_bytes=4096):
    """One-time cleanup right after OPENING the port, BEFORE any command is
    sent: passively absorb and discard whatever is already sitting in the
    OS/driver RX buffer (or arrives within `settle_s`), and LOG it if
    non-empty.

    WHY THIS EXISTS: a stray old reply -- left over from a previous session,
    from another tool (Motion Manager, a terminal) that had the port open
    earlier and whose driver-level buffer wasn't fully cleared, or a queued
    ANSW-mode acknowledgment from drive power-on -- can otherwise sit there
    and get read as part of the FIRST real command's reply, corrupting it.
    This does NOT write anything to the drive -- pure listen-then-discard.

    Reports exactly what it found (repr of raw bytes) so a stale-buffer
    theory can be CONFIRMED or RULED OUT by what actually shows in the log
    on the next occurrence, instead of guessed. See BACKLOG.md: the ~587 nm
    repeating-spike investigation."""
    try:
        ser.reset_input_buffer()
        time.sleep(max(0.0, float(settle_s)))
        n = getattr(ser, "in_waiting", 0)
        if n:
            data = ser.read(min(n, max_bytes))
            log(f"[CONNECT] Drained {len(data)} stale byte(s) from the serial "
                f"buffer before the first command: {data!r}. If this keeps "
                f"happening on every connect, something is queuing replies "
                f"independent of our own commands (see BACKLOG.md).", "warn")
        else:
            log("[CONNECT] Serial buffer clean before first command (no stale bytes).")
    except Exception as e:
        log(f"[CONNECT] Could not check for stale serial data: {e}", "warn")

def init_motor():
    send_cmd("EN")
    # "HP0" is NOT a motion command -- it writes the LIMIT SWITCH polarity
    # bitmask (Hard Polarity) on the drive. See device_constants.
    # SEND_LEGACY_HP0 for the full manual reference and the power-cycle
    # experiment that decides whether the historical behaviour ever mattered.
    # Default: not sent.
    if SEND_LEGACY_HP0:
        send_cmd("HP0")
    send_cmd("V0")
    # Answer mode. Value comes from device_constants.MOTOR_ANSW_MODE (None =
    # leave the drive's current mode alone). Default 0 = silent: the app's
    # whole protocol layer already assumes the drive only ever replies to what
    # was just asked and never volunteers anything, and silent mode is what
    # makes that assumption true. The manual documents the Motion-Manager
    # DEFAULT as NOT silent; this app previously sent no ANSW at all and simply
    # inherited whatever was last configured on the drive.
    #
    # COST OF SILENT MODE, and why it must be paired with NO_REPLY_COMMANDS:
    # in ANSW0 the drive does not acknowledge SET commands (EN/HP0/V0/AC/DEC/
    # SP/LR/M/ST). When this line was first added, send_cmd still waited for a
    # reply after each of them -> full timeout + resync probe (~1.2 s) per
    # command, ~14 s of dead time per Go To, and a wall of misleading
    # "[RESYNC] Timed out" lines on the rig. send_cmd now recognises those
    # commands and writes them fire-and-forget instead (see _is_no_reply_cmd).
    # ANSW is RAM-only here (no SAVE/EEPSAV), so it stays set on the drive
    # until power-cycle -- a mode set in one session is still active in the
    # next one.
    if MOTOR_ANSW_MODE is not None:
        send_cmd(f"ANSW{int(MOTOR_ANSW_MODE)}")
    if USE_MOTION_RAMP:
        configure_ramp(RAMP_AC, RAMP_DEC, RAMP_SP)

def safe_stop():
    """Bring the drive to a controlled halt: stop the move, then park it in
    velocity mode at 0 rpm (which also releases the position controller).

    "HP0" used to sit between the two. It is a limit-switch polarity
    CONFIGURATION write, not a stop command -- see
    device_constants.SEND_LEGACY_HP0. It is only sent if that flag is on."""
    send_cmd("ST")
    if SEND_LEGACY_HP0:
        send_cmd("HP0")
    send_cmd("V0")

def recover_after_stop():
    """Re-arm the drive/UI after STOP so Jog/Go work immediately."""
    try:
        state['current_step_target_abs'] = None
        state['stop_flag'] = False
        state['is_paused'] = False
        state['is_scanning'] = False
        if ser_ok():
            s = state['ser']
            try:
                s.reset_input_buffer(); s.reset_output_buffer()
            except Exception:
                pass
            # HP0 only if explicitly re-enabled -- it is a limit-switch
            # polarity config write, not a recovery command (see
            # device_constants.SEND_LEGACY_HP0).
            _seq = ("ST", "EN", "HP0", "V0") if SEND_LEGACY_HP0 else ("ST", "EN", "V0")
            for cmd in _seq:
                try: send_cmd(cmd)
                except Exception: pass
        set_fsm("IDLE")
        ui_hook('set_buttons_scanning', False)
        ui_hook('set_buttons_connected', state.get("connected", False))
        log("[OK] Recovered after STOP; ready.")
    except Exception as e:
        log(f"[WARN] Recover failed: {e}", "warn")

def read_temperature():
    """Read the drive's housing temperature via the ASCII "TEM" command.
    Returns an int in degrees Celsius, or None on a missing/garbled reply.

    WHAT THIS VALUE ACTUALLY IS (manual DE_7000_00029, 7.5 / 3.8.4):
    TEM is the "Gehäusetemperatur" -- the HOUSING temperature measured inside
    the unit, not a separate motor sensor. This rig's drive reports
    GTYP = 'CS-BX4', i.e. one of the integrated units on the manual's cover
    (32xx...BX4 CS), where motor and electronics share that housing -- so for
    THIS hardware the housing temperature does track the motor's housing.
    The winding is hotter than the housing: the drive derives coil/MOSFET
    temperature from this reading plus its current measurement via an internal
    thermal model, and that model (not this number) is what drives the
    overtemperature cut-out (OST bit 7, OST_OVERTEMPERATURE). So treat TEM as
    a useful trend/plausibility indicator, NOT as a winding temperature.

    Read-only, no side effects. Costs one serial round-trip, so callers should
    poll it slowly and not while a move is being polled (see the GUI's idle-
    only refresh)."""
    try:
        raw = send_cmd("TEM", timeout=POLL_TIMEOUT) or ""
        m = re.search(r"(-?\d+)", raw)
        if not m:
            return None
        return int(m.group(1))
    except Exception:
        return None


def read_position() -> int:
    """Read absolute steps via POS (controller-specific)."""
    try:
        return int(send_cmd("POS", timeout=POLL_TIMEOUT) or "0")
    except Exception as e:
        return 0

def read_position_or_none():
    """Like read_position(), but returns None on a failed/garbled reply
    instead of silently returning 0.

    WHY THIS EXISTS (separate from read_position(), which is NOT changed
    here): read_position() intentionally never raises -- most callers
    (wait_until_position's POS-tolerance loop, timeout_for_target_steps,
    the Goto/Scan delta math) only ever use the returned int for a
    tolerance/equality check against a target. A stray 0 there just looks
    like "not arrived yet" and self-heals on the next poll -- harmless.

    A caller that plots/uses the raw returned position DIRECTLY (e.g.
    scan_engine.free_run_scan_action, which turns every POS poll straight
    into a plotted wavelength via motion.steps_to_delta_nm) cannot tolerate
    that: a transient serial timeout silently becomes a plausible-looking
    position of exactly 0 steps, which is then plotted as a wild, wrong
    wavelength jump -- this is what produced the zigzag/garbage traces in
    a free-run plot. This variant lets such a caller detect and SKIP a
    failed read instead of trusting a fabricated zero."""
    val, _raw = read_position_raw_or_none()
    return val

def read_position_raw_or_none():
    """Same as read_position_or_none(), but also returns the RAW string the
    drive actually sent, before parsing -- (parsed_int_or_None, raw_str).

    WHY: when scan_engine's free-run outlier filter rejects an impossible
    POS jump, the previous version only logged "rejected", discarding the
    exact bytes that caused it. That's not enough to diagnose WHERE a wrong
    value like the repeating ~587 nm spike (BACKLOG.md) actually came from
    -- a garbled/partial reply looks very different from a stale-but-valid
    OLD reply from several polls ago. Keeping the raw string lets the
    outlier path log it verbatim for forensic comparison next time it
    happens, instead of just recording that *a* rejection occurred."""
    try:
        raw = send_cmd("POS", timeout=POLL_TIMEOUT)
        if not raw:
            return None, raw
        return int(raw), raw
    except Exception:
        return None, None

def read_position_retry(n=3, dt=0.05):
    """Read position with short retries to avoid transient errors."""
    last = None
    for _ in range(max(1, int(n))):
        try:
            return read_position()
        except Exception as e:
            last = e
            time.sleep(dt)
    # last attempt (let it raise if still failing)
    return read_position()

def _crc8_d5(payload_bytes: bytes) -> int:
    crc = 0xFF
    for b in payload_bytes:
        crc ^= b
        for _ in range(8):
            crc = ((crc >> 1) ^ 0xD5) if (crc & 1) else (crc >> 1)
    return crc & 0xFF

def _frame_sdo_read(node: int, index: int, sub: int) -> bytes:
    idx_lb, idx_hb = index & 0xFF, (index >> 8) & 0xFF
    user = bytes([7, node & 0xFF, 0x01, idx_lb, idx_hb, sub & 0xFF])
    crc = _crc8_d5(user)
    return b"S" + user + bytes([crc]) + b"E"

def _read_sdo_value(index: int, sub: int, node: int, timeout_s=0.3) -> int:
    """Poll one SDO value (unsigned LE int) from 0xXXXX.sub. Raises on timeout."""
    if not ser_ok():
        raise TimeoutError("No serial")
    s = state["ser"]
    frame = _frame_sdo_read(node, index, sub)
    deadline = time.time() + float(timeout_s)
    # Use the same serial lock as ASCII commands to avoid interleaving
    with state["lock"]:
        try:
            s.reset_input_buffer()
            s.reset_output_buffer()
            s.timeout = timeout_s
            s.write(frame); s.flush()
            # Parse frames until we see SDORead response for (index,sub)
            while time.time() < deadline:
                # sync to 'S'
                got = s.read_until(b"S")
                if not got or got[-1:] != b"S":
                    continue
                length = s.read(1)
                if not length:
                    continue
                n = length[0]
                payload = s.read(n)
                eof = s.read(1)
                if len(payload) != n or eof != b"E":
                    continue
                # CRC check
                if _crc8_d5(bytes([n]) + payload[:-1]) != payload[-1]:
                    continue
                node_rx, cmd = payload[0], payload[1]
                if cmd != 0x01:   # not an SDORead response; ignore async frames
                    continue
                idx_lb, idx_hb, sub_rx = payload[2], payload[3], payload[4]
                if (((idx_hb << 8) | idx_lb) != index) or (sub_rx != sub):
                    continue
                raw = payload[5:-1]  # value bytes (1..4 bytes LE)
                return int.from_bytes(raw, "little", signed=False)
        except Exception as e:
            raise
    raise TimeoutError(f"No SDO response for 0x{index:04X}.{sub:02X}")

def _read_statusword() -> int:
    return _read_sdo_value(0x6041, 0x00, STATUSWORD_NODE, timeout_s=0.3)
