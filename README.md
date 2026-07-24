![Monochromator Slip Calibration UI](https://github.com/doctormord/Monochromator_GCA_2051/blob/QT6-future/images/Monochromator_Qt6.png)

# VRS41 Monochromator Controller

Control software for a grating monochromator driven by a FAULHABER brushless
servo (`CS-BX4`, integrated motor + controller, RS232), with an NI-DAQ analog
input reading the PMT.

A Qt6 rewrite of an earlier Tkinter/matplotlib version — see
[What changed against the Tkinter version](#what-changed-against-the-tkinter-version)
for the measured differences.

---

## At a glance

| | |
|---|---|
| **Drive resolution** | 361 765 encoder steps per nm — one step is 2.8 fm of wavelength (120.6 motor turns per nm) |
| **Servo positioning** | The drive holds a 20-step corridor = 0.055 pm; the application's arrival tolerance is 10 pm |
| **Usable step size** | 0.01 nm routinely. 0.001 nm is 362 steps, 18× the servo corridor, so the hardware supports it — the arrival tolerance has to be tuned first (`step_resolution_test.py`) |
| **Scan speed** | 5 nm continuous sweep in ≈ 6.5 s at full speed; sweep speed adjustable from 1 rpm up to the configured maximum |
| **Sampling rate** | ≈ 19–20 Hz measured on this rig, limited by the NI-DAQ task setup, not by the plot |
| **Averaging** | Per point: pre-settle → averaging window → idle, all inside one *Timebase*. Fixed sample count or a fraction of the available time |
| **Backlash** | Measured on this rig at 0.08–0.09 nm. Measured automatically by the calibration routine and compensated on every direction reversal |
| **Plot** | Up to 16 distinguishable traces at once, live, with legend, crosshair readout and per-trace show/hide/delete |

### Accuracy — what is measured and what is not

Stated plainly, because a monochromator without an absolute position sensor
invites optimistic claims:

- **Wavelength is calculated, never read back.** The drive counts encoder
  steps. Every displayed wavelength derives from an anchor you set with
  *Known λ here*. Move the grating by hand and the reading is wrong until you
  re-anchor.
- **Repeatability was measured, not assumed.** Ten identical repeat scans
  showed a strictly monotonic drift of **+22.5 pm per cycle** over 200-step
  scans. Tracing it showed the drift scaled with the *number of steps*, not
  with the number of direction reversals — which ruled out backlash and
  identified a relative step chain in the scan loop as the cause. It now uses
  an absolute step grid, so a step that falls short is corrected by the next
  move instead of accumulating.
- **That fix is verified in simulation** (residual < 1 pm over five scans);
  **verification on the instrument is still outstanding.** See `BACKLOG.md`.

---

## Install and run

```bash
python3 -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python3 main.py
```

Requires Python 3.9+, PyQt6 and pyqtgraph. `nidaqmx` is optional — without it
the analog input is simulated (and clearly labelled as such).

Enter `SIM` as the serial port to run the full application against the
built-in simulator, no hardware needed.

---

## Documentation

**[MANUAL.md](MANUAL.md)** is the complete manual — every feature, what it
does, how to use it, what the log means, how to handle errors, and where
every constant lives. A rendered PDF is provided alongside it.

| Document | Purpose |
|---|---|
| `MANUAL.md` | User manual (single source; the PDF is generated from it) |
| `HANDOVER.md` | Architecture and current state of every module |
| `BACKLOG.md` | Open items, and the measurements behind past decisions |

---

## Feature overview

**Motion** — absolute Go To, jog, reference run to seat backlash, offset
calibration against a known line (default: He I D3 at 587.5618 nm), and
backlash compensation applied on every direction reversal.

**Scanning** — stepped scans on an absolute step grid (errors are corrected,
not accumulated), and a continuous Free Run for finding lines quickly.

**Averaging** — a per-point *Timebase* split into pre-settle, averaging and
idle. Two modes: a fixed number of samples, or a fraction of the available
time. The GUI translates the settings into milliseconds live and reports the
measured read rate.

**Scan queue** — each entry stores its own complete acquisition settings, so
a queue can mix different averaging setups. Entries can be updated in place.

**Live AI monitor** — continuous analog-input readout with a colour-coded bar
and max-hold, for adjusting the PMT without starting a scan. Polls only while
idle so it never competes with a running scan for the DAQ channel.

**Export** — CSV with a full metadata header, including whether the signal
came from real hardware or the simulator.

---

## What changed against the Tkinter version

This is a rewrite of an earlier Tkinter + matplotlib application (kept as
`gui_main_tk.py` for reference). The differences that matter:

### Plotting: measured, not asserted

The old redraw loop ran on the GUI thread every 66 ms and, on **every frame**,
re-set all series, called `relim()` + `autoscale_view()`, and rebuilt the
legend with `loc="best"` — which searches for the least-overlapping position
by comparing against all plotted data. Benchmarked against the current
pyqtgraph path with identical data:

| Scenario | Tkinter + matplotlib | Qt + pyqtgraph | Factor |
|---|---|---|---|
| 1 scan × 200 points | 21 ms | 1.2 ms | **18×** |
| 5 scans × 2000 points | 40 ms | 3.0 ms | **13×** |
| 10 scans × 2000 points (a repeat series) | 62 ms | 4.5 ms | **14×** |
| 10 scans × 10 000 points (fine 0.001 nm scans) | 128 ms | 8.5 ms | **15×** |

Both versions targeted 15 redraws per second, a 66 ms budget. The old one
already consumed nearly all of it with ten ordinary scans on screen, and
blew past it entirely on fine scans — 128 ms per frame means the GUI thread
is busy essentially all the time, so buttons (including **Stop**) respond
only between frames. The current path uses 4 % of the same budget, and only
redraws when data actually changed.

### Correctness fixes found along the way

Rewriting the motion and protocol layers surfaced a series of real defects.
Each was diagnosed from instrument logs rather than guessed; `BACKLOG.md`
records the measurements behind every one:

- **Scan drift.** Each step was commanded relative to the position measured
  just before it, so any shortfall was carried forward permanently. Now an
  absolute step grid, anchored once per scan.
- **`HP0` was never a motion command.** Sent since the original version
  alongside `EN`/`V0` in the belief that it was a "hold position" command, it
  actually writes the **limit-switch polarity mask** — a configuration write
  to the safety hardware on every connect and every stop.
- **Serial replies were parsed even when truncated.** A reply cut short by a
  pause became a shorter, perfectly parseable and completely wrong position.
  Replies must now be CR-terminated, gaps between bytes are tolerated, and
  the recovery path no longer issues an extra query on a link that is already
  failing.
- **Stop sometimes did nothing.** The re-arming step cleared the stop flag
  while the scan worker was still running, so the request could be missed
  entirely.
- **Free Run counted backlash compensation as optical travel**, so a sweep
  landed exactly one compensation short of its target and disagreed with a
  stepped scan of the same line.

### Everything else

- **Scan queue** where each entry carries its own acquisition settings, so one
  queue can mix different averaging setups.
- **Live analog-input monitor** with colour thresholds and max-hold, for
  adjusting the PMT without starting a scan.
- **Simulator with modelled mechanics** — including backlash — so positioning
  behaviour can be reproduced without hardware. The old simulator had ideal
  mechanics and derived its signal from the application's own bookkeeping,
  which made position bugs structurally invisible.
- **Simulated data is labelled everywhere** (status bar, log, CSV header), so
  a run recorded without a DAQ driver cannot be mistaken for a measurement.
- **Diagnostic scripts** for the drive configuration, serial link quality and
  achievable step resolution (below).
- Numeric readouts use tabular figures and pinned widths, so live values no
  longer jitter as digits change.

---

## Diagnostic tools

Standalone scripts, none of which writes anything permanent to the drive:

| Script | Purpose |
|---|---|
| `read_faulhaber_config.py` | Dump and decode the drive's entire configuration |
| `idle_poll_test.py` | Separate serial-link problems from motion-correlated ones |
| `step_resolution_test.py` | Determine the finest usable scan step and arrival tolerance |
| `probe_move_timing.py` | Measure real move and poll timing |
| `tune_ramp.py` | Experiment with ramp settings |

---

## Project layout

```
main.py                    entry point
gui_main.py                Qt GUI, plotting, adapters
scan_engine.py             scan/free-run/queue workers, CSV export
motion.py                  wavelength <-> steps, waits, compensation
protocol_faulhaber.py      RS232 protocol layer
daq.py                     NI-DAQ reads, averaging, simulator signal
sim_hardware.py            virtual drive (incl. modelled backlash)
device_constants.py        hardware and timing constants
app_config.py              persistent settings (vrs41_settings.json)
app_context.py             shared state, logging, UI hooks
backlash_cal.py            backlash calibration routine
```

---

## Important to know

This instrument has **no absolute position sensor**. Every wavelength shown
is calculated from an anchor you provide. After a restart, or if the grating
was moved by hand, re-anchor with *Known λ here* before trusting the
readings.

Endstops are enforced by the drive itself, not by this software. The
application's own endstop guard is deliberately left disabled — see the
Known Limits section of the manual.

---

## Configuration

Runtime settings live in `vrs41_settings.json` next to the program and are
written automatically. Hardware and timing constants live in
`device_constants.py`. The manual documents both in full.
