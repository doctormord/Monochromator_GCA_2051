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

from device_constants import (
    MOTOR_ANSW_MODE, SIM_BACKLASH_NM, STEPS_PER_NM,
    SIM_STEPS_PER_S_PER_RPM, SIM_MAX_STEPS_PER_S, SIM_POLL_LATENCY_S,
)
import time as _time


def _is_printable_ascii(buf: bytes) -> bool:
    """True if `buf` looks like an ASCII command line rather than a binary
    frame. Only the part up to the first CR is judged, since that is what the
    dispatcher will hand to _handle(); anything beyond it belongs to the next
    command."""
    head = buf.split(b"\r", 1)[0]
    if not head:
        return True
    return all(32 <= b <= 126 for b in head)


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
        # --- motion takes TIME (see device_constants.SIM_STEPS_PER_S_PER_RPM)
        # A move is not applied instantly: _move_total is what "M" was asked
        # to travel, _move_done how much of it has been handed to
        # _apply_move() so far. POS advances as real time passes, so a Free
        # Run can actually sample a sweep in progress.
        self._sp_rpm = 10000.0            # last SP command; drives the speed
        self._move_total = 0              # signed steps still to travel
        self._move_done = 0               # signed steps already travelled
        self._move_t0 = 0.0
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
            # Do not treat a binary SDO frame ('S'...'E') as ASCII -- but do
            # not throw away every command that merely STARTS with 'S'.
            #
            # This used to be `if self._tx[:1] == b"S"`, which silently
            # discarded the ASCII commands "SP<n>" (speed) and "ST" (stop),
            # because both begin with S. Consequences in SIM: a Free Run speed
            # override had no effect at all -- sweeps took the same time at
            # 2000 rpm as at 10000 -- and Stop did not stop the simulated
            # drive. Neither was visible before, because the simulator used to
            # execute moves instantly, so speed and stopping were meaningless.
            #
            # An SDO frame is BINARY; an ASCII command is printable. Deciding
            # on that rather than on the first letter keeps real SDO frames out
            # while letting SP/ST/SR/SHA/... through.
            if self._tx[:1] == b"S" and not _is_printable_ascii(self._tx):
                self._tx = b""
                return len(data)
            while b"\r" in self._tx:
                line, self._tx = self._tx.split(b"\r", 1)
                resp = self._handle(line.decode("ascii", errors="ignore").strip())
                self._rx += resp
        return len(data)

    def _advance_time(self):
        """Move the drive along by however much real time has passed.

        Called before answering POS (and before starting a new move), so the
        position a client reads is the position at the moment it asked --
        exactly how a real drive behaves. Speed comes from the last SP
        command, converted with the rig-derived factor and capped, so a
        simulated sweep takes about as long as the real one."""
        if self._move_total == 0:
            return
        sps = min(SIM_MAX_STEPS_PER_S,
                  max(1.0, abs(self._sp_rpm) * SIM_STEPS_PER_S_PER_RPM))
        elapsed = _time.monotonic() - self._move_t0
        should = int(sps * elapsed)                      # magnitude travelled
        total = abs(self._move_total)
        should = min(should, total)
        delta = should - abs(self._move_done)
        if delta <= 0:
            return
        sgn = 1 if self._move_total > 0 else -1
        self._apply_move(sgn * delta)
        self._move_done += sgn * delta
        if abs(self._move_done) >= total:                # arrived
            self._move_total = 0
            self._move_done = 0

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
            # Cost a little time, like a real round trip -- otherwise a Free
            # Run in SIM samples orders of magnitude faster than the rig.
            if SIM_POLL_LATENCY_S > 0:
                _time.sleep(SIM_POLL_LATENCY_S)
            self._advance_time()      # position as of NOW, like a real drive
            return f"{self._pos}\r\n".encode("ascii")
        m = re.match(r"LR\s*(-?\d+)", up)
        if m:
            self._pending = int(m.group(1))
            return b"\r\n"
        if up == "M":
            # Start a move that takes TIME (see _advance_time). Finish any
            # move still running first, so a new target is measured from
            # where the drive actually is.
            self._advance_time()
            if self._move_total:
                self._apply_move(self._move_total - self._move_done)
            self._move_total = int(self._pending)
            self._move_done = 0
            self._move_t0 = _time.monotonic()
            self._pending = 0
            return b"\r\n"
        if up == "OST":
            # Operational status with no fault/limit bits -> postmove audit OK.
            return b"0\r\n"
        m = re.match(r"SP\s*(-?\d+)", up)
        if m:
            # Remember the commanded speed -- _advance_time() derives the
            # simulated travel rate from it, so a Free Run speed override
            # changes the sweep duration in SIM just as it does on the rig.
            self._sp_rpm = abs(int(m.group(1))) or 1
            return b"" if MOTOR_ANSW_MODE == 0 else b"\r\n"
        if up == "ST":
            # Stop where we are: apply whatever has been travelled so far and
            # abandon the rest, instead of silently completing the move.
            self._advance_time()
            self._move_total = 0
            self._move_done = 0
            self._pending = 0
            return b"" if MOTOR_ANSW_MODE == 0 else b"\r\n"
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
