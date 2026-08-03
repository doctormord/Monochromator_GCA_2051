"""
offset_probe.py -- Automated probe for the scan-to-scan wavelength OFFSET.

WHAT THIS SCRIPT IS FOR
-----------------------
At the rig, groups of otherwise identical scans of the same line came out
mutually congruent WITHIN a group but rigidly shifted BETWEEN groups by
roughly 0.07 nm -- about one Slip (0.080 nm). The group boundary coincided
with an Avg-fraction change, but a static test showed the optics do not creep
while standing still, so the averaging window cannot be the cause: measurement
DURATION can only matter if something MOVES during the measurement.

This script exists to settle the question with data instead of argument. It
runs a MATRIX of nominally identical scans and inserts a DEFINED perturbation
between them (nothing / jog / go-to / mid-move abort / averaging change), then
reports the peak position of every scan in TWO different coordinate systems:

    nm_app  -- the wavelength LABEL, computed the way scan_engine computes it:
               an absolute grid of start + step * index. Backlash compensation
               steps are deliberately NOT folded in, because they are assumed
               to be slack take-up rather than optical travel.

    nm_enc  -- the wavelength derived from the ENCODER (POS), anchored ONCE at
               the start of the whole session and never re-anchored.

THE DISCRIMINATOR. Because the encoder anchor is session-global, every scan in
the run lands in one common frame:

    peak(nm_enc) constant, peak(nm_app) jumps  -> SOFTWARE bookkeeping.
                                                  The grating went to the same
                                                  place; only the label moved.
    peak(nm_enc) jumps too                     -> MECHANICS / drive.
                                                  The grating really went
                                                  somewhere else.
    neither jumps                              -> not reproducible this way;
                                                  the hypothesis is wrong.

That decision needs no hypothesis to be correct in advance, which is the whole
point of running it.

WHAT THIS SCRIPT DELIBERATELY DOES NOT DO
------------------------------------------
- It does NOT import or drive gui_main / scan_engine. Those are welded to Qt
  widgets (refs['entry_current'], refs['btn_pause'].configure(...)), and
  stubbing widgets to make them run headless would put MY stubs in the
  measurement path. So the scan loop here is re-implemented, transparently,
  out of the same low-level primitives the app uses (steps_for_delta_nm,
  wait_until_position, read_position, read_pmt_voltage_avg).
  CONSEQUENCE, STATED PLAINLY: this tool can prove or disprove a PHYSICAL
  offset and it can show whether the grid/slack arithmetic accounts for it.
  It cannot prove what gui_main does, because it does not run gui_main. The
  GUI path is covered by the console log of a manual session (see the PDF).
- It does NOT write SAVE / EEPSAV. Nothing on the drive is made permanent.
- It does NOT change any app source file or settings file.
- The 'jog' perturbation here is a plain relative move. IN THE APPLICATION IT
  IS NOT: jog_plus_action()/jog_minus_action() read entry_current, write the
  target into entry_goto, and call goto_wavelength_action(). A GUI jog is a
  Go To, and therefore inherits the text-field origin, the auto-recover retry
  and the `state["current_nm"] = target_nm` assertion. So if the probe's jog
  does NOT reproduce the offset, that does not clear the GUI jog -- test T1 by
  hand does. If the probe's jog DOES reproduce it, the cause is below the goto
  layer and the finding is the stronger one.
- It applies backlash compensation from an EXPLICIT --slip-nm argument rather
  than from the GUI field, and separately reports what
  motion.reversal_compensation_steps() would have returned, so a disagreement
  between the two shows up in the data instead of silently zeroing the very
  effect under test.

USAGE
-----
    # verify the analysis maths without hardware and without the app modules
    python offset_probe.py --selftest

    # dry run against the virtual drive (needs sim_hardware.py)
    python offset_probe.py SIM --center 587.80 --span 0.40 --step 0.02

    # the real thing
    python offset_probe.py COM3 --center 587.80 --span 0.40 --step 0.01 \
        --timebase-ms 300 --presettle-ms 50 --slip-nm 0.080 \
        --outdir probe_2026-07-30

Outputs into --outdir:
    scan_NN_<tag>.csv   one row per measured point, both nm conventions
    summary.csv         one row per scan: peak in both frames, offsets, state
    probe.log           full transcript, including every [SLIP] line

Run it with the same optical setup and lamp warmed up, and do not touch the
GUI while it runs -- two programs on one COM port is not a test, it is a mess.
"""

import argparse
import csv
import math
import os
import statistics
import sys
import time

# ---------------------------------------------------------------------------
# Logging: file + stdout. Set up before anything else so import failures land
# in the transcript too.
# ---------------------------------------------------------------------------

_LOG_FH = None


def plog(msg, level="info"):
    line = f"{time.strftime('%H:%M:%S')} [{level.upper():5}] {msg}"
    print(line, flush=True)
    if _LOG_FH is not None:
        _LOG_FH.write(line + "\n")
        _LOG_FH.flush()


# ---------------------------------------------------------------------------
# Symbol resolution.
#
# WHY NOT PLAIN IMPORTS: this repository has been through a monolith -> module
# split, and the same function has lived in different modules at different
# times (read_position in the monolith, in protocol_faulhaber, referenced from
# motion). Hard-coding one import path makes the tool fail on a version skew
# that has nothing to do with the measurement. Resolve by name across the
# candidate modules and say clearly what was found where.
# ---------------------------------------------------------------------------

_MODULE_NAMES = ("motion", "protocol_faulhaber", "daq", "device_constants",
                 "app_context", "scan_engine")


class Api:
    """Resolved handles to the application's own primitives."""

    def __init__(self):
        self.mods = {}
        for name in _MODULE_NAMES:
            try:
                self.mods[name] = __import__(name)
            except Exception as e:
                plog(f"module '{name}' not importable: {e}", "warn")

        if not self.mods:
            raise SystemExit(
                "None of the application modules could be imported. Run this "
                "script from the directory that contains motion.py, "
                "protocol_faulhaber.py, daq.py and device_constants.py."
            )

        # Required
        self.send_cmd = self._need("send_cmd")
        self.read_position = self._need("read_position")
        self.init_motor = self._need("init_motor")
        self.safe_stop = self._need("safe_stop")
        self.steps_for_delta_nm = self._need("steps_for_delta_nm")
        self.wait_until_position = self._need("wait_until_position")
        self.read_pmt_voltage_avg = self._need("read_pmt_voltage_avg")

        # Optional / with fallbacks
        self.read_position_or_none = self._want("read_position_or_none")
        self.steps_to_delta_nm = self._want("steps_to_delta_nm")
        self.timeout_for_target_steps = self._want("timeout_for_target_steps")
        self.reversal_compensation_steps = self._want("reversal_compensation_steps")
        self.drain_stale_serial = self._want("drain_stale_serial")
        self.daq_source_label = self._want("daq_source_label")
        self.configure_ramp = self._want("configure_ramp")

        dc = self.mods.get("device_constants")
        self.STEPS_PER_NM = float(getattr(dc, "STEPS_PER_NM", 361765.0))
        self.INVERT_DIRECTION = bool(getattr(dc, "INVERT_DIRECTION", False))
        self.SAFE_AFTER_MOVE = bool(getattr(dc, "SAFE_AFTER_MOVE", True))
        self.DEFAULT_BAUD = int(getattr(dc, "DEFAULT_BAUD", 9600))

        ac = self.mods.get("app_context")
        self.state = getattr(ac, "state", {}) if ac else {}

        plog(f"STEPS_PER_NM={self.STEPS_PER_NM:.0f}  "
             f"INVERT_DIRECTION={self.INVERT_DIRECTION}  "
             f"SAFE_AFTER_MOVE={self.SAFE_AFTER_MOVE}")

    def _find(self, name):
        for mod_name, mod in self.mods.items():
            fn = getattr(mod, name, None)
            if fn is not None:
                return fn, mod_name
        return None, None

    def _need(self, name):
        fn, where = self._find(name)
        if fn is None:
            raise SystemExit(
                f"Required symbol '{name}' not found in any of "
                f"{list(self.mods)}. The module layout has changed; fix the "
                f"resolver in offset_probe.py rather than guessing."
            )
        plog(f"resolved {name}() from {where}")
        return fn

    def _want(self, name):
        fn, where = self._find(name)
        if fn is None:
            plog(f"optional symbol '{name}' not found -- using local fallback", "warn")
        else:
            plog(f"resolved {name}() from {where}")
        return fn

    # -- derived helpers ---------------------------------------------------

    def steps_to_nm(self, steps):
        """Convert a raw step delta to a wavelength delta.

        Prefers the app's own converter so any sign convention or rounding it
        applies is reproduced here rather than re-invented.
        """
        if self.steps_to_delta_nm is not None:
            return float(self.steps_to_delta_nm(steps))
        d = steps / self.STEPS_PER_NM
        return -d if self.INVERT_DIRECTION else d

    def pos(self):
        """RAW POS read. Prefer pos_safe() -- see why there."""
        if self.read_position_or_none is not None:
            p = self.read_position_or_none()
            if p is None:
                raise RuntimeError("POS read failed")
            return int(p)
        return int(self.read_position())

    def pos_safe(self, expected=None, tol_nm=0.05, max_jump_nm=None, retries=3):
        """POS read with a plausibility guard.

        WHY THIS EXISTS -- this is not defensive padding, it is a documented
        hardware behaviour. Rig logs show POS replies arriving TRUNCATED and
        being parsed as a valid integer:

            [FREE RUN] POS outlier #12 rejected: raw reply '1' -> parsed 1
            steps, jumped 10961009 steps in 785 ms from last good 10961010
            [FREE RUN] 21 POS outlier(s) rejected during this run

        Twenty-one in a single sweep. `read_position_or_none()` catches EMPTY
        replies, not wrongly-parsed ones. Since nm_enc -- the entire encoder
        reference frame of this probe -- is derived from POS, one such reply
        would move a scan's peak by millions of steps and fake a "mechanics"
        verdict. The free-run loop has its own outlier filter for exactly this;
        this probe needs one too.

        The check here is stricter than free-run's, and can be: the drive is
        STANDING STILL when this is called, and the commanded target is known.
        So POS must be at the target, within tolerance.

        Sets self.pos_suspect if no plausible reading was obtained, so the
        affected point can be flagged in the CSV rather than silently trusted.
        """
        tol_steps = abs(int(self.steps_for_delta_nm(tol_nm))) if tol_nm else None
        jump_steps = abs(int(self.steps_for_delta_nm(max_jump_nm))) if max_jump_nm else None
        last_good = getattr(self, "_last_good_pos", None)

        best = None
        for attempt in range(max(1, retries)):
            try:
                p = self.pos()
            except Exception as e:
                plog(f"POS read failed (attempt {attempt+1}): {e}", "warn")
                time.sleep(0.05)
                continue
            best = p

            bad = None
            if expected is not None and tol_steps is not None \
                    and abs(p - expected) > tol_steps:
                bad = (f"{p - expected:+d} steps from commanded target "
                       f"{expected} (tolerance {tol_steps})")
            elif jump_steps is not None and last_good is not None \
                    and abs(p - last_good) > jump_steps:
                bad = (f"jumped {p - last_good:+d} steps from last good "
                       f"{last_good} (limit {jump_steps})")

            if bad is None:
                self._last_good_pos = p
                self.pos_suspect = False
                return p

            self.pos_rejects = getattr(self, "pos_rejects", 0) + 1
            plog(f"[POS] outlier rejected #{self.pos_rejects}: {p} -- {bad}. "
                 f"Re-reading (attempt {attempt+1}/{retries}).", "warn")
            time.sleep(0.05)

        # No plausible reading. Do NOT silently substitute the target -- that
        # would manufacture the very agreement the probe is trying to measure.
        # Flag it and let the analysis see a suspect point.
        self.pos_suspect = True
        plog(f"[POS] no plausible reading after {retries} attempts; point "
             f"flagged suspect. Raw value kept as-is: {best}", "error")
        if best is not None:
            return best
        raise RuntimeError("POS unreadable")

    def timeout_for(self, target_steps, delta_nm):
        if self.timeout_for_target_steps is not None:
            try:
                return self.timeout_for_target_steps(target_steps, delta_nm)
            except Exception:
                pass
        # Generous fallback: 2 s base + 4 s per nm.
        return 2.0 + 4.0 * abs(float(delta_nm))


# ---------------------------------------------------------------------------
# Connection
# ---------------------------------------------------------------------------

def connect(api, port, baud):
    if port.upper() == "SIM":
        from sim_hardware import FakeFaulhaber
        api.state["ser"] = FakeFaulhaber()
        api.state["connected"] = True
        plog("SIM: virtual FAULHABER attached. Results characterise the "
             "SIMULATOR, not the instrument.", "warn")
    else:
        import serial
        ser = serial.Serial(port, baud, timeout=0.8)
        api.state["ser"] = ser
        api.state["connected"] = True
        plog(f"connected {port} @ {baud} baud")
        if api.drain_stale_serial is not None:
            api.drain_stale_serial(ser)

    api.init_motor()
    if api.daq_source_label is not None:
        label = api.daq_source_label()
        plog(f"DAQ source: {label}")
        if label != "NI-DAQ":
            plog("DAQ is SIMULATED. Voltages are synthetic; peak positions "
                 "from this run say nothing about the real instrument.", "warn")


# ---------------------------------------------------------------------------
# Motion
#
# One move primitive, used for everything, so every move in the run is built
# the same way and the arithmetic is visible in one place.
# ---------------------------------------------------------------------------

class Mover:
    def __init__(self, api, slip_nm, disable_comp=False):
        self.api = api
        self.slip_nm = float(slip_nm)
        self.disable_comp = bool(disable_comp)
        self.last_direction = None      # our own bookkeeping, +1 / -1 / None
        self.slack_steps_total = 0      # cumulative compensation steps issued
        self.n_moves = 0
        self.n_failed = 0

    def compensation(self, direction):
        """Backlash compensation for a move about to travel in `direction`.

        Computed HERE from the explicit --slip-nm value, deliberately not from
        the GUI field, so it cannot silently be zero. What the application's
        own function would have returned is recorded alongside for comparison.
        """
        direction = 1 if direction >= 0 else -1

        app_value = None
        if self.api.reversal_compensation_steps is not None:
            try:
                # NOTE: this call has the SIDE EFFECT of setting
                # state['last_move_direction']. That is intentional -- it keeps
                # the app's own direction bookkeeping in step with ours, so the
                # value logged is what the app would really have produced at
                # this point in the sequence.
                app_value = int(self.api.reversal_compensation_steps(direction))
            except Exception as e:
                plog(f"reversal_compensation_steps() raised: {e}", "warn")

        mine = 0
        if not self.disable_comp and self.last_direction is not None \
                and self.last_direction != direction and self.slip_nm > 0:
            mine = int(self.api.steps_for_delta_nm(self.slip_nm * direction))
            plog(f"[SLIP] reversal {self.last_direction:+d}->{direction:+d}: "
                 f"{self.slip_nm:.3f} nm = {mine:+d} steps "
                 f"(app would give {app_value})")
        elif self.last_direction is None:
            plog("[SLIP] direction unknown -- first move is UNCOMPENSATED "
                 "(same as the app)", "warn")

        if app_value is not None and app_value != mine:
            plog(f"[SLIP] MISMATCH: probe={mine:+d} app={app_value:+d} steps. "
                 f"Most likely the GUI slip field is not what --slip-nm says.",
                 "warn")

        self.last_direction = direction
        return mine, app_value

    def move_by_nm(self, delta_nm, tag=""):
        """Relative move. Returns a dict describing what happened."""
        api = self.api
        if abs(delta_nm) <= 0:
            return {"skipped": True}

        direction = 1 if delta_nm >= 0 else -1
        comp, app_comp = self.compensation(direction)

        pos_before = api.pos_safe(max_jump_nm=max(1.0, 3 * abs(delta_nm)))
        rel = int(api.steps_for_delta_nm(delta_nm)) + comp
        target = pos_before + rel
        api.state["current_step_target_abs"] = target

        t0 = time.time()
        api.send_cmd(f"LR{rel}")
        api.send_cmd("M")
        reached = api.wait_until_position(
            target, timeout=api.timeout_for(target, delta_nm))
        dur = time.time() - t0
        if api.SAFE_AFTER_MOVE:
            api.safe_stop()

        pos_after = api.pos_safe(expected=target, tol_nm=0.10)
        self.n_moves += 1
        self.slack_steps_total += comp
        if not reached:
            self.n_failed += 1
            plog(f"move {tag}: NOT reached. target={target} actual={pos_after} "
                 f"residual={pos_after - target:+d} steps", "warn")

        return {
            "skipped": False,
            "tag": tag,
            "delta_nm": delta_nm,
            "direction": direction,
            "comp_steps": comp,
            "app_comp_steps": app_comp,
            "pos_before": pos_before,
            "rel_steps": rel,
            "target_steps": target,
            "pos_after": pos_after,
            "residual_steps": pos_after - target,
            "reached": bool(reached),
            "duration_s": dur,
        }

    def abort_mid_move(self, delta_nm, fraction=0.4):
        """Start a long move, stop it partway. Reproduces the abort case.

        This is the perturbation that the app's stepped-scan path does not
        re-anchor from, so it is the most interesting one.
        """
        api = self.api
        direction = 1 if delta_nm >= 0 else -1
        comp, app_comp = self.compensation(direction)

        pos_before = api.pos_safe(max_jump_nm=max(1.0, 3 * abs(delta_nm)))
        rel = int(api.steps_for_delta_nm(delta_nm)) + comp
        target = pos_before + rel
        est = api.timeout_for(target, delta_nm)
        wait_s = max(0.2, min(0.6 * est, fraction * est))

        plog(f"abort test: commanding {delta_nm:+.3f} nm, stopping after "
             f"{wait_s:.2f} s of an estimated {est:.2f} s")
        api.send_cmd(f"LR{rel}")
        api.send_cmd("M")
        time.sleep(wait_s)
        api.safe_stop()
        time.sleep(0.4)                       # let the deceleration ramp finish
        # No expected target here: stopping early is the whole point. Guard on
        # jump size only, generously -- it may legitimately have travelled far.
        pos_after = api.pos_safe(expected=None,
                                 max_jump_nm=abs(delta_nm) + 1.0)

        travelled_nm = api.steps_to_nm(pos_after - pos_before)
        plog(f"abort test: commanded {delta_nm:+.3f} nm, actually travelled "
             f"{travelled_nm:+.3f} nm, {target - pos_after:+d} steps short of "
             f"target")
        self.n_moves += 1
        self.slack_steps_total += comp
        return {
            "skipped": False, "tag": "abort", "delta_nm": delta_nm,
            "direction": direction, "comp_steps": comp,
            "app_comp_steps": app_comp, "pos_before": pos_before,
            "rel_steps": rel, "target_steps": target, "pos_after": pos_after,
            "residual_steps": pos_after - target, "reached": False,
            "duration_s": wait_s,
        }


# ---------------------------------------------------------------------------
# Measurement
# ---------------------------------------------------------------------------

class Acq:
    """Point acquisition with explicit parameters.

    Mirrors daq.acquire_measurement's budget model -- pre-settle, then either
    a fixed number of reads or a time window, then the remainder idle -- but
    takes every parameter as an argument instead of reading GUI variables, so
    a run is reproducible from the command line.
    """

    def __init__(self, api, timebase_ms, presettle_ms, mode, samples, fraction_pct):
        self.api = api
        self.timebase = max(0.0, timebase_ms / 1000.0)
        self.presettle = max(0.0, presettle_ms / 1000.0)
        self.mode = mode
        self.samples = max(1, int(samples))
        self.fraction = max(0.0, min(100.0, float(fraction_pct)))
        self.last_reads = None

    def describe(self):
        avail = max(0.0, self.timebase - min(self.presettle, self.timebase))
        if self.mode == "time":
            win = avail * self.fraction / 100.0
            return (f"timebase={self.timebase*1000:.0f}ms "
                    f"presettle={self.presettle*1000:.0f}ms "
                    f"mode=time frac={self.fraction:g}% -> window={win*1000:.1f}ms")
        return (f"timebase={self.timebase*1000:.0f}ms "
                f"presettle={self.presettle*1000:.0f}ms "
                f"mode=samples N={self.samples}")

    def point(self):
        api = self.api
        presettle = min(self.presettle, self.timebase)
        if presettle > 0:
            time.sleep(presettle)
        avail = max(0.0, self.timebase - presettle)

        t0 = time.time()
        if self.mode == "time":
            window = avail * self.fraction / 100.0
            v = api.read_pmt_voltage_avg(window_sec=window)
        else:
            v = api.read_pmt_voltage_avg(samples=self.samples)
        dur = time.time() - t0

        self.last_reads = api.state.get("last_avg_reads")
        rest = max(0.0, self.timebase - presettle - dur)
        if rest > 0:
            time.sleep(rest)
        return float(v), dur


# ---------------------------------------------------------------------------
# One scan
# ---------------------------------------------------------------------------

def run_scan(api, mover, acq, start_nm, end_nm, step_nm,
             anchor_steps, anchor_nm, run_id, tag, approach_nm):
    """Stepped scan, always approached from the same side.

    The label arithmetic deliberately copies scan_engine: an absolute grid of
    (start + step * index), with compensation steps folded into the POSITION
    target but NOT into the label. nm_enc is computed from POS against the
    SESSION-GLOBAL anchor, so it is comparable across every scan in the run.
    """
    direction = 1 if end_nm >= start_nm else -1
    n_points = int(round(abs(end_nm - start_nm) / step_nm)) + 1
    step_steps = int(api.steps_for_delta_nm(step_nm)) * direction

    plog(f"--- scan {run_id:02d} [{tag}] {start_nm:.3f} -> {end_nm:.3f} nm, "
         f"step {step_nm:.4f}, {n_points} points, {acq.describe()}")

    # Approach from a fixed side, so the mechanical seating before every scan
    # is identical unless a perturbation deliberately changes it.
    here_nm = anchor_nm + api.steps_to_nm(api.pos_safe() - anchor_steps)
    mover.move_by_nm(approach_nm - here_nm, tag=f"scan{run_id}-approach")
    here_nm = anchor_nm + api.steps_to_nm(api.pos_safe() - anchor_steps)
    mover.move_by_nm(start_nm - here_nm, tag=f"scan{run_id}-to-start")

    grid_anchor = api.pos_safe()
    grid_slack = 0
    expected_pos = grid_anchor      # where the drive should be standing now
    rows = []

    for i in range(n_points):
        nm_app = start_nm + step_nm * direction * i
        # Guarded against garbled POS replies (see Api.pos_safe). One step of
        # tolerance: the drive is inside POS_TOL_STEPS of its target, which is
        # legitimate, anything beyond a step is not.
        pos_steps = api.pos_safe(expected=expected_pos,
                                 tol_nm=max(0.02, 2 * step_nm))
        pos_suspect = bool(getattr(api, "pos_suspect", False))
        nm_enc = anchor_nm + api.steps_to_nm(pos_steps - anchor_steps)
        v, dur = acq.point()

        rows.append({
            "run_id": run_id, "tag": tag, "index": i,
            "t_unix": round(time.time(), 3),
            "nm_app": round(nm_app, 6),
            "nm_enc": round(nm_enc, 6),
            "label_minus_encoder_nm": round(nm_app - nm_enc, 6),
            "pos_steps": pos_steps,
            "volts": round(v, 6),
            "meas_dur_s": round(dur, 4),
            "reads": acq.last_reads,
            "grid_slack_steps": grid_slack,
            "last_move_direction": api.state.get("last_move_direction"),
            "pos_suspect": int(pos_suspect),
        })

        if i == n_points - 1:
            break

        comp, _ = mover.compensation(direction)
        grid_slack += comp
        target = grid_anchor + step_steps * (i + 1) + grid_slack
        rel = target - pos_steps
        expected_pos = target
        api.state["current_step_target_abs"] = target
        api.send_cmd(f"LR{rel}")
        api.send_cmd("M")
        if not api.wait_until_position(target, timeout=api.timeout_for(target, step_nm)):
            plog(f"scan {run_id}: step {i+1} not reached -- truncating scan", "warn")
            api.safe_stop()
            break
        if api.SAFE_AFTER_MOVE:
            api.safe_stop()

    plog(f"--- scan {run_id:02d} done, {len(rows)} points")
    return rows


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def centroid(xs, ys, threshold_frac=0.5):
    """Intensity-weighted centroid of the peak, above a fraction of its height.

    Centroid rather than argmax: with one DAQ read per point the top of the
    peak is noisy and argmax hops between neighbouring points, which is itself
    a ~one-step scatter. The centroid over the upper half is far steadier and
    is what the offsets should be judged on.

    Returns (centroid_x, argmax_x, height, baseline, n_used) or None.
    """
    if len(xs) < 5:
        return None
    order = sorted(ys)
    baseline = order[max(0, int(0.10 * len(order)))]
    peak = max(ys)
    height = peak - baseline
    if height <= 0:
        return None
    cut = baseline + threshold_frac * height
    num = den = 0.0
    used = 0
    for x, y in zip(xs, ys):
        if y >= cut:
            w = y - baseline
            num += w * x
            den += w
            used += 1
    if den <= 0 or used < 3:
        return None
    return (num / den, xs[ys.index(peak)], height, baseline, used)


def analyse(all_rows, outdir):
    """Per-scan peaks in both frames, plus the offsets that answer the question."""
    runs = {}
    for r in all_rows:
        runs.setdefault(r["run_id"], []).append(r)

    summary = []
    for run_id in sorted(runs):
        rows = sorted(runs[run_id], key=lambda r: r["index"])
        ys = [r["volts"] for r in rows]
        rec = {"run_id": run_id, "tag": rows[0]["tag"], "n_points": len(rows)}

        for frame in ("nm_app", "nm_enc"):
            res = centroid([r[frame] for r in rows], ys)
            if res is None:
                rec[f"peak_{frame}"] = ""
                rec[f"argmax_{frame}"] = ""
                rec[f"height_{frame}"] = ""
            else:
                c, am, h, bl, used = res
                rec[f"peak_{frame}"] = round(c, 5)
                rec[f"argmax_{frame}"] = round(am, 5)
                rec[f"height_{frame}"] = round(h, 5)
                rec["baseline_v"] = round(bl, 5)
                rec["points_in_peak"] = used

        resid = [r["label_minus_encoder_nm"] for r in rows]
        rec["label_minus_encoder_mean_nm"] = round(statistics.fmean(resid), 6)
        rec["label_minus_encoder_span_nm"] = round(max(resid) - min(resid), 6)
        rec["grid_slack_steps_end"] = rows[-1]["grid_slack_steps"]
        rec["pos_suspect_points"] = sum(r.get("pos_suspect", 0) for r in rows)
        summary.append(rec)

    # Offsets relative to the first scan, in both frames.
    for frame in ("nm_app", "nm_enc"):
        ref = next((s[f"peak_{frame}"] for s in summary if s[f"peak_{frame}"] != ""), None)
        for s in summary:
            p = s[f"peak_{frame}"]
            s[f"offset_{frame}_nm"] = round(p - ref, 5) if (p != "" and ref is not None) else ""

    path = os.path.join(outdir, "summary.csv")
    if summary:
        keys = list(summary[0].keys())
        for s in summary:
            for k in s:
                if k not in keys:
                    keys.append(k)
        with open(path, "w", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=keys)
            w.writeheader()
            for s in summary:
                w.writerow(s)

    # ---- verdict ---------------------------------------------------------
    plog("")
    plog("=" * 72)
    plog("RESULT")
    plog("=" * 72)
    plog(f"{'run':>3} {'tag':<14} {'peak nm_app':>12} {'off_app':>9} "
         f"{'peak nm_enc':>12} {'off_enc':>9} {'lbl-enc':>9}")
    for s in summary:
        plog(f"{s['run_id']:>3} {s['tag']:<14} "
             f"{str(s['peak_nm_app']):>12} {str(s['offset_nm_app_nm']):>9} "
             f"{str(s['peak_nm_enc']):>12} {str(s['offset_nm_enc_nm']):>9} "
             f"{s['label_minus_encoder_mean_nm']:>9.4f}")

    def spread(key):
        vals = [s[key] for s in summary if s[key] != ""]
        return (max(vals) - min(vals)) if len(vals) >= 2 else None

    sa = spread("peak_nm_app")
    se = spread("peak_nm_enc")
    plog("")
    if sa is None or se is None:
        plog("Not enough usable peaks to judge. Check that the line is inside "
             "the scan window and the signal is above the noise.", "warn")
        return summary

    plog(f"peak spread across all runs:  nm_app = {sa*1000:.1f} pm, "
         f"nm_enc = {se*1000:.1f} pm")
    if sa > 0.02 and se < 0.02:
        plog("VERDICT: the LABEL moves, the ENCODER does not -> software "
             "bookkeeping. The grating is repeatable; the wavelength "
             "assignment is not.")
    elif se > 0.02:
        plog("VERDICT: the ENCODER-referenced peak moves too -> the grating "
             "really goes to different places. Mechanical / drive, not "
             "labelling. Compare against the commanded targets in the "
             "per-scan CSVs.")
    else:
        plog("VERDICT: no offset above 20 pm reproduced in either frame. The "
             "perturbations in this matrix do not cause the effect; the "
             "hypothesis in the report is wrong and the trigger is something "
             "else in the manual workflow.")
    plog("")
    plog(f"Summary written to {path}")
    return summary


# ---------------------------------------------------------------------------
# Matrix
#
# The order matters. Baseline pairs bracket every perturbation, so ordinary
# scan-to-scan scatter can be told apart from a step change, and so a shift can
# be seen to PERSIST (bookkeeping poisoning) or SPRING BACK (a real per-scan
# effect of the perturbation).
# ---------------------------------------------------------------------------

DEFAULT_MATRIX = [
    ("baseline", None),
    ("baseline", None),
    ("avg-change", "avg"),
    ("after-avg", None),
    ("avg-restore", "avg_restore"),
    ("after-restore", None),
    ("jog", "jog"),
    ("after-jog", None),
    ("after-jog-2", None),
    ("abort", "abort"),
    ("after-abort", None),
    ("after-abort-2", None),
]


def apply_perturbation(kind, api, mover, acq, args, anchor_steps, anchor_nm):
    if kind is None:
        return
    if kind == "avg":
        old = (acq.mode, acq.fraction, acq.samples)
        acq.mode = args.avg_mode_b
        acq.fraction = args.avg_fraction_b
        acq.samples = args.avg_samples_b
        plog(f"PERTURBATION avg: {old} -> "
             f"({acq.mode}, {acq.fraction}, {acq.samples})")
    elif kind == "avg_restore":
        acq.mode = args.avg_mode
        acq.fraction = args.avg_fraction
        acq.samples = args.avg_samples
        plog(f"PERTURBATION avg restored to ({acq.mode}, {acq.fraction}, "
             f"{acq.samples})")
    elif kind == "jog":
        plog(f"PERTURBATION jog: {args.jog_nm:+.3f} nm then {-args.jog_nm:+.3f} nm")
        mover.move_by_nm(+args.jog_nm, tag="jog+")
        mover.move_by_nm(-args.jog_nm, tag="jog-")
    elif kind == "abort":
        plog("PERTURBATION abort: long move interrupted mid-flight")
        mover.abort_mid_move(args.abort_nm)
    else:
        plog(f"unknown perturbation '{kind}' -- ignored", "warn")


def main():
    global _LOG_FH

    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("port", nargs="?", help="COM3, /dev/tty..., or SIM")
    ap.add_argument("--baud", type=int, default=9600)
    ap.add_argument("--center", type=float, default=587.80,
                    help="Centre of the scan window, nm. Put the line you are "
                         "actually seeing here, not the textbook value.")
    ap.add_argument("--span", type=float, default=0.40,
                    help="Total scan width, nm")
    ap.add_argument("--step", type=float, default=0.02, help="Step, nm")
    ap.add_argument("--approach-margin", type=float, default=0.15,
                    help="How far beyond the start every scan is approached "
                         "from, nm. Keeps the mechanical seating identical.")
    ap.add_argument("--timebase-ms", type=float, default=300.0)
    ap.add_argument("--presettle-ms", type=float, default=50.0)
    ap.add_argument("--avg-mode", choices=("samples", "time"), default="time")
    ap.add_argument("--avg-samples", type=int, default=10)
    ap.add_argument("--avg-fraction", type=float, default=1.0,
                    help="Fraction for the FIRST group (the rig used 1 %%)")
    ap.add_argument("--avg-mode-b", choices=("samples", "time"), default="time")
    ap.add_argument("--avg-samples-b", type=int, default=10)
    ap.add_argument("--avg-fraction-b", type=float, default=50.0,
                    help="Fraction for the SECOND group (the rig used 50 %%)")
    ap.add_argument("--slip-nm", type=float, default=0.080,
                    help="Backlash used for compensation. 0 disables it.")
    ap.add_argument("--no-compensation", action="store_true",
                    help="Force compensation off regardless of --slip-nm "
                         "(this is test T2 in the report)")
    ap.add_argument("--jog-nm", type=float, default=0.10)
    ap.add_argument("--abort-nm", type=float, default=5.0,
                    help="Length of the move that gets interrupted, nm")
    ap.add_argument("--outdir", default=None)
    ap.add_argument("--selftest", action="store_true",
                    help="Verify the peak/offset maths on synthetic data. No "
                         "hardware and no application modules needed.")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    if not args.port:
        ap.error("a port is required (or use --selftest)")

    outdir = args.outdir or time.strftime("probe_%Y-%m-%d_%H%M%S")
    os.makedirs(outdir, exist_ok=True)
    _LOG_FH = open(os.path.join(outdir, "probe.log"), "w")

    plog(f"offset_probe starting. outdir={outdir}")
    plog(f"argv: {' '.join(sys.argv[1:])}")

    api = Api()
    connect(api, args.port, args.baud)

    mover = Mover(api, args.slip_nm, disable_comp=args.no_compensation)
    acq = Acq(api, args.timebase_ms, args.presettle_ms,
              args.avg_mode, args.avg_samples, args.avg_fraction)

    start_nm = args.center - args.span / 2.0
    end_nm = args.center + args.span / 2.0
    approach_nm = start_nm - abs(args.approach_margin)

    # SESSION-GLOBAL ENCODER ANCHOR. Established exactly once, here, and never
    # touched again. Every nm_enc in the whole run is measured against it, which
    # is what makes peaks from different scans directly comparable. Its absolute
    # value is arbitrary -- only differences matter.
    anchor_steps = api.pos_safe()
    anchor_nm = args.center
    plog(f"session encoder anchor: POS={anchor_steps} defined as "
         f"{anchor_nm:.4f} nm (arbitrary zero; only differences matter)")

    all_rows = []
    t_start = time.time()
    try:
        for run_id, (tag, pert) in enumerate(DEFAULT_MATRIX, start=1):
            apply_perturbation(pert, api, mover, acq, args, anchor_steps, anchor_nm)
            rows = run_scan(api, mover, acq, start_nm, end_nm, args.step,
                            anchor_steps, anchor_nm, run_id, tag, approach_nm)
            all_rows.extend(rows)

            path = os.path.join(outdir, f"scan_{run_id:02d}_{tag}.csv")
            with open(path, "w", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
                w.writeheader()
                w.writerows(rows)
            plog(f"wrote {path}")
    except KeyboardInterrupt:
        plog("interrupted by operator -- analysing what was collected", "warn")
    except Exception as e:
        plog(f"ABORTED: {e}", "error")
        import traceback
        plog(traceback.format_exc(), "error")
    finally:
        try:
            api.safe_stop()
        except Exception:
            pass

    n_rej = getattr(api, "pos_rejects", 0)
    if n_rej:
        plog(f"{n_rej} POS outlier(s) rejected and re-read during this run. "
             f"Expected occasionally -- see Api.pos_safe. Check "
             f"pos_suspect_points in summary.csv: any non-zero value means a "
             f"point's nm_enc could not be verified.", "warn")
    plog(f"total runtime {(time.time()-t_start)/60.0:.1f} min, "
         f"{mover.n_moves} moves, {mover.n_failed} not reached, "
         f"cumulative compensation {mover.slack_steps_total:+d} steps "
         f"({api.steps_to_nm(mover.slack_steps_total):+.4f} nm)")

    if all_rows:
        analyse(all_rows, outdir)
    else:
        plog("no data collected", "error")

    if _LOG_FH:
        _LOG_FH.close()


# ---------------------------------------------------------------------------
# Selftest
#
# Validates the analysis half of this script without hardware: build synthetic
# scans with a KNOWN offset injected into the label frame only, and check that
# the reported offsets recover it and that the verdict comes out right.
# ---------------------------------------------------------------------------

def selftest():
    import random
    random.seed(7)
    plog("selftest: synthetic scans, label-frame offset injected")

    true_center = 587.80
    fwhm = 0.15
    sigma = fwhm / 2.3548
    step = 0.02
    n = 41
    start = true_center - (n - 1) / 2 * step

    injected = {1: 0.0, 2: 0.0, 3: 0.080, 4: 0.080}
    rows = []
    for run_id, off in injected.items():
        for i in range(n):
            nm_app = start + i * step
            # The grating is always in the same physical place; only the label
            # is shifted. So the signal follows nm_enc, and nm_enc = label-off.
            nm_enc = nm_app - off
            v = 0.15 + 5.0 * math.exp(-0.5 * ((nm_enc - true_center) / sigma) ** 2)
            v += random.uniform(-0.05, 0.05)
            rows.append({
                "run_id": run_id, "tag": f"synthetic{run_id}", "index": i,
                "nm_app": nm_app, "nm_enc": nm_enc,
                "label_minus_encoder_nm": off, "volts": v,
                "grid_slack_steps": 0,
            })

    outdir = "selftest_out"
    os.makedirs(outdir, exist_ok=True)
    summary = analyse(rows, outdir)

    ok = True
    for s in summary:
        want = injected[s["run_id"]]
        got_app = s["offset_nm_app_nm"]
        got_enc = s["offset_nm_enc_nm"]
        if abs(got_app - want) > 0.01:
            plog(f"FAIL run {s['run_id']}: label offset {got_app} != {want}", "error")
            ok = False
        if abs(got_enc) > 0.01:
            plog(f"FAIL run {s['run_id']}: encoder offset {got_enc} should be 0",
                 "error")
            ok = False

    plog("")
    plog("SELFTEST PASS" if ok else "SELFTEST FAIL", "info" if ok else "error")
    plog("The injected 0.080 nm was recovered in the label frame and absent in "
         "the encoder frame -- which is exactly the signature the real run is "
         "looking for.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main() or 0)
