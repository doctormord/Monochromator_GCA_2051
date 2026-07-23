#!/usr/bin/env python3
"""
idle_poll_test.py — Standalone idle-POS-poll diagnostic.

WHAT THIS TESTS, AND WHY IT'S THE DECISIVE EXPERIMENT
-------------------------------------------------------
RIG RESULT (2026-07-23, 120s @ ~20 Hz, 2401 polls): 0 truncated/failed reads
at rest. CONCLUSION: the cause is MOTION-CORRELATED (EMI/grounding/cable
routing), not the serial link or driver itself -- see BACKLOG.md for the
follow-up electrical checks. The rest of this docstring is kept as the
general-purpose explanation of the test for future incidents (a different
symptom might point the other way).

The 2026-07-23 rig log showed truncated POS replies during Free Run sweeps,
at a rate that rose sharply as the sweep got SLOWER (10000/5000 rpm: 0%,
1000 rpm: 7.9%, 500 rpm: 24%). That rules out "too much data too fast", but
it does NOT by itself say whether the cause is:

  (a) motion-correlated -- e.g. electrical noise (EMI) from the motor/drive
      onto the signal wiring, worse at low speed/high current, or some other
      effect that only exists while the motor is actually moving; or
  (b) NOT motion-correlated -- a USB-serial adapter/driver issue (e.g. an
      FTDI latency timer, buffering behaviour, a flaky cable/connector) that
      stalls mid-reply independent of whether the motor is turning at all.

This script settles that by polling POS at ~20 Hz while the motor does
NOTHING -- connected, enabled, completely idle -- for a couple of minutes,
using the EXACT same code path the app uses (protocol_faulhaber.send_cmd,
including the 2026-07-23 fixes: gap-tolerant reply reading, CR-completeness
check, serial_dirty recovery). It is deliberately NOT a reimplementation
(unlike probe_move_timing.py, which has its own separate read_pos()) --
we need to know what THIS code, on THIS link, does at rest.

HOW TO READ THE RESULT
-----------------------
  - (near) zero truncated/failed reads at rest, but many during a slow
    sweep -> motion-correlated. Next steps are electrical (cable routing,
    shielding, grounding, ferrite on the serial cable), not code.
  - Truncated/failed reads happen at rest too, at a similar or even higher
    rate -> the link/driver itself is unreliable independent of motion.
    Next steps: try a different USB port/cable, check the adapter's chipset
    and (on Windows, FTDI-based adapters) its Latency Timer setting in
    Device Manager -> Port Settings -> Advanced.

WHAT THIS SCRIPT DELIBERATELY DOES NOT DO
------------------------------------------
- Never sends a motion command (no LR/M/V/EN-with-nonzero-speed). It DOES
  send EN once via protocol_faulhaber.init_motor() up front, matching the
  app's own connect sequence, so the drive is in the same electrical/logic
  state it would be in right before a real move -- just not moving.
- Never calls SAVE/EEPSAV.
- No GUI import (app_context has none either) -- safe to run standalone,
  same philosophy as tune_ramp.py / probe_move_timing.py.

USAGE
-----
    python idle_poll_test.py COM3
    python idle_poll_test.py COM3 --baud 9600 --duration 120 --rate 20
    python idle_poll_test.py COM3 --duration 300 --csv idle_poll.csv

Prints a running counter every 10 s and a summary at the end. With --csv,
writes one row per poll (index, elapsed_s, dt_s, ok, raw) for later
inspection (e.g. to see if failures cluster in time).
"""

import argparse
import csv as csv_module
import sys
import time

import serial

from app_context import state, log
import protocol_faulhaber as P


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("port", help="Serial port, e.g. COM3 or /dev/tty.usbserial-XXXX")
    ap.add_argument("--baud", type=int, default=9600)
    ap.add_argument("--duration", type=float, default=120.0,
                     help="How long to poll, in seconds (default: 120 = 2 min)")
    ap.add_argument("--rate", type=float, default=20.0,
                     help="Target poll rate in Hz (default: 20, matching the "
                          "app's measured Free Run poll rate)")
    ap.add_argument("--csv", type=str, default=None,
                     help="Optional path to write a per-poll CSV log")
    args = ap.parse_args()

    interval = 1.0 / max(0.1, args.rate)

    print(f"Connecting to {args.port} @ {args.baud} baud...")
    ser = serial.Serial(args.port, args.baud, timeout=1.0)
    state["ser"] = ser
    state["connected"] = True

    try:
        # Same sequence the app runs right after opening the port, so the
        # drive/link is in the same state a real move would start from --
        # just never actually told to move.
        P.drain_stale_serial(ser)
        P.init_motor()

        pos0 = P.read_position_or_none()
        if pos0 is None:
            print("[WARN] Could not get an initial POS reading -- check the "
                  "connection before trusting this run's numbers.")
        else:
            print(f"Initial POS: {pos0} steps (motor will NOT be moved during this test)")

        rows = []
        n_total = n_ok = n_fail = 0
        t_start = time.monotonic()
        t_next = t_start
        t_last_report = t_start
        print(f"\nPolling POS at ~{args.rate:.0f} Hz for {args.duration:.0f} s "
              f"(idle -- motor not moving)...")
        print(f"{'elapsed':>8s}  {'polls':>6s}  {'ok':>6s}  {'failed':>6s}  {'fail%':>7s}")

        while True:
            now = time.monotonic()
            elapsed = now - t_start
            if elapsed >= args.duration:
                break
            if now < t_next:
                time.sleep(t_next - now)
            t_poll = time.monotonic()
            val, raw = P.read_position_raw_or_none()
            dt = time.monotonic() - t_poll
            n_total += 1
            ok = val is not None
            if ok:
                n_ok += 1
            else:
                n_fail += 1
            if args.csv:
                rows.append((n_total, round(time.monotonic() - t_start, 4),
                             round(dt, 4), int(ok), raw))
            t_next += interval

            if time.monotonic() - t_last_report >= 10.0:
                t_last_report = time.monotonic()
                pct = (100.0 * n_fail / n_total) if n_total else 0.0
                print(f"{elapsed:8.1f}  {n_total:6d}  {n_ok:6d}  {n_fail:6d}  {pct:6.1f}%")

        pct = (100.0 * n_fail / n_total) if n_total else 0.0
        print(f"\n{'='*60}")
        print(f"RESULT: {n_total} polls in {args.duration:.0f}s, "
              f"{n_ok} ok, {n_fail} failed/truncated ({pct:.1f}%)")
        print(f"  truncated_replies counter : {state.get('truncated_replies', 0)}")
        print(f"  late_reply_drains counter : {state.get('late_reply_drains', 0)}")
        print(f"  serial_recovery_drains    : {state.get('serial_recovery_drains', 0)}")
        print(f"{'='*60}")
        if n_fail == 0:
            print("No failures at rest. If a slow sweep still shows failures, "
                  "this points AWAY from the code and TOWARD something that "
                  "only happens while the motor is actually moving (EMI, "
                  "grounding, cable routing near the motor/drive).")
        else:
            print("Failures occurred with the motor completely idle. This "
                  "points AWAY from motion/EMI and TOWARD the serial link or "
                  "driver itself (USB-serial adapter chipset, cable, or -- on "
                  "Windows with an FTDI-based adapter -- its Latency Timer "
                  "setting under Device Manager -> Port Settings -> "
                  "Advanced).")

        if args.csv:
            with open(args.csv, "w", newline="") as f:
                w = csv_module.writer(f)
                w.writerow(["poll", "elapsed_s", "dt_s", "ok", "raw"])
                w.writerows(rows)
            print(f"\nPer-poll log written to {args.csv}")

    finally:
        try:
            P.safe_stop()
        except Exception:
            pass
        state["connected"] = False
        ser.close()
        print("\nDisconnected. Nothing was moved, nothing was saved to the drive.")


if __name__ == "__main__":
    main()
