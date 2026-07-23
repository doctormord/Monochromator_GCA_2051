![Monochromator Slip Calibration UI](https://github.com/doctormord/Monochromator_GCA_2051/blob/QT6-future/images/Monochromator_Qt6.png)

# VRS41 Monochromator Controller

Control software for a grating monochromator driven by a FAULHABER brushless
servo (`CS-BX4`, integrated motor + controller, RS232), with an NI-DAQ analog
input reading the PMT.

Live plotting, stepped and continuous scans, configurable averaging, a scan
queue, backlash measurement and compensation, CSV export, and a hardware
simulator that runs the whole application with nothing attached.

---

## Install and run

```bash
python3 -m venv .venv && source .venv/bin/activate
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
calibration against a known line, and backlash compensation applied on every
direction reversal.

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
