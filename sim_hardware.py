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
        self._pos = int(start_steps)      # simulated absolute position (steps)
        self._pending = 0                 # delta queued by LR until the next M
        self._tx = b""                    # accumulated, not-yet-terminated input
        self._rx = b""                    # prepared reply(s) for read_until/read
        self._lock = threading.Lock()

    # --- pyserial buffer API (no-ops / clear RX) -----------------------
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

    def _handle(self, cmd: str) -> bytes:
        """Execute one ASCII command and return its (CR-terminated) reply as
        bytes. Unknown commands are simply acknowledged."""
        if not cmd:
            return b"\r"
        up = cmd.upper()
        if up == "POS":
            return f"{self._pos}\r".encode("ascii")
        m = re.match(r"LR\s*(-?\d+)", up)
        if m:
            self._pending = int(m.group(1))
            return b"\r"
        if up == "M":
            # Execute immediately (POS tolerance is large; no ramping needed).
            self._pos += self._pending
            self._pending = 0
            return b"\r"
        if up == "OST":
            # Operational status with no fault/limit bits -> postmove audit OK.
            return b"0\r"
        # EN, HP0, V0, ST, HP and anything else: acknowledge.
        return b"\r"

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
