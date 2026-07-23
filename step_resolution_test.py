#!/usr/bin/env python3
"""
step_resolution_test.py -- How small a scan step can this rig actually resolve,
and how tight can POS_TOL_STEPS safely be?

THE QUESTION THIS ANSWERS
--------------------------
device_constants.POS_TOL_STEPS is a FIXED 0.01 nm (3617 steps on this rig). It
is the "close enough, we've arrived" band used by motion.wait_until_position().
Because it does not scale with the scan step, it stops being a tolerance and
starts being the dominant error as soon as the step gets fine:

    0.1   nm step -> tolerance is 0.1x the step   (fine)
    0.01  nm step -> tolerance is 1.0x the step   (marginal)
    0.001 nm step -> tolerance is 10x  the step   (broken: a move counts as
                                                   arrived before it has
                                                   meaningfully started)

The hardware is NOT the limit here. From the rig's own config dump
(read_faulhaber_config.py): the drive's position corridor is GCORRIDOR = 20
steps, i.e. it settles ~181x finer than the app's 3617-step tolerance, and one
encoder count is ~2.8 femtometres of wavelength. A 0.001 nm step is 362 steps,
18x the drive's own corridor -- comfortably inside what the servo can hold.

So the useful question is not "can the drive do it" but "how tight can the
tolerance be before wait_until_position() starts timing out on real hardware".
That is a measurement, not a guess, and this script makes it.

WHAT IT DOES
------------
For each requested step size, it performs N single relative steps and, for
each one, records:
  * the settling curve: how long until the position is within each candidate
    tolerance band,
  * where the move ACTUALLY comes to rest (final error vs the commanded
    target, in steps and pm),
  * whether it would have TIMED OUT at each candidate tolerance.

From that it prints, per step size, the tightest candidate tolerance that
still succeeded on every move -- which is the number to put into
POS_TOL_STEPS (with margin).

It uses the app's own protocol layer (protocol_faulhaber), so the numbers
include the real serial polling behaviour, not an idealised model.

SAFETY
------
Moves are small and RELATIVE, alternating direction is OFF by default so the
test does not fight backlash (use --alternate to include reversals). Total
travel is printed up front. Nothing is written to the drive's EEPROM. The
drive is left stopped (V0) at the end.

USAGE
-----
    python step_resolution_test.py COM3
    python step_resolution_test.py COM3 --steps 0.01 0.001 0.0001 --moves 10
    python step_resolution_test.py COM3 --moves 20 --csv steps.csv
    python step_resolution_test.py COM3 --alternate      # include reversals
"""

import argparse
import csv as csv_module
import sys
import time

import serial

from app_context import state
import protocol_faulhaber as P
from device_constants import (
    STEPS_PER_NM, POS_TOL_STEPS, POLL_TIMEOUT, POS_STABLE_COUNT,
)


# Candidate tolerances to evaluate, in STEPS. 20 is the drive's own
# GCORRIDOR; 3617 is what the app uses today (0.01 nm).
CANDIDATE_TOL_STEPS = [20, 50, 100, 200, 400, 800, 1800, 3617]


def pm(steps):
    """Convert a step count to picometres of wavelength."""
    return steps / STEPS_PER_NM * 1000.0


def settle_profile(target_abs, max_wait_s, poll_s=0.02):
    """Poll POS until it stops changing or max_wait_s expires.

    Returns (samples, stable_within) where
      samples       = [(t_since_command, pos, err_steps), ...]
      stable_within = {tol_steps: t_when_STABLY_reached or None}

    "Stably reached" means POS_STABLE_COUNT CONSECUTIVE readings inside the
    band -- deliberately the same rule motion.wait_until_position() uses.
    Counting a single transient touch instead would be misleading: a move that
    overshoots sweeps briefly through every band on its way past, so the
    tightest band would always look reachable even when the drive comes to
    rest far outside it.
    """
    t0 = time.monotonic()
    samples = []
    stable_within = {t: None for t in CANDIDATE_TOL_STEPS}
    consec = {t: 0 for t in CANDIDATE_TOL_STEPS}
    last_pos = None
    stable_since = None
    while True:
        now = time.monotonic() - t0
        if now > max_wait_s:
            break
        pos = P.read_position_or_none()
        if pos is not None:
            err = abs(pos - target_abs)
            samples.append((now, pos, err))
            for tol in CANDIDATE_TOL_STEPS:
                if err <= tol:
                    consec[tol] += 1
                    if (stable_within[tol] is None
                            and consec[tol] >= POS_STABLE_COUNT):
                        stable_within[tol] = now
                else:
                    consec[tol] = 0
            # stop early once the position has been unchanged for a while AND
            # we are inside the tightest band we could hope for
            if last_pos is not None and pos == last_pos:
                if stable_since is None:
                    stable_since = now
                elif now - stable_since > 0.30:
                    break
            else:
                stable_since = None
            last_pos = pos
        time.sleep(poll_s)
    return samples, stable_within


def run_step_size(step_nm, moves, alternate, max_wait_s, rows):
    step_steps = int(round(step_nm * STEPS_PER_NM))
    print(f"\n{'='*72}")
    print(f"STEP {step_nm:g} nm  =  {step_steps} steps "
          f"(drive corridor is 20 steps = {pm(20):.4f} pm)")
    print(f"{'='*72}")
    if step_steps < 20:
        print(f"  !! {step_steps} steps is BELOW the drive's own position corridor "
              f"(20). The servo cannot be expected to distinguish this; results "
              f"here show the floor, not a usable step size.")

    final_errs = []
    ok_at_tol = {t: 0 for t in CANDIDATE_TOL_STEPS}

    print(f"  {'#':>3s} {'cmd':>8s} {'final err':>10s} {'':>6s} {'settle':>8s}")
    for i in range(moves):
        direction = -1 if (alternate and i % 2) else 1
        delta = step_steps * direction
        start = P.read_position_or_none()
        if start is None:
            print("  [ERR] no POS reading; aborting this step size.")
            return
        target = start + delta
        P.send_cmd(f"LR{delta}")
        P.send_cmd("M")
        samples, stable_within = settle_profile(target, max_wait_s)
        if not samples:
            print(f"  {i+1:3d}  no samples")
            continue
        final_err = samples[-1][2]
        final_errs.append(final_err)
        settle_t = samples[-1][0]
        for tol in CANDIDATE_TOL_STEPS:
            if stable_within[tol] is not None:
                ok_at_tol[tol] += 1
        print(f"  {i+1:3d} {delta:8d} {final_err:7.0f} st {pm(final_err):5.2f}pm "
              f"{settle_t:7.2f}s")
        if rows is not None:
            for (t, pos, err) in samples:
                rows.append((step_nm, i + 1, round(t, 4), pos, err,
                             round(pm(err), 4)))
        P.safe_stop()
        time.sleep(0.1)

    if not final_errs:
        return
    worst = max(final_errs)
    mean = sum(final_errs) / len(final_errs)
    print(f"\n  final positioning error: mean {mean:.0f} steps ({pm(mean):.2f} pm), "
          f"worst {worst:.0f} steps ({pm(worst):.2f} pm)")
    print(f"  {'tolerance':>10s} {'':>10s} {'moves reaching it':>18s}")
    tightest_ok = None
    for tol in CANDIDATE_TOL_STEPS:
        mark = ""
        if ok_at_tol[tol] == moves:
            if tightest_ok is None:
                tightest_ok = tol
                mark = "  <-- tightest that ALWAYS worked"
        print(f"  {tol:8d} st {pm(tol):8.2f}pm {ok_at_tol[tol]:12d}/{moves}{mark}")
    if tightest_ok:
        print(f"\n  => For a {step_nm:g} nm step, POS_TOL_STEPS could go down to "
              f"~{tightest_ok} steps ({pm(tightest_ok):.2f} pm).")
        print(f"     That is {step_steps/tightest_ok:.1f}x finer than the step "
              f"itself (today: {step_steps/POS_TOL_STEPS:.2f}x).")
    else:
        print(f"\n  => No candidate tolerance was reached on every move. "
              f"Either the step is below what the servo can resolve, or "
              f"max_wait ({max_wait_s:g}s) is too short.")


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("port")
    ap.add_argument("--baud", type=int, default=9600)
    ap.add_argument("--steps", type=float, nargs="+",
                    default=[0.01, 0.001, 0.0001],
                    help="Step sizes in nm to test (default: 0.01 0.001 0.0001)")
    ap.add_argument("--moves", type=int, default=10,
                    help="Moves per step size (default: 10)")
    ap.add_argument("--max-wait", type=float, default=5.0,
                    help="Max settle observation per move, seconds (default: 5)")
    ap.add_argument("--alternate", action="store_true",
                    help="Alternate direction (includes backlash reversals). "
                         "Off by default so the numbers show pure positioning.")
    ap.add_argument("--csv", type=str, default=None,
                    help="Write the full settling curves to this CSV")
    args = ap.parse_args()

    total_nm = sum(s * args.moves for s in args.steps)
    print(f"Rig constants: STEPS_PER_NM = {STEPS_PER_NM:,}  "
          f"(1 step = {pm(1)*1000:.2f} fm)")
    print(f"Current POS_TOL_STEPS = {POS_TOL_STEPS} steps = {pm(POS_TOL_STEPS):.2f} pm")
    print(f"POLL_TIMEOUT = {POLL_TIMEOUT}s, POS_STABLE_COUNT = {POS_STABLE_COUNT} "
          f"(a band counts as reached only after that many consecutive hits, "
          f"same rule as wait_until_position)")
    print(f"Planned total travel: ~{total_nm:.4f} nm "
          f"({'alternating' if args.alternate else 'one direction'})")
    print(f"\nConnecting to {args.port} @ {args.baud} baud...")

    ser = serial.Serial(args.port, args.baud, timeout=1.0)
    state["ser"] = ser
    state["connected"] = True
    rows = [] if args.csv else None
    try:
        P.drain_stale_serial(ser)
        P.init_motor()
        P.configure_ramp()
        pos0 = P.read_position_or_none()
        print(f"Start POS: {pos0}")
        for step_nm in args.steps:
            run_step_size(step_nm, args.moves, args.alternate, args.max_wait, rows)

        print(f"\n{'='*72}")
        print("HOW TO USE THIS")
        print(f"{'='*72}")
        print("Take the tightest tolerance that ALWAYS worked for the finest step")
        print("you actually want to scan at, add margin (say 2x), and put that in")
        print("device_constants.POS_TOL_STEPS. Then re-run a real scan at that step")
        print("size and check the log for '[TIMEOUT] Did not reach target' lines --")
        print("if any appear, the tolerance is too tight for the servo on this rig.")
        print("\nNote: the absolute step grid in scan_engine already prevents these")
        print("errors from ACCUMULATING across a scan. A tighter tolerance improves")
        print("where each INDIVIDUAL point sits, which is what matters for fine steps.")
    finally:
        try:
            P.safe_stop()
        except Exception:
            pass
        if rows:
            with open(args.csv, "w", newline="") as f:
                w = csv_module.writer(f)
                w.writerow(["step_nm", "move", "t_s", "pos_steps", "err_steps", "err_pm"])
                w.writerows(rows)
            print(f"\nSettling curves written to {args.csv}")
        state["connected"] = False
        ser.close()
        print("\nDisconnected. Drive left stopped; nothing saved to EEPROM.")


if __name__ == "__main__":
    main()
