#!/usr/bin/env python3
"""
Ad hoc SIM-mode probe -- NOT part of the delivered package.

Drives the REAL gui_main / scan_engine / motion code paths headlessly
(QT_QPA_PLATFORM=offscreen) against sim_hardware.FakeFaulhaber, to check
whether Defects A/B/C (documented in CONTEXT.md / BACKLOG.md P0) are already
observable in SIM before spending rig time on them. This mirrors the
CLAUDE.md-mandated "real script start, not just py_compile" verification,
plus scripted repro scenarios for the P0 wavelength-offset investigation.

Ground truth is always an ANCHORED conversion (anchor_steps/anchor_nm
captured at a known-good sync point, converted via the app's own
motion.steps_to_delta_nm -- same convention scan_engine's own [DIAG] logging
uses), never a bare steps/STEPS_PER_NM division -- POS has no absolute zero
and INVERT_DIRECTION flips sign, so a naive conversion is meaningless.

Each section is guarded by a faulthandler watchdog: if a section hangs, all
thread stacks are dumped and the process hard-exits, instead of hanging
silently forever.

RESULTS AS OF THE 2026-08-04 SIM SESSION (scan_engine.py/app_context.py
fixes applied, see BACKLOG.md P0):

- Defect A (scan interrupted mid-step, label never re-anchored) and
  Defect B (GoTo interrupted, retry computed from a stale label) are FIXED
  and confirmed here: after a Stop, the label converges exactly onto the
  encoder-frame truth (label - encoder_truth = +0.00000 in Section 3/4).
- A THIRD, previously undocumented bug was found while verifying B: Stop
  during a GoTo cleared state['stop_flag'] (via recover_after_stop(), fired
  by stop_action()'s background thread) before goto_worker's own
  wait_until_position() poll ever saw it -- because GoTo never set
  state['is_scanning'], the only flag stop_action() waited on before
  re-arming. GoTo's Stop therefore only took effect after the FULL move
  timeout (up to MAX_MOVE_TIMEOUT = 900 s), while the console immediately
  logged "Recovered after STOP; ready." as if it had worked. Fixed via a new
  state['is_moving'] flag (app_context.py), mirrored by is_scanning.
  A related fsm bug (recover_after_stop() resetting fsm to IDLE mid-retry,
  hiding that a second real move was still in flight) was fixed alongside it.
- Defect C is NOT a pure code bug in the way it first looked. Traced with a
  fully instrumented run (trace_backlash2.py pattern): for any reversal move
  that fully exceeds the modelled backlash width, sim_hardware's optical
  position converges to (pos_before + commanded_delta) REGARDLESS of the
  compensation value used -- so the label-vs-optical error scales with
  (configured slip_nm - true backlash), not with the backlash amount itself.
  Section 2 in this script still shows a clean +0.082 nm result, but that
  specific number is CONTAMINATED by the already-catalogued, deliberately
  untouched P3 bug ("scan overruns end by one step" -- BACKLOG.md P3): the
  extra step lands the read at a different point than the scan's true end.
  Applied fix: re-anchor the label against the (compensation-adjusted)
  encoder position after every GoTo and at scan end (goto_worker,
  scan_worker's while/else), mirroring free_run_worker's existing pattern --
  NOT blindly folding compensation into the label (that would reintroduce
  the historical free-run bug, "587.000 -> 586.918 nm", for a
  well-calibrated slip value). This is a real improvement (catches
  POS_TOL_STEPS-level drift, provides an explicit verification/logging
  safety net) but it is mathematically a NO-OP for a move that lands exactly
  on its commanded target -- it cannot and does not eliminate a genuine
  slip_nm miscalibration. That residual can only be reduced by verifying
  slip_nm against the real backlash (backlash_cal.py, at the rig), not by
  more code.
"""
import os
import sys
import time
import faulthandler

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

REPO = "/Users/christian/Downloads/Monochromator_GCA_2051-QT6-future"
sys.path.insert(0, REPO)

import app_context  # noqa: E402

LOG_LINES = []


def _sink(msg, tag="info"):
    LOG_LINES.append(f"[{tag}] {msg}")


app_context.set_log_sink(_sink)

import gui_main  # noqa: E402  (builds the whole Qt app as an import side effect)
import scan_engine  # noqa: E402
import motion  # noqa: E402
import device_constants as dc  # noqa: E402

# gui_main's import wires up ITS OWN console-widget log sink as a side
# effect (see gui_main.py's wiring block) -- silently overwriting the one
# registered above. Without this second call, every log() line from here on
# goes to a QPlainTextEdit nobody reads, and every "[DIAG]/[SLIP] lines from
# this scan:" section below prints nothing even though the app logged
# plenty (found the hard way: a whole scan ran correctly, confirmed via
# state/POS, while LOG_LINES stayed empty).
app_context.set_log_sink(_sink)

STEPS_PER_NM = dc.STEPS_PER_NM


def section(title, watchdog_s=25):
    faulthandler.cancel_dump_traceback_later()
    print("\n" + "=" * 78, flush=True)
    print(title, flush=True)
    print("=" * 78, flush=True)
    faulthandler.dump_traceback_later(watchdog_s, exit=True)


def wait_until(pred, timeout=15.0, poll=0.02):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if pred():
            return True
        time.sleep(poll)
    return False


def dump_recent_log(n=12):
    for line in LOG_LINES[-n:]:
        print("   ", line, flush=True)


def connect_sim():
    gui_main.entry_port.setText("SIM")
    gui_main.on_connect()
    ok = app_context.state.get("connected")
    print(f"connected={ok}  ser={app_context.state.get('ser')}", flush=True)
    return ok


def reference_run():
    motion.reference_run_action()
    wait_until(lambda: app_context.state.get("fsm") != "MOVING", timeout=10)
    print(f"after reference run: current_nm={app_context.state.get('current_nm')} "
          f"last_move_direction={app_context.state.get('last_move_direction')} "
          f"POS={motion.read_position()}", flush=True)


def do_goto(target_nm, timeout=20.0):
    gui_main.entry_goto.setText(f"{target_nm:.3f}")
    scan_engine.goto_wavelength_action()
    wait_until(lambda: app_context.state.get("fsm") != "MOVING", timeout=timeout)


def do_scan(start_nm, end_nm, step_nm, wait_ms=0.0, timeout=60.0):
    gui_main.entry_start.setText(f"{start_nm:.3f}")
    gui_main.entry_end.setText(f"{end_nm:.3f}")
    gui_main.entry_step.setText(f"{step_nm:.3f}")
    gui_main.entry_wait.setText(f"{wait_ms:.0f}")
    scan_engine.scan_action()
    wait_until(lambda: not app_context.state.get("is_scanning"), timeout=timeout)
    wait_until(lambda: app_context.state.get("fsm") != "SCAN", timeout=5)


def make_anchor(ser):
    """Capture a (steps, nm) sync point. Only valid right after a successful
    move, where state['current_nm'] and ser._pos are known to agree (goto
    success sets current_nm := target_nm exactly)."""
    return ser._pos, app_context.state["current_nm"]


def truth_nm(anchor, steps):
    anchor_steps, anchor_nm = anchor
    return anchor_nm + motion.steps_to_delta_nm(steps - anchor_steps)


# ---------------------------------------------------------------------------
section("1) CONNECT (SIM) + SMOKE TEST", watchdog_s=30)
# ---------------------------------------------------------------------------
assert connect_sim(), "SIM connect failed"
ser = app_context.state["ser"]
print(f"btn_scan.isEnabled()={gui_main.btn_scan.isEnabled()}  "
      f"btn_goto.isEnabled()={gui_main.btn_goto.isEnabled()}  "
      f"btn_stop.isEnabled()={gui_main.btn_stop.isEnabled()}", flush=True)

reference_run()
ref_anchor = make_anchor(ser)  # last_move_direction known, current_nm/POS in sync

# All test wavelengths are relative to wherever the app actually starts
# (persisted last_current_nm from vrs41_settings.json, NOT a hardcoded 500) --
# SIM move duration is real elapsed time (~0.77 nm/s at the capped sim speed,
# see sim_hardware._advance_time), so small relative deltas keep this script's
# runtime sane instead of accidentally commanding a 20+ nm jump.
base_nm = app_context.state["current_nm"]
print(f"base_nm (post-reference current_nm) = {base_nm:.4f}", flush=True)

print(f"-- running a short scan ({base_nm:.3f}->{base_nm+2:.3f}nm, step 0.5, no dwell) --", flush=True)
do_scan(base_nm, base_nm + 2.0, 0.5, wait_ms=0.0)
print(f"is_scanning={app_context.state.get('is_scanning')}  fsm={app_context.state.get('fsm')}  "
      f"btn_scan.isEnabled()={gui_main.btn_scan.isEnabled()}  "
      f"btn_pause.isEnabled()={gui_main.btn_pause.isEnabled()}", flush=True)
dump_recent_log(4)

# ---------------------------------------------------------------------------
section("2) DEFECT C repro: reversal on the scan's OWN first grid step", watchdog_s=60)
# ---------------------------------------------------------------------------
# Put the drive at base+5 nm with last_move_direction=+1 (Go To upward from
# wherever we are now). This GoTo's own success also gives us a fresh anchor.
do_goto(base_nm + 5.0)
anchor = make_anchor(ser)
print(f"pre-scenario: current_nm={app_context.state['current_nm']:.4f}  "
      f"last_move_direction={app_context.state.get('last_move_direction')}  "
      f"POS={ser._pos}  (anchor set here)", flush=True)

# Scan start (base+2) < current (base+5) -> jump direction is -1 (reversal
# from the +1 we just set). Scan direction itself (base+2 -> base+4) is +1 ->
# reversal again on the scan loop's OWN first grid step. That first step is
# the one where reversal_compensation_steps() folds slip steps into
# target_abs/grid_slack but (per Defect C) NOT into pos_nm_next -- so
# state["current_nm"] should drift away from the encoder/optical truth by the
# slip amount from here on.
LOG_LINES.clear()
do_scan(base_nm + 2.0, base_nm + 4.0, 1.0, wait_ms=0.0)
after_pos = ser._pos
after_optical = ser._optical_pos

label_nm = app_context.state["current_nm"]
encoder_truth_nm = truth_nm(anchor, after_pos)
optical_truth_nm = truth_nm(anchor, after_optical)

print(f"post-scan: label current_nm={label_nm:.4f}  "
      f"encoder_truth_nm={encoder_truth_nm:.4f}  optical_truth_nm={optical_truth_nm:.4f}  "
      f"optical_lag_steps={ser.optical_lag_steps()}", flush=True)
print(f"label - encoder_truth  = {label_nm - encoder_truth_nm:+.5f} nm", flush=True)
print(f"label - optical_truth  = {label_nm - optical_truth_nm:+.5f} nm  "
      f"(this is what a rig operator would see as 'the offset')", flush=True)

print("\n[DIAG]/[SLIP] lines from this scan:", flush=True)
for line in LOG_LINES:
    if "[DIAG] i=" in line or "[SLIP]" in line or "[DIAG] scan-start" in line:
        print("   ", line, flush=True)

# ---------------------------------------------------------------------------
section("3) DEFECT B repro: Stop mid-GoTo, then compare vs. encoder truth", watchdog_s=60)
# ---------------------------------------------------------------------------
do_goto(base_nm)
anchor = make_anchor(ser)
print(f"reseated at current_nm={app_context.state['current_nm']:.4f}  POS={ser._pos}  (anchor reset)", flush=True)

LOG_LINES.clear()
long_target = base_nm + 8.0   # ~8 nm move (~10s at capped sim speed): long enough to interrupt reliably
gui_main.entry_goto.setText(f"{long_target:.3f}")
scan_engine.goto_wavelength_action()

# Wait until the simulated move has actually started advancing, then let it
# get roughly 1/3 of the way there before hitting Stop -- driven off the
# SIMULATOR's own progress counters, not a guessed sleep duration.
wait_until(lambda: ser._move_total != 0, timeout=5.0)
target_total = abs(ser._move_total) if ser._move_total else 0


def one_third_done():
    if ser._move_total == 0 and ser._move_done == 0:
        return True  # already finished before we could interrupt
    return target_total and abs(ser._move_done) >= target_total / 3


wait_until(one_third_done, timeout=10.0, poll=0.005)

pos_at_stop_request = ser._pos
print(f"issuing Stop with move ~{abs(ser._move_done)}/{target_total} steps done "
      f"(POS={pos_at_stop_request}, truth_nm={truth_nm(anchor, pos_at_stop_request):.4f})", flush=True)

scan_engine.stop_action()
wait_until(lambda: app_context.state.get("fsm") != "MOVING", timeout=15.0)
time.sleep(0.3)  # let trailing recover_after_stop()/log lines land

pos_after_stop = ser._pos
label_after_stop = app_context.state.get("current_nm")
encoder_truth_after_stop = truth_nm(anchor, pos_after_stop)
print(f"after Stop settled: label current_nm={label_after_stop:.4f}  "
      f"POS={pos_after_stop}  encoder_truth_nm={encoder_truth_after_stop:.4f}", flush=True)
print(f"label - encoder_truth = {label_after_stop - encoder_truth_after_stop:+.5f} nm", flush=True)

print("\nrelevant log lines from the interrupted GoTo:", flush=True)
for line in LOG_LINES:
    if any(tag in line for tag in ("[STOP]", "[SLIP]", "[DONE]", "[ERROR]")):
        print("   ", line, flush=True)

# Now issue a SECOND Go To to a nearby target. Compare the ACTUAL step delta
# it commands against the delta expected from the freshly-read encoder
# position -- Defect B's stale-current_nm_ui-vs-fresh-current_steps bug means
# rel_steps is computed from the STALE UI label, not from where the drive
# actually is post-stop, so the commanded relative move overshoots/undershoots
# by exactly (stale_label_nm -> true_position_nm) worth of steps.
LOG_LINES.clear()
pos_before_second = ser._pos
label_before_second = app_context.state["current_nm"]
second_target = base_nm + 3.0
do_goto(second_target, timeout=20.0)
pos_after_second = ser._pos

# Expected steps IF the move correctly used the true pre-move position:
expected_steps_from_truth = motion.steps_for_delta_nm(second_target - truth_nm(anchor, pos_before_second))
# Expected steps IF the move used the (possibly stale) UI label instead:
expected_steps_from_label = motion.steps_for_delta_nm(second_target - label_before_second)
actual_steps = pos_after_second - pos_before_second

print(f"\nsecond GoTo (-> {second_target:.3f} nm) from POS={pos_before_second} "
      f"(true_nm={truth_nm(anchor, pos_before_second):.4f}, label_nm={label_before_second:.4f}):", flush=True)
print(f"   actual commanded delta                : {actual_steps:+d} steps", flush=True)
print(f"   expected IF based on TRUE position     : {expected_steps_from_truth:+d} steps", flush=True)
print(f"   expected IF based on (stale) UI label  : {expected_steps_from_label:+d} steps", flush=True)
print(f"   final label current_nm={app_context.state['current_nm']:.4f}, "
      f"final true_nm={truth_nm(anchor, pos_after_second):.4f}", flush=True)
print("[DONE/SLIP/ERROR/STOP] log lines from the second GoTo:", flush=True)
for line in LOG_LINES:
    if any(tag in line for tag in ("[DONE]", "[SLIP]", "[ERROR]", "[STOP]")):
        print("   ", line, flush=True)

# ---------------------------------------------------------------------------
section("4) DEFECT A repro: scan interrupted mid-step (label re-anchored?)", watchdog_s=45)
# ---------------------------------------------------------------------------
do_goto(base_nm)
anchor = make_anchor(ser)
print(f"reseated at current_nm={app_context.state['current_nm']:.4f}  POS={ser._pos}  (anchor reset)", flush=True)

LOG_LINES.clear()
gui_main.entry_start.setText(f"{base_nm:.3f}")
gui_main.entry_end.setText(f"{base_nm + 5.0:.3f}")
gui_main.entry_step.setText("0.500")
gui_main.entry_wait.setText("50")  # 50 ms dwell so the loop doesn't finish instantly
scan_engine.scan_action()

wait_until(lambda: app_context.state.get("is_scanning"), timeout=5.0)
time.sleep(0.4)  # let a few grid points complete, then interrupt mid-step
pos_at_stop_request = ser._pos
label_at_stop_request = app_context.state.get("current_nm")
scan_engine.stop_action()
wait_until(lambda: not app_context.state.get("is_scanning"), timeout=15.0)
time.sleep(0.3)

label_after_stop = app_context.state.get("current_nm")
pos_after_stop = ser._pos
truth_after_stop = truth_nm(anchor, pos_after_stop)
print(f"label just before Stop request: {label_at_stop_request:.4f} nm "
      f"(POS was {pos_at_stop_request}, true_nm was {truth_nm(anchor, pos_at_stop_request):.4f})", flush=True)
print(f"label AFTER scan_worker exits : {label_after_stop:.4f} nm  "
      f"encoder POS={pos_after_stop}  true_nm={truth_after_stop:.4f}", flush=True)
print(f"label - true_nm after stop    = {label_after_stop - truth_after_stop:+.5f} nm", flush=True)
print(f"label re-anchored to actual stop position? "
      f"{'NO -- label unchanged (Defect A)' if label_after_stop == label_at_stop_request else 'YES -- label moved after stop'}", flush=True)
print("\nrelevant log lines:", flush=True)
for line in LOG_LINES:
    if any(tag in line for tag in ("[STOP]", "[DONE]", "[ERROR]")):
        print("   ", line, flush=True)

faulthandler.cancel_dump_traceback_later()
print("\n" + "=" * 78, flush=True)
print("DONE", flush=True)
print("=" * 78, flush=True)
