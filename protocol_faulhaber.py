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
)

# OST log tracking in state (survives across calls, see read_ost).
state.setdefault("last_ost_log_ts", 0.0)
state.setdefault("last_ost_value", None)


def ser_ok():
    return state["ser"] is not None and state["ser"].is_open

def send_cmd(cmd: str, timeout: float = 1.0) -> str:
    """Send ASCII cmd with CR and read CR-terminated reply. Clean, quiet."""
    if not ser_ok():
        return ""
    if not cmd.endswith("\r"):
        cmd += "\r"
    with state["lock"]:
        s = state["ser"]
        try:
            s.reset_input_buffer()
            s.reset_output_buffer()
            s.write(cmd.encode("ascii"))
            s.flush()
            s.timeout = timeout
            raw = s.read_until(b"\r").decode(errors="ignore").strip()
            # RESYNC on timeout/empty response (optional)
            if (not raw) and RESYNC_ON_TIMEOUT:
                try:
                    log(f"[RESYNC] Timed out on {cmd.strip()!r}; draining and probing POS.")
                except Exception:
                    pass
                try:
                    s.reset_input_buffer(); s.reset_output_buffer()
                    time.sleep(max(0.0, float(WRITE_SETTLE_S)))
                    s.write(b"POS\r"); s.flush()
                    s.timeout = max(0.2, float(POLL_TIMEOUT))
                    _ = s.read_until(b"\r")  # ignore content
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

def init_motor():
    send_cmd("EN")
    send_cmd("HP0")
    send_cmd("V0")
    if USE_MOTION_RAMP:
        configure_ramp(RAMP_AC, RAMP_DEC, RAMP_SP)

def safe_stop():
    send_cmd("ST")
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
            for cmd in ("ST","EN","HP0","V0"):
                try: send_cmd(cmd)
                except Exception: pass
        set_fsm("IDLE")
        ui_hook('set_buttons_scanning', False)
        ui_hook('set_buttons_connected', state.get("connected", False))
        log("[OK] Recovered after STOP; ready.")
    except Exception as e:
        log(f"[WARN] Recover failed: {e}", "warn")

def read_position() -> int:
    """Read absolute steps via POS (controller-specific)."""
    try:
        return int(send_cmd("POS", timeout=POLL_TIMEOUT) or "0")
    except Exception as e:
        return 0

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
