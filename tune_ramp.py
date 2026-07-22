#!/usr/bin/env python3
"""
tune_ramp.py — Standalone FAULHABER ramp/speed diagnostic (read + optional test).

WHAT THIS SCRIPT DOES
----------------------
Reads the CURRENTLY ACTIVE acceleration/deceleration ramp and max speed off
the drive (GAC/GDEC/GSP -- FAULHABER RS232 manual 7000.00029, Kap. 7.2.1).
These are real, live values -- whatever was set at commissioning time or the
factory default, NOT anything this project's main app has ever sent (it
never has, until you enable USE_MOTION_RAMP in device_constants.py).

Optionally (--set), it applies a candidate AC/DEC/SP via AC<n>/DEC<n>/SP<n>
(Kap. 3.8.1 + 7.1.3) and runs one small back-and-forth test move (a few
hundred steps) so you can watch/listen to the mechanism before deciding.

WHAT THIS SCRIPT DELIBERATELY DOES NOT DO
------------------------------------------
- Never calls SAVE/EEPSAV. Anything set here is RAM-only and reverts on the
  next power cycle. Persisting to EEPROM is a separate, deliberate decision
  (and the manual caps EEPSAV at ~10 000 writes over the drive's lifetime --
  treat it as precious). If you want a value permanent, do that yourself
  once you've picked it, or put it in device_constants.py (RAMP_AC/RAMP_DEC/
  RAMP_SP + USE_MOTION_RAMP=True) so the app applies it in RAM every connect
  -- same effect, no EEPROM wear.
- Never guesses a "safe" starting number. It reads what's ACTUALLY active
  first; the test move (if you use --set) starts from there, one nudge at a
  time, not from an invented constant.
- Independent of the rest of the codebase (no app_context/gui_main import)
  so it is genuinely simple to read and safe to run stand-alone.

USAGE
-----
    python3 tune_ramp.py COM3
        -> just reads and prints the current AC/DEC/SP (and a few related
           controller parameters) — completely read-only, zero motion.

    python3 tune_ramp.py COM3 --set ac=800 dec=800 sp=3000 --move 2000
        -> applies AC800/DEC800/SP3000 (RAM only), then moves +2000 steps
           and back -2000 steps so you can observe the result, then reads
           GAC/GDEC/GSP back to confirm what actually took.

Requires only pyserial (already a project dependency).
"""

import sys
import time
import argparse

import serial


def send(ser, cmd: str, timeout: float = 1.0) -> str:
    """Send one ASCII command (CR-terminated) and return the CR-terminated
    reply, stripped. Minimal, no resync/retry logic -- this is a diagnostic
    tool, not the production protocol layer (see protocol_faulhaber.py for
    that)."""
    ser.reset_input_buffer()
    ser.reset_output_buffer()
    ser.write((cmd + "\r").encode("ascii"))
    ser.flush()
    ser.timeout = timeout
    raw = ser.read_until(b"\r").decode(errors="ignore").strip()
    return raw


def read_int(ser, cmd: str, label: str, unit: str = ""):
    raw = send(ser, cmd)
    try:
        val = int(raw)
        print(f"  {label:32s} ({cmd:10s}) = {val}{unit}")
        return val
    except Exception:
        print(f"  {label:32s} ({cmd:10s}) = <no/invalid reply: {raw!r}>")
        return None


def read_current_config(ser):
    print("\n--- Currently active on the drive (read-only, GAC/GDEC/GSP etc.) ---")
    ac = read_int(ser, "GAC", "Acceleration (AC)", " [1/s^2]")
    dec = read_int(ser, "GDEC", "Deceleration (DEC)", " [1/s^2]")
    sp = read_int(ser, "GSP", "Max speed (SP)", " [rpm]")
    print("--- For reference, related controller gains (not touched here) ---")
    read_int(ser, "GPOR", "Velocity P-gain (POR)")
    read_int(ser, "GI", "Velocity I-gain (I)")
    read_int(ser, "GPP", "Position P-gain (PP)")
    read_int(ser, "GPD", "Position D-gain (PD)")
    return ac, dec, sp


def apply_and_test(ser, ac, dec, sp, move_steps):
    print("\n--- Applying candidate values (RAM only, NOT saved to EEPROM) ---")
    if ac is not None:
        send(ser, f"AC{int(ac)}")
        print(f"  sent AC{int(ac)}")
    if dec is not None:
        send(ser, f"DEC{int(dec)}")
        print(f"  sent DEC{int(dec)}")
    if sp is not None:
        send(ser, f"SP{int(sp)}")
        print(f"  sent SP{int(sp)}")

    print("\n--- Read-back to confirm what the drive actually accepted ---")
    read_current_config(ser)

    if move_steps:
        print(f"\n--- Test move: +{move_steps} then -{move_steps} steps "
              f"(watch/listen to the mechanism now) ---")
        send(ser, "EN")
        send(ser, "HP0")
        send(ser, f"LR{int(move_steps)}")
        send(ser, "M")
        time.sleep(2.0)
        send(ser, f"LR{-int(move_steps)}")
        send(ser, "M")
        time.sleep(2.0)
        send(ser, "V0")
        print("  done.")

    print("\nNothing was written to EEPROM. Power-cycling the drive reverts "
          "these values to whatever was active before this script ran. If "
          "you like the result, put it in device_constants.py "
          "(RAMP_AC/RAMP_DEC/RAMP_SP + USE_MOTION_RAMP=True) so the app "
          "applies it every connect -- no need to touch SAVE/EEPSAV at all.")


def parse_set(s: str) -> dict:
    """Parse 'ac=800 dec=800 sp=3000' (any subset, any order) into a dict."""
    out = {}
    for tok in s.split():
        if "=" not in tok:
            continue
        k, v = tok.split("=", 1)
        k = k.strip().lower()
        if k in ("ac", "dec", "sp"):
            out[k] = int(v)
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("port", help="Serial port, e.g. COM3 or /dev/tty.usbserial-XXXX")
    p.add_argument("--baud", type=int, default=9600)
    p.add_argument("--set", type=str, default=None,
                   help="e.g. --set \"ac=800 dec=800 sp=3000\" (any subset)")
    p.add_argument("--move", type=int, default=0,
                   help="steps for the optional test move (0 = no move)")
    args = p.parse_args()

    print(f"Connecting to {args.port} @ {args.baud} baud...")
    ser = serial.Serial(args.port, args.baud, timeout=1.0)
    time.sleep(0.3)

    try:
        read_current_config(ser)
        if args.set:
            vals = parse_set(args.set)
            if not vals:
                print("\n--set given but nothing parsed -- expected e.g. "
                      "\"ac=800 dec=800 sp=3000\"")
            else:
                apply_and_test(ser, vals.get("ac"), vals.get("dec"), vals.get("sp"),
                               args.move)
        else:
            print("\n(Read-only run. Add --set \"ac=.. dec=.. sp=..\" "
                  "[--move 2000] to try new values with a small test move.)")
    finally:
        ser.close()


if __name__ == "__main__":
    main()
