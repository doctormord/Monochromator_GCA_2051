"""
sim_hardware.py — Virtual FAULHABER controller for testing WITHOUT hardware.

WHAT THIS MODULE DOES
---------------------
Provides FakeFaulhaber: an object that behaves like a pyserial handle and speaks
enough of the FAULHABER ASCII protocol that the whole stack
(protocol_faulhaber -> motion -> scan_engine) runs without a real motor.

WHERE/WHEN it is used
---------------------
gui_main.on_connect() injects a FakeFaulhaber instance into state['ser'] when the
port is entered as "SIM". From then on ser_ok() is True and all motion runs
against the simulated motor. The DAQ side is simulated anyway (daq.read_pmt_
voltage falls back to a shaped noise value without an NI driver), so a full scan
plots points and exports CSV entirely in software.

WHY a separate module (not in daq/motion)
-----------------------------------------
protocol_faulhaber/motion/daq are deliberately left untouched by the Qt lift. The
simulator only hooks in at the serial seam (state['ser']), exactly where pyserial
would sit — no logic module needs to know anything about the simulation.

Supported protocol (ASCII, CR-terminated), as protocol_faulhaber expects it:
  POS      -> current absolute position in steps (ASCII integer)
  LR<n>    -> queue a relative target (signed delta in steps)
  M        -> execute the queued move (position += delta, immediately)
  OST      -> operational status; here 0 (no fault/limit bits) -> audits pass
  EN/HP0/V0/ST/HP -> acknowledged (empty line)
Binary SDO frames (0x6041 statusword) are NOT answered -> the caller falls back
to the POS path automatically (USE_STATUSWORD is False anyway).
"""

import re
import threading

from device_constants import MOTOR_ANSW_MODE, SIM_BACKLASH_NM, STEPS_PER_NM


class FakeFaulhaber:
    """pyserial-compatible virtual FAULHABER drive (see module docstring).

    Implements only the subset of the pyserial API used by protocol_faulhaber:
    is_open, timeout, reset_input_buffer, reset_output_buffer, write, flush,
    read_until, read, close. Replies are produced SYNCHRONOUSLY on write() and
    stored in an internal RX buffer that read_until()/read() then drain — in the
    same order send_cmd() works (reset -> write -> read_until)."""

    def __init__(self, start_steps: int = 0):
        self.is_open = True
        self.timeout = 1.0
        self._pos = int(start_steps)      # ENCODER position (steps) -- what POS reports
        self._pending = 0                 # delta queued by LR until the next M
        # --- mechanical model (see device_constants.SIM_BACKLASH_NM) ---
        # The encoder counts every commanded step. The GRATING only follows
        # once the slack in the drive train has been taken up, so after a
        # direction reversal the first _backlash_steps of travel move the
        # encoder but not the optics. _optical_pos is therefore what the
        # simulated PMT signal must be derived from -- deriving it from the
        # app's own current_nm (the old behaviour) made backlash structurally
        # invisible in SIM.
        self._backlash_steps = int(round(abs(SIM_BACKLASH_NM) * STEPS_PER_NM))
        self._optical_pos = int(start_steps)
        self._last_dir = 0                # +1 / -1 / 0 = unknown
        self._slack = 0                   # slack currently taken up, in steps
        self._tx = b""                    # accumulated, not-yet-terminated input
        self._rx = b""                    # prepared reply(s) for read_until/read
        self._lock = threading.Lock()

    # --- pyserial buffer API (no-ops / clear RX) -----------------------
    @property
    def in_waiting(self):
        """Bytes currently sitting in the RX buffer, like pyserial.

        NEEDED BY protocol_faulhaber._drain_late_reply(), which only flushes
        when something is actually waiting (so it never cuts a reply in half).
        Without this attribute the fake port looked permanently empty and the
        drain step was skipped."""
        with self._lock:
            return len(self._rx)

    def reset_input_buffer(self):
        """Discard incoming (RX) data. send_cmd calls this BEFORE write(); the
        reply is produced afterwards, so nothing is lost."""
        with self._lock:
            self._rx = b""

    def reset_output_buffer(self):
        """Discard outgoing (TX) data (unfinished command line)."""
        with self._lock:
            self._tx = b""

    def flush(self):
        return None

    def close(self):
        self.is_open = False

    # --- writing: parse command, prepare reply -------------------------
    def write(self, data: bytes):
        """Accept ASCII command(s). Once a full CR ('\\r') is present the command
        is evaluated and its reply is placed in the RX buffer. Binary SDO frames
        (start with 'S') are ignored -> no reply -> caller falls back to POS."""
        with self._lock:
            self._tx += bytes(data)
            # Do not treat a binary SDO frame ('S'...'E') as ASCII.
            if self._tx[:1] == b"S":
                self._tx = b""
                return len(data)
            while b"\r" in self._tx:
                line, self._tx = self._tx.split(b"\r", 1)
                resp = self._handle(line.decode("ascii", errors="ignore").strip())
                self._rx += resp
        return len(data)

    def _apply_move(self, delta: int):
        """Advance the encoder by `delta` and the optics by however much of it
        actually reaches the grating.

        Slack model: `_slack` is how much of the backlash is currently taken
        up in the CURRENT direction of travel. Moving on in the same direction
        costs nothing extra. Reversing empties the slack, so the first
        `_backlash_steps` of the new direction are absorbed before the optics
        start to follow again. Partial reversals are handled correctly because
        the slack is consumed proportionally, not all-or-nothing.

        The result is what a real drive does: POS advances by the full delta
        (the encoder sees every step), the grating lags by up to one backlash
        width, and the lag only appears when the direction changes."""
        delta = int(delta)
        self._pos += delta
        if delta == 0:
            return
        if self._backlash_steps <= 0:
            self._optical_pos += delta
            return
        d = 1 if delta > 0 else -1
        if d != self._last_dir:
            # Direction changed: the slack has to be re-taken-up from zero.
            self._slack = 0
            self._last_dir = d
        remaining = abs(delta)
        take = min(remaining, self._backlash_steps - self._slack)
        if take > 0:
            self._slack += take        # absorbed: encoder moves, optics don't
            remaining -= take
        if remaining > 0:
            self._optical_pos += d * remaining

    def optical_lag_steps(self) -> int:
        """How far the grating currently lags the encoder, in steps.
        Consumed by daq._simulated_pmt_voltage() so the fake spectrum is a
        function of where the OPTICS are, not of what the app believes."""
        return self._optical_pos - self._pos

    def _handle(self, cmd: str) -> bytes:
        """Execute one ASCII command and return its (CR+LF-terminated) reply
        as bytes -- matching the real drive's protocol (manual DE_7000_00029,
        4.2: every reply is CR followed by LF). Unknown commands are simply
        acknowledged.

        WHY THE LF MATTERS FOR SIM FIDELITY: without it, SIM could never have
        caught the "every command pays a bogus late-reply drain" bug found at
        the rig via idle_poll_test.py (protocol_faulhaber._consume_trailing_lf)
        -- there would have been no LF for the fix to consume, and no LF left
        behind for the old code to (mis)trigger on either. Sending it here
        keeps SIM able to catch this class of protocol-shape bug in the
        future instead of only ever seeing it at the rig."""
        if not cmd:
            return b"" if MOTOR_ANSW_MODE == 0 else b"\r\n"
        up = cmd.upper()
        if up == "POS":
            return f"{self._pos}\r\n".encode("ascii")
        m = re.match(r"LR\s*(-?\d+)", up)
        if m:
            self._pending = int(m.group(1))
            return b"\r\n"
        if up == "M":
            # Execute immediately (POS tolerance is large; no ramping needed).
            self._apply_move(self._pending)
            self._pending = 0
            return b"\r\n"
        if up == "OST":
            # Operational status with no fault/limit bits -> postmove audit OK.
            return b"0\r\n"
        # EN, HP0, V0, ST, HP and anything else.
        # In silent answer mode (MOTOR_ANSW_MODE == 0, which is what the rig's
        # drive is actually set to -- confirmed via CST) the drive does NOT
        # acknowledge set commands at all. Emitting a bare CR here made the
        # simulator UNfaithful: those stray acks piled up in the RX buffer
        # (send_cmd no longer reads a reply for fire-and-forget commands) and
        # were then returned as the answer to the next POS query, which showed
        # up as spurious RESYNC timeouts in SIM only.
        if MOTOR_ANSW_MODE == 0:
            return b""
        return b"\r\n"

    # --- reading: from the prepared RX buffer --------------------------
    def read_until(self, expected: bytes = b"\r"):
        """Read up to and including 'expected' from the RX buffer. For ASCII
        (expected == CR) this returns the prepared reply. For SDO (expected ==
        b'S') there is nothing -> b'' (caller times out)."""
        with self._lock:
            idx = self._rx.find(expected)
            if idx == -1:
                out, self._rx = self._rx, b""
                return out
            end = idx + len(expected)
            out, self._rx = self._rx[:end], self._rx[end:]
            return out

    def read(self, n: int = 1):
        """Read up to n bytes from the RX buffer (only used by the SDO path;
        usually empty here)."""
        with self._lock:
            out, self._rx = self._rx[:n], self._rx[n:]
            return out
