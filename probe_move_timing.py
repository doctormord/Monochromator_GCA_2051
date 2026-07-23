#!/usr/bin/env python3
"""
probe_move_timing.py — Standalone timing probe for "read while moving".

WHAT THIS SCRIPT DOES
----------------------
Before building a continuous-acquisition scan mode (DAQ reads interleaved
with position polls WHILE the motor is running, instead of stop-move-then-
read), we need real numbers, not guesses:

  - How long does a single POS query actually take on this rig/baud, both
    IDLE and DURING an active move (LR+M)?
  - How tight/jittery is the achievable poll loop (min/mean/max interval,
    stddev)?
  - If an NI-DAQ read is added into the same loop iteration, how much does
    that add, and what's the resulting COMBINED cycle time (-> the real
    ceiling on sample rate for a future continuous-scan mode)?
  - How many motor steps pass between two consecutive polls at that rate,
    for a few different move distances (short/long) -- i.e. what SPATIAL
    resolution a continuous scan would actually achieve given the current
    AC/DEC/SP ramp?

This script does NOT decide anything or change app behaviour. It produces
a data set (CSV per run) and a text report (probe_report_<timestamp>.md)
to work from afterwards -- per project policy (HANDOVER.md): hardware
timing is measured, never guessed.

WHAT THIS SCRIPT DELIBERATELY DOES NOT DO
------------------------------------------
- Never calls SAVE/EEPSAV.
- Never changes AC/DEC/SP unless you explicitly pass --ac/--dec/--sp
  (RAM-only, same as tune_ramp.py); by default it measures with whatever
  ramp is CURRENTLY ACTIVE on the drive (read back and printed first).
- Independent of the rest of the codebase (no app_context/gui_main/
  protocol_faulhaber import) -- deliberately simple and safe to run
  stand-alone, same philosophy as tune_ramp.py.
- Move distances are explicit CLI args, nothing is auto-guessed; default
  distances are intentionally small (a few thousand steps -- well under
  one nm on this rig's STEPS_PER_NM) unless you widen them yourself.

USAGE
-----
    # POS-only timing, default distances, both directions, 3 repeats:
    python3 probe_move_timing.py COM3

    # Include NI-DAQ read timing in the same loop (needs nidaqmx + real
    # hardware or it will just report "not available"):
    python3 probe_move_timing.py COM3 --daq-dev Dev1 --daq-ai ai1

    # Custom distances (steps, signed, comma-separated) and repeats:
    python3 probe_move_timing.py COM3 --moves 2000,-2000,50000,-50000 --repeat 2

    # Apply a specific ramp before probing (RAM-only, mirrors tune_ramp.py):
    python3 probe_move_timing.py COM3 --ac 50 --dec 100 --sp 10000

Requires pyserial. nidaqmx optional (only needed for --daq-dev/--daq-ai).
"""

import sys
import time
import argparse
import csv
import json
import statistics
from datetime import datetime

import serial

try:
    import nidaqmx
    from nidaqmx.constants import AcquisitionType, TerminalConfiguration
    DAQ_AVAILABLE = True
except Exception:
    nidaqmx = None
    AcquisitionType = None
    TerminalConfiguration = None
    DAQ_AVAILABLE = False

# Try to pull STEPS_PER_NM purely for display (nm/s, nm-per-poll) -- optional,
# never required. device_constants.py is a pure-constants module (no GUI/app
# import chain), so this stays consistent with the "standalone, safe" goal.
try:
    sys.path.insert(0, ".")
    from device_constants import STEPS_PER_NM
except Exception:
    STEPS_PER_NM = None


# =====================================================================
# Low-level serial helpers (deliberately minimal -- diagnostic tool, not
# the production protocol layer; no resync/retry magic, see
# protocol_faulhaber.py for that).
# =====================================================================

def send(ser, cmd: str, timeout: float = 1.0) -> str:
    ser.reset_input_buffer()
    ser.reset_output_buffer()
    ser.write((cmd + "\r").encode("ascii"))
    ser.flush()
    ser.timeout = timeout
    return ser.read_until(b"\r").decode(errors="ignore").strip()


def read_pos(ser, timeout: float = 0.5):
    """One POS query. Returns (position_or_None, round_trip_seconds)."""
    t0 = time.perf_counter()
    raw = send(ser, "POS", timeout=timeout)
    dt = time.perf_counter() - t0
    try:
        return int(raw), dt
    except Exception:
        return None, dt


def read_int(ser, cmd: str, label: str, unit: str = ""):
    raw = send(ser, cmd)
    try:
        val = int(raw)
        print(f"  {label:32s} ({cmd:10s}) = {val}{unit}")
        return val
    except Exception:
        print(f"  {label:32s} ({cmd:10s}) = <no/invalid reply: {raw!r}>")
        return None


def read_current_ramp(ser):
    print("\n--- Currently active ramp on the drive (read-only) ---")
    ac = read_int(ser, "GAC", "Acceleration (AC)", " [1/s^2]")
    dec = read_int(ser, "GDEC", "Deceleration (DEC)", " [1/s^2]")
    sp = read_int(ser, "GSP", "Max speed (SP)", " [rpm]")
    return ac, dec, sp


def apply_ramp(ser, ac, dec, sp):
    print("\n--- Applying probe ramp (RAM only, NOT saved to EEPROM) ---")
    if ac is not None:
        send(ser, f"AC{int(ac)}"); print(f"  sent AC{int(ac)}")
    if dec is not None:
        send(ser, f"DEC{int(dec)}"); print(f"  sent DEC{int(dec)}")
    if sp is not None:
        send(ser, f"SP{int(sp)}"); print(f"  sent SP{int(sp)}")


# =====================================================================
# DAQ helper (mirrors daq.read_pmt_voltage's single-read shape, so the
# measured timing is representative of what the real acquire path costs
# -- same open/read/close-per-call pattern, not a persistent task).
# =====================================================================

def read_daq_once(dev: str, ai: str, samples: int = 10, rate: int = 1000):
    """One averaged NI-DAQ read. Returns (voltage_or_None, seconds)."""
    t0 = time.perf_counter()
    if not DAQ_AVAILABLE:
        return None, time.perf_counter() - t0
    try:
        with nidaqmx.Task() as task:
            task.ai_channels.add_ai_voltage_chan(
                f"{dev}/{ai}", min_val=0.0, max_val=10.0,
                terminal_config=TerminalConfiguration.RSE
            )
            if samples > 1:
                task.timing.cfg_samp_clk_timing(
                    rate, sample_mode=AcquisitionType.FINITE, samps_per_chan=samples)
                vals = task.read(number_of_samples_per_channel=samples)
                v = float(sum(vals) / len(vals)) if isinstance(vals, list) and vals else float(vals)
            else:
                v = float(task.read())
    except Exception as e:
        print(f"  [DAQ WARN] read failed: {e}")
        v = None
    return v, time.perf_counter() - t0


# =====================================================================
# Baseline probes (IDLE -- no motion, reference floor for pure comm
# overhead, to separate "serial/DAQ latency" from "affected by motion").
# =====================================================================

def probe_pos_baseline(ser, n=30):
    dts = []
    for _ in range(n):
        _, dt = read_pos(ser)
        dts.append(dt)
    return dts


def probe_daq_baseline(dev, ai, n=30, samples=10, rate=1000):
    dts = []
    for _ in range(n):
        _, dt = read_daq_once(dev, ai, samples, rate)
        dts.append(dt)
    return dts


# =====================================================================
# The actual move probe: send one relative move, then poll POS (+
# optional DAQ) in a tight loop, as fast as the hardware/serial allow,
# until position is stable for N consecutive polls or a safety timeout
# hits. This deliberately does NOT assume constant velocity (the ramp
# means it isn't) -- every sample gets its own real POS read.
# =====================================================================

def run_move_probe(ser, steps: int, poll_timeout: float, daq_args,
                    stable_polls: int = 4, max_wait_s: float = 120.0):
    samples = []
    send(ser, "EN")
    send(ser, "HP0")
    send(ser, f"LR{int(steps)}")
    t_start = time.perf_counter()
    send(ser, "M")

    last_pos = None
    stable = 0
    while True:
        now = time.perf_counter()
        if now - t_start > max_wait_s:
            print(f"  [WARN] max_wait_s={max_wait_s}s exceeded, aborting wait for this move.")
            break

        pos, pos_dt = read_pos(ser, timeout=poll_timeout)
        v, v_dt = (None, None)
        if daq_args is not None:
            v, v_dt = read_daq_once(*daq_args)
        t_sample = time.perf_counter() - t_start

        samples.append(dict(t=t_sample, pos=pos, pos_dt=pos_dt, v=v, v_dt=v_dt))

        if pos is not None and last_pos is not None and pos == last_pos:
            stable += 1
            if stable >= stable_polls:
                break
        else:
            stable = 0
        last_pos = pos

    send(ser, "V0")
    return samples


# =====================================================================
# Stats / report
# =====================================================================

def interval_stats(times):
    """times: list of cumulative seconds (monotonic, ascending). Returns
    dict of poll-interval statistics, or None if too few samples."""
    if len(times) < 3:
        return None
    intervals = [b - a for a, b in zip(times[:-1], times[1:])]
    return dict(
        n_intervals=len(intervals),
        mean=statistics.mean(intervals),
        stdev=statistics.stdev(intervals) if len(intervals) > 1 else 0.0,
        min=min(intervals),
        max=max(intervals),
        hz_mean=1.0 / statistics.mean(intervals) if statistics.mean(intervals) > 0 else float("nan"),
    )


def fmt_ms(x):
    return f"{x*1000.0:.1f} ms" if x is not None else "n/a"


def build_report(args, ramp_active, pos_base_dts, daq_base_dts, runs):
    lines = []
    lines.append(f"# probe_move_timing report — {datetime.now().isoformat(timespec='seconds')}")
    lines.append("")
    lines.append(f"Port: {args.port} @ {args.baud} baud")
    lines.append(f"Active ramp during probe: AC={ramp_active[0]} DEC={ramp_active[1]} SP={ramp_active[2]}")
    if STEPS_PER_NM:
        lines.append(f"STEPS_PER_NM (device_constants.py): {STEPS_PER_NM}")
    lines.append(f"DAQ included: {'yes (' + args.daq_dev + '/' + args.daq_ai + ')' if args.daq_dev else 'no'}"
                 + ("" if DAQ_AVAILABLE or not args.daq_dev else "  [nidaqmx driver NOT available -- DAQ timing NOT measured]"))
    lines.append("")

    lines.append("## Baseline (IDLE, no motion)")
    if pos_base_dts:
        m = statistics.mean(pos_base_dts)
        lines.append(f"- POS query round-trip: mean={fmt_ms(m)}, "
                      f"min={fmt_ms(min(pos_base_dts))}, max={fmt_ms(max(pos_base_dts))}, "
                      f"n={len(pos_base_dts)}")
    if daq_base_dts:
        m = statistics.mean(daq_base_dts)
        lines.append(f"- DAQ read round-trip: mean={fmt_ms(m)}, "
                      f"min={fmt_ms(min(daq_base_dts))}, max={fmt_ms(max(daq_base_dts))}, "
                      f"n={len(daq_base_dts)}")
    lines.append("")

    lines.append("## Per-move results (DURING motion)")
    lines.append("")
    lines.append("| steps | n samples | move duration | poll interval mean | stdev | min | max | eff. Hz | steps/poll (mean) |")
    lines.append("|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for run in runs:
        st = run["interval_stats"]
        steps_per_poll = "n/a"
        n_valid_pos = [s["pos"] for s in run["samples"] if s["pos"] is not None]
        if len(n_valid_pos) >= 2 and st:
            total_steps = abs(n_valid_pos[-1] - n_valid_pos[0])
            steps_per_poll = f"{total_steps / max(1, st['n_intervals']):.0f}"
        if st:
            lines.append(
                f"| {run['steps']:+d} | {len(run['samples'])} | "
                f"{run['samples'][-1]['t']:.2f}s | {fmt_ms(st['mean'])} | "
                f"{fmt_ms(st['stdev'])} | {fmt_ms(st['min'])} | {fmt_ms(st['max'])} | "
                f"{st['hz_mean']:.1f} | {steps_per_poll} |"
            )
        else:
            lines.append(f"| {run['steps']:+d} | {len(run['samples'])} | "
                          f"(too few samples for interval stats) | | | | | | |")
    lines.append("")

    all_intervals_hz = [r["interval_stats"]["hz_mean"] for r in runs if r["interval_stats"]]
    if all_intervals_hz:
        lines.append(f"## Overall achievable rate across all runs")
        lines.append(f"- mean of per-run mean Hz: {statistics.mean(all_intervals_hz):.1f} Hz")
        lines.append(f"- slowest run: {min(all_intervals_hz):.1f} Hz")
        lines.append(f"- fastest run: {max(all_intervals_hz):.1f} Hz")
        lines.append("")

    lines.append("## Notes / how to read this")
    lines.append("- This is SOFTWARE-polled timing over serial + (optionally) a per-read")
    lines.append("  NI-DAQ open/read/close -- not hardware-triggered/timestamped. POS and")
    lines.append("  DAQ values in one sample are sequential, not simultaneous; the gap")
    lines.append("  between them is `pos_dt`/`v_dt` in the raw CSV per run.")
    lines.append("- 'steps/poll (mean)' is the average spatial resolution a continuous-scan")
    lines.append("  mode would achieve at this ramp/rate for this move -- convert to nm via")
    lines.append("  STEPS_PER_NM if you need it in wavelength units.")
    lines.append("- Interval stdev matters more than mean: high stdev = jittery loop, which")
    lines.append("  would make position/voltage pairing uneven across a real scan.")
    lines.append("- Raw per-run samples are in the accompanying CSV files")
    lines.append("  (`probe_run_<n>_<steps>.csv`), for deeper analysis if the summary above")
    lines.append("  isn't enough to decide.")
    return "\n".join(lines)


def write_run_csv(path, run):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["t_s", "pos_steps", "pos_dt_s", "voltage", "daq_dt_s"])
        for s in run["samples"]:
            w.writerow([f"{s['t']:.6f}", s["pos"], f"{s['pos_dt']:.6f}",
                        "" if s["v"] is None else f"{s['v']:.6f}",
                        "" if s["v_dt"] is None else f"{s['v_dt']:.6f}"])


# =====================================================================
# CLI
# =====================================================================

def parse_moves(s: str):
    out = []
    for tok in s.split(","):
        tok = tok.strip()
        if not tok:
            continue
        out.append(int(tok))
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("port", help="Serial port, e.g. COM3 or /dev/tty.usbserial-XXXX")
    p.add_argument("--baud", type=int, default=9600)
    p.add_argument("--ac", type=int, default=None, help="Apply this AC before probing (RAM only)")
    p.add_argument("--dec", type=int, default=None, help="Apply this DEC before probing (RAM only)")
    p.add_argument("--sp", type=int, default=None, help="Apply this SP before probing (RAM only)")
    p.add_argument("--moves", type=str, default="2000,-2000,50000,-50000",
                   help="Comma-separated signed step distances to probe, e.g. '2000,-2000,50000,-50000'")
    p.add_argument("--repeat", type=int, default=2, help="Repeat the whole move list this many times")
    p.add_argument("--poll-timeout", type=float, default=0.5,
                   help="Per-POS-query timeout in seconds (generous on purpose -- we want to SEE real timing, not clamp it)")
    p.add_argument("--stable-polls", type=int, default=4,
                   help="Consecutive identical POS reads to consider a move finished")
    p.add_argument("--max-wait", type=float, default=120.0, help="Safety timeout per move (s)")
    p.add_argument("--daq-dev", type=str, default=None, help="NI-DAQ device, e.g. Dev1 (omit to skip DAQ timing)")
    p.add_argument("--daq-ai", type=str, default="ai1", help="NI-DAQ AI channel, e.g. ai1")
    p.add_argument("--daq-samples", type=int, default=10, help="Hardware samples per DAQ read")
    p.add_argument("--daq-rate", type=int, default=1000, help="DAQ sample rate (Hz)")
    p.add_argument("--outdir", type=str, default=".", help="Where to write the report + CSVs")
    args = p.parse_args()

    if args.daq_dev and not DAQ_AVAILABLE:
        print("[WARN] --daq-dev given but nidaqmx is not importable here -- "
              "DAQ timing will be skipped, POS-only timing still runs.")

    daq_args = (args.daq_dev, args.daq_ai, args.daq_samples, args.daq_rate) if (args.daq_dev and DAQ_AVAILABLE) else None

    print(f"Connecting to {args.port} @ {args.baud} baud...")
    ser = serial.Serial(args.port, args.baud, timeout=1.0)
    time.sleep(0.3)

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    runs = []
    try:
        ramp_active = read_current_ramp(ser)
        if args.ac is not None or args.dec is not None or args.sp is not None:
            apply_ramp(ser, args.ac, args.dec, args.sp)
            ramp_active = read_current_ramp(ser)

        print("\n--- Baseline: POS query round-trip, IDLE (no motion) ---")
        pos_base = probe_pos_baseline(ser, n=30)
        print(f"  mean={fmt_ms(statistics.mean(pos_base))}  "
              f"min={fmt_ms(min(pos_base))}  max={fmt_ms(max(pos_base))}")

        daq_base = []
        if daq_args:
            print("\n--- Baseline: DAQ read round-trip, IDLE ---")
            daq_base = probe_daq_baseline(daq_args[0], daq_args[1], n=30,
                                          samples=daq_args[2], rate=daq_args[3])
            if any(d is not None for d in daq_base):
                print(f"  mean={fmt_ms(statistics.mean(daq_base))}  "
                      f"min={fmt_ms(min(daq_base))}  max={fmt_ms(max(daq_base))}")

        move_list = parse_moves(args.moves) * max(1, args.repeat)
        print(f"\n--- Move probes: {move_list} (steps) ---")
        for i, steps in enumerate(move_list, 1):
            print(f"\n[{i}/{len(move_list)}] moving {steps:+d} steps, polling "
                  f"{'POS+DAQ' if daq_args else 'POS'}...")
            samples = run_move_probe(ser, steps, args.poll_timeout, daq_args,
                                     stable_polls=args.stable_polls, max_wait_s=args.max_wait)
            times = [s["t"] for s in samples]
            st = interval_stats(times)
            run = dict(steps=steps, samples=samples, interval_stats=st)
            runs.append(run)
            if st:
                print(f"  {len(samples)} samples over {times[-1]:.2f}s, "
                      f"mean interval {fmt_ms(st['mean'])} (~{st['hz_mean']:.1f} Hz), "
                      f"jitter stdev {fmt_ms(st['stdev'])}")
            else:
                print(f"  {len(samples)} samples -- too few for interval stats")

            csv_path = f"{args.outdir}/probe_run_{i:02d}_{steps:+d}.csv"
            write_run_csv(csv_path, run)
            print(f"  raw samples -> {csv_path}")

        report = build_report(args, ramp_active, pos_base, daq_base, runs)
        report_path = f"{args.outdir}/probe_report_{ts}.md"
        with open(report_path, "w") as f:
            f.write(report)
        print(f"\n=== Report written to {report_path} ===\n")
        print(report)

    finally:
        try:
            send(ser, "ST"); send(ser, "HP0"); send(ser, "V0")
        except Exception:
            pass
        ser.close()


if __name__ == "__main__":
    main()
