# VRS41 Monochromator — Test & Benchmark Plan
 
## Instrument and test record
 
Fill this cover block in once per test campaign. It identifies what was tested,
under what conditions, and against what reference — the same information a
calibration laboratory records, so the results are defensible later.
 
| Item | Entry |
|---|---|
| Instrument (UUT) | VRS41 grating monochromator |
| Drive | FAULHABER CS-BX4 (BL servo, RS232) |
| Software version / commit | |
| Serial port / baud | |
| Gearbox setting(s) tested | |
| Operator | |
| Date(s) of test | |
| Ambient temperature | °C |
| Reference source(s) | ☐ He (587.5618 nm) ☐ Hg ☐ Na ☐ H ☐ other |
| Reference wavelengths taken as true from | ☐ NIST ASD ☐ other |
| Signal source | ☐ NI-DAQ (real) ☐ simulated (results not valid for the instrument) |
 
**Traceability note.** The wavelength scale is anchored to known atomic
emission lines whose accepted air wavelengths are taken from a cited reference
(e.g. the NIST Atomic Spectra Database). The instrument has no absolute
position sensor of its own; every wavelength it reports is derived from that
anchor. State the reference used above — an uncertainty figure without a stated
reference is not meaningful.
 
**How results are reported.** Repeatability and accuracy figures in this plan
are to be reported as a value together with an **expanded uncertainty U at
coverage factor k = 2 (approximately 95 % confidence)**, following the GUM
(JCGM 100 / NIST TN 1297). The formulae and a worked example are in
[Appendix A](#appendix-a-statistics-and-uncertainty). Record enough raw data
(the individual runs, not just the summary) that the numbers can be
recomputed.
 
---
 
Purpose: characterise the instrument itself using the new software, before the
software is trusted for measurement. Each test states what it checks, how to
run it, what to record, and what a good result looks like. Work top to bottom;
later tests assume the earlier ones passed.
 
**Structure.** Sections 1–8 describe each measurement once, in detail. Section 9
is the part that gets *repeated*: a compact characterisation matrix to fill in
for **every gearbox setting** (150 / 200 / 400 / 1000) at **two or three speeds
each**. The detailed sections tell you how; the matrix is where the numbers
land. Do the full detailed pass once at gearbox 200 to learn the procedure,
then work the matrix for the other settings.
 
**How to record.** Keep one CSV/PNG per test where the software produces one
(the filename pattern already timestamps them), and fill in the result tables
below by hand or in a lab notebook. Note the ambient conditions and the date
at the start of a session — grating position repeatability is temperature
sensitive.
 
**Reference numbers for this rig** (from the drive configuration and prior
measurements; verify, don't assume):
 
| Quantity | Value |
|---|---|
| Steps per nm (gearbox 200) | 361 765 |
| One step | 2.76 fm of wavelength |
| Arrival tolerance `POS_TOL_STEPS` | 90 steps = 0.25 pm |
| Reference line | He I D3, 587.5618 nm (air) |
| Max sweep speed `RAMP_SP` | 10 000 rpm |
| Endstop inputs | 1 and 3, hard-blocking, active low; normal `OST` = `0x0500` |
 
> **Safety first.** The drive enforces the endstops in hardware, but the
> software's own endstop guard is disabled and both configured limit switches
> block the *same* direction of travel (`HD = 00101`). Treat one end of travel
> as **unprotected**. During the travel-range tests (section 2) approach every
> limit slowly and be ready to press Stop.
 
---
 
## 0. Session setup
 
Do this once at the start of every test session.
 
1. Note date, time, room temperature, and the gearbox setting (currently 200).
2. Start the software, connect, confirm the log shows
   `Motor initialized (EN, HP0, V0, ANSW, ramp)`. If `HP0` is missing, the
   endstop polarity compensation is off and section 2 will misbehave — stop and
   check `SEND_LEGACY_HP0`.
3. Confirm the signal source. For real measurements the log must say
   `nidaqmx driver present -- real measurements`. If it says SIMULATED, there
   is no DAQ and every reading below is fiction.
4. Do a **Reference Run**, then anchor the scale with **Known λ here** against
   a known line (He D3 is the default). Everything downstream depends on this.
---
 
## 1. Wavelength accuracy across the whole range
 
**Question:** does the monochromator's displayed wavelength match the true line
positions everywhere, or does the error grow toward the ends of the range —
i.e. is there a scale (grating) error on top of a simple offset?
 
This is the most important characterisation. An *offset* is harmless (one
calibration fixes it). A *scale* error means `STEPS_PER_NM` is slightly wrong
for this gearbox and the error accumulates linearly with distance from the
calibration point.
 
### 1a. Anchor, then check a distant line
 
1. Calibrate on He D3 (587.5618 nm) with a stepped scan: step 0.001 nm, span
   ±0.1 nm, and take the peak from the fit (the calibration dialog does this).
2. Without recalibrating, drive to a **different** known line as far away as a
   lamp allows — good choices: Hg 546.074 nm, Hg 435.833 nm, Na D 588.995 /
   589.592 nm, H-α 656.279 nm. Scan it the same way and record the measured
   peak.
3. Repeat for every line you can reach.
**Record** for each line: true λ, measured λ, error = measured − true, and the
distance from the He D3 anchor.
 
| Line | True λ (nm) | Measured λ (nm) | Error (pm) | Distance from anchor (nm) |
|---|---|---|---|---|
| He D3 (anchor) | 587.5618 | — | 0 | 0 |
| | | | | |
| | | | | |
| | | | | |
 
**Interpretation:**
- Error roughly **constant** across all lines → pure offset, already removed by
  the anchor. Good.
- Error **grows with distance** from the anchor → scale error. Plot error vs.
  distance; the slope in pm/nm is the fractional `STEPS_PER_NM` error. A slope
  of e.g. +5 pm/nm means `STEPS_PER_NM` is low by 5 ppm. This is the number
  that would justify a per-gearbox `STEPS_PER_NM` correction later.
- Error **non-linear** (curves) → grating non-linearity or a periodic drive
  error; note the shape, it is worth a closer look.
### 1b. Same line from both directions
 
At one convenient line, scan it once increasing in wavelength and once
decreasing. The difference between the two measured peaks is the residual
backlash *after* compensation. Expect it to be small (the compensation targets
~0.08–0.09 nm); a large or direction-dependent value means the slip constant
needs re-measuring (section 4).
 
| Line | Peak upward (nm) | Peak downward (nm) | Difference (pm) |
|---|---|---|---|
| | | | |
 
---
 
## 2. Travel range and endstops
 
**Question:** how far does the mechanism travel each way, do both endstops
trigger, and what does the software report when they do?
 
Approach each limit **slowly** — use Free Run at a low speed (e.g. 500 rpm) or
repeated small Go To steps — and watch the log.
 
### 2a. Find each end
 
1. From a mid-range position, drive toward increasing wavelength until motion
   stops or the drive faults. Record the wavelength reached and the exact log
   lines (`OST` value, any `[STOP]`/`[TIMEOUT]`/fault).
2. Return to mid-range, drive toward decreasing wavelength, record the same.
| End | Direction | Last λ reached (nm) | Log at the limit (`OST`, messages) |
|---|---|---|---|
| Upper | + | | |
| Lower | − | | |
 
**Interpretation:**
- Normal `OST` while free is `0x0500`. A limit switch is **active low**, so a
  pressed switch *clears* its bit — watch for `OST` changing away from `0x0500`
  (e.g. `0x0400` or `0x0100`), or for the drive refusing further motion in that
  direction.
- Remember only one direction is protected by both switches. If one end simply
  runs into a mechanical hard stop with no switch reaction, that is the
  unprotected end — note it explicitly and do not test it at speed.
### 2b. Usable optical range
 
Independently of the switches, note the wavelengths of the first and last
spectral lines you can still bring to a clean peak. That is the *usable* range
for measurement, which may be narrower than the mechanical range.
 
---
 
## 3. Repeatability
 
**Question:** if you leave and return to a line many times, how tightly does the
software land on the same wavelength? This is the number that decides what the
instrument is good for.
 
Three separate repeatabilities matter; test them separately.
 
### 3a. Go To repeatability (positioning only)
 
1. Pick a line. Go To a wavelength 2 nm below it, then Go To the line, take a
   stepped scan (0.001 nm, ±0.05 nm), record the peak.
2. Repeat **10×**, always approaching from the *same* side (2 nm below), so
   backlash is not in play.
3. Compute the spread of the 10 peaks.
| Run | Peak (nm) | | Run | Peak (nm) |
|---|---|---|---|---|
| 1 | | | 6 | |
| 2 | | | 7 | |
| 3 | | | 8 | |
| 4 | | | 9 | |
| 5 | | | 10 | |
 
Spread (max − min): <span class="fill"></span> pm &nbsp; Std. dev: <span class="fill"></span> pm
 
**Good result:** spread within a few pm. The encoder positioning was measured at
0–1 step (≤ 3 fm), so anything much larger than a few pm here is mechanical
(grating mount, gear play, thermal), not the servo.
 
### 3b. Bidirectional repeatability (positioning + backlash)
 
Same as 3a, but **alternate** the approach direction: odd runs approach from
2 nm below, even runs from 2 nm above. Compare the spread to 3a.
 
- Spread similar to 3a → backlash compensation is working.
- Even runs systematically offset from odd runs → residual uncompensated
  backlash; feeds into section 4.
### 3c. Scan-to-scan drift
 
This checks the fix for the accumulating scan drift, which is verified in
simulation but **not yet on the instrument**.
 
1. Queue **10 identical** stepped scans of the same line (e.g. 0.2 nm span,
   0.01 nm step, same averaging).
2. Run the queue. Autosave on.
3. From the 10 CSVs, take each peak and look at peak vs. scan number.
| Scan | Peak (nm) | Δ from scan 1 (pm) |
|---|---|---|
| 1 | | 0 |
| 2 | | |
| 3 | | |
| 4 | | |
| 5 | | |
| 6 | | |
| 7 | | |
| 8 | | |
| 9 | | |
| 10 | | |
 
**Good result:** no monotonic trend — the peaks scatter around a constant, they
do not walk in one direction. A steady climb of ~+2 pm per scan would mean the
drift is back; that is exactly what the absolute-grid fix is meant to prevent,
confirming its absence is the purpose of this test. Record the result
either way; this closes an open item.
 
---
 
## 4. Backlash characterisation
 
**Question:** what is the true mechanical backlash at this gearbox setting, and
is the compensation value right?
 
1. Use the calibration dialog (BACKLASH / SLIP): reference line He D3, span
   ±0.3 nm, step 0.01 nm. It sweeps the line up and down and reports the offset
   between the two peaks.
2. Run it **5×**. Record each value.
| Run | Measured backlash (nm) |
|---|---|
| 1 | |
| 2 | |
| 3 | |
| 4 | |
| 5 | |
 
Mean: <span class="fill"></span> nm &nbsp; Spread: <span class="fill"></span> nm
 
**Interpretation:** the value should be repeatable to a few pm and land near the
current compensation setting. If the mean differs from what is configured,
apply and save it (the dialog offers this). Note that backlash may **change**
with the gearbox setting — re-run this after any gearbox change (section 5).
 
---
 
## 5. Gearbox settings (manual change, config only — no code change)
 
The mechanism has a hand-switchable gearbox: 150, 200, 400, 1000. Changing it
changes how many motor turns correspond to one nm, i.e. `STEPS_PER_NM`. The
software does not manage this yet, but it can be tested **by editing one
constant** and restarting. Nothing in the program needs changing.
 
**Predicted `STEPS_PER_NM`** (scaling the measured 200 value linearly — this is
the hypothesis under test, not a guarantee):
 
| Gearbox | Predicted `STEPS_PER_NM` | Factor vs. 200 |
|---|---|---|
| 150 | 271 324 | 0.75× |
| 200 | 361 765 | 1.00× (measured) |
| 400 | 723 530 | 2.00× |
| 1000 | 1 808 825 | 5.00× |
 
> **Watch the motor and gearbox, and adapt the ramp.** Speed and acceleration
> are set in motor rpm, but what the *mechanism* feels depends on the gearbox.
> A finer gearbox (400, 1000) multiplies the torque the motor has to deliver to
> hit the same rpm ramp, and a coarser one (150) lets the grating move faster
> per motor turn than at 200. The AC/DEC/SP values that were fine at 200 can
> therefore over-stress the motor or the gear train at another setting, or
> simply sound and feel wrong. **After every gearbox change, before running the
> range and repeatability tests, sanity-check the ramp:** start conservative
> (lower SP and gentler AC/DEC), make a few moves, and listen — no stalling, no
> harsh jerk at the start of a move, no whine at speed. `tune_ramp.py` sets
> candidate values in RAM only (never to EEPROM), so it is safe to experiment.
> Only once a setting moves smoothly, raise SP toward what the throughput test
> (section 8) shows is actually useful. Record the AC/DEC/SP you settle on per
> gearbox in the results table.
 
### Procedure for each gearbox setting
 
1. Switch the gearbox by hand to the new setting.
2. In `device_constants.py` set `STEPS_PER_NM` to the predicted value for that
   setting. Save, restart the software.
3. **Check and, if needed, retune the ramp for this gearbox** (see the warning
   above and section 8a) before driving any distance.
4. Reference Run, then anchor on He D3.
5. **Verify the scaling is right** before trusting anything: Go To a line about
   1 nm away and check it lands on the true wavelength. If it overshoots or
   undershoots by a fixed *fraction*, the constant is off — adjust
   `STEPS_PER_NM` until a 1 nm commanded move produces a true 1 nm optical move.
   The correct value is the one where two lines a known distance apart come out
   the right distance apart.
6. Once verified, note the *calibrated* `STEPS_PER_NM` for that gearbox.
7. Re-run the quick versions of sections 1a (two lines), 3a (repeatability),
   4 (backlash) and 8 (throughput) — every mechanical property can change with
   the gearbox.
**Record** the calibrated constant per setting. Do not fill it in here — every
per-gearbox result, including the calibrated `STEPS_PER_NM`, has its own
pre-printed block in the **characterisation matrix (section 11)**. That is the
single place all gearbox results are collected, and the basis for adding
gearbox support to the software later.
 
> Finer gearboxes (400, 1000) give more steps per nm, so a smaller step is
> physically possible — but check the arrival tolerance still makes sense
> (`POS_TOL_STEPS` is in *steps*, so its pm value shrinks automatically as
> `STEPS_PER_NM` grows, which is the right direction). Also re-run the optical
> fine-step check (section 6) at the finer settings, where it matters most.
 
---
 
## 6. Smallest genuinely useful step (optical, not encoder)
 
**Question:** the encoder positions to 0–1 step, but does the *grating* actually
move at the finest steps, or does stiction leave it parked while the encoder
advances?
 
1. Take a sharp line (He D3). Scan it with a **very fine** step — 0.001 nm, then
   0.0005 nm, then 0.0002 nm — over ±0.03 nm, with generous averaging.
2. Look at the peak: is it still cleanly resolved, or does the line smear /
   flat-top / break into steps as the step size drops?
**Interpretation:** the step size at which the peak stops sharpening, or starts
looking stepped, is the real optical resolution limit for this gearbox — as
opposed to the encoder limit, which is far finer. Record it per gearbox; it is
the smallest optically useful step for this gearbox — the encoder can resolve far finer, but that is not the same as the grating moving.
 
| Gearbox | Finest step still clean (nm) |
|---|---|
| 200 | |
| | |
 
---
 
## 7. Speed vs. quality (Free Run)
 
**Question:** how does sweep speed trade against point density and peak fidelity?
 
1. Free Run the same line at several speeds (e.g. 10, 100, 1000, 5000, 10000
   rpm). Record points captured and the peak wavelength each time.
2. Compare against a slow stepped scan of the same line as the reference truth.
 
| Speed (rpm) | Points | Peak (nm) | Δ from stepped reference (pm) |
|---|---|---|---|
| 10 | | | |
| 100 | | | |
| 1000 | | | |
| 5000 | | | |
| 10000 | | | |
 
**Interpretation:** the slowest speeds give many points and should agree closely
with the stepped scan; the fastest give a coarse, unevenly spaced sweep good
only for *finding* a line. The crossover — where the free-run peak starts to
disagree with the stepped reference — is the fastest speed still usable for a
rough measurement.
 
---
 
## 8. Throughput and ramp per speed and gearbox
 
**Question:** how many nm per second does the mechanism actually cover at a given
speed and gearbox, and what ramp runs cleanly? This turns "10000 rpm" into a
real nm/s figure and confirms the motor is not being over-driven.
 
Two helper scripts do the measuring. Both talk to the drive directly, so run
them with the software **closed** (only one program can hold the serial port).
Replace `COM3` with the actual port.
 
### 8a. Read and, if needed, retune the ramp — `tune_ramp.py`
 
First just read what the drive currently uses:
 
```
python tune_ramp.py COM3
```
 
Then try a candidate acceleration / deceleration / speed and watch one
back-and-forth test move. `--set` takes `key=value` tokens (`ac=`, `dec=`,
`sp=`, any subset, space-separated and quoted); `--move` is the test distance
in steps:
 
```
python tune_ramp.py COM3 --set "ac=50 dec=100 sp=10000" --move 200000   # current 200-gearbox baseline
python tune_ramp.py COM3 --set "ac=30 dec=60 sp=4000"   --move 100000   # gentler, e.g. after switching to 400
```
 
Values are applied in **RAM only** — they revert on the next power cycle and
nothing is written to EEPROM. Start gentle at a new gearbox and work up. Once a
setting moves smoothly, put it in `device_constants.py` (`RAMP_AC` / `RAMP_DEC`
/ `RAMP_SP`, with `USE_MOTION_RAMP = True`) so the app applies it on every
connect — same effect, and no EEPROM wear.
 
**Listen while testing:** a stall, a harsh jerk at move start, or a whine at
speed means AC/DEC/SP is too aggressive for this gearbox — back it off.
 
### 8b. Measure move timing — `probe_move_timing.py`
 
This reports how long moves actually take at a given ramp, and the poll cadence,
from which nm/s follows. Run it at each speed you care about:
 
```
python probe_move_timing.py COM3 --sp 10000 --ac 50 --dec 100 --moves 5 --repeat 3
python probe_move_timing.py COM3 --sp 5000  --ac 50 --dec 100 --moves 5 --repeat 3
python probe_move_timing.py COM3 --sp 1000  --ac 30 --dec 60  --moves 5 --repeat 3
```
 
Use the same AC/DEC you settled on in 8a. `--moves` is how many moves per run,
`--repeat` how many runs to average. The output includes the per-move time; the
commanded distance divided by that time is the throughput.
 
### 8c. Cross-check against a real Free Run
 
As an end-to-end check, do a Free Run over a known span in the software at the
same speed and divide the span by the elapsed time (the log timestamps the start
and end). It should agree with 8b.
 
**Record** per gearbox and speed:
 
| Gearbox | Speed (rpm) | AC/DEC | Move time (s) for known distance | Throughput (nm/s) | Sounds/feels OK? |
|---|---|---|---|---|---|
| 200 | 10000 | 50/100 | | | |
| 200 | 5000 | 50/100 | | | |
| 200 | 1000 | 30/60 | | | |
| 400 | | | | | |
| 1000 | | | | | |
 
**Interpretation:** throughput should rise with speed until the ramp (AC/DEC)
starts to dominate on short moves — past that, a higher SP buys little because
the move spends most of its time accelerating and decelerating. The useful top
speed is where nm/s stops improving *and* the mechanism still sounds healthy.
At finer gearboxes the same nm/s needs a higher rpm (more motor turns per nm),
so watch the motor there in particular.
 
---
 
## 9. Motor and gearbox load, temperature, and lag
 
**Question:** at each gearbox and speed, does the drive keep up, and does it run
within thermal and mechanical limits — or does it lag, over-heat, or over-load
the gear train?
 
This runs alongside the throughput test (section 8); do them together.
 
### 9a. Motor / drive temperature
 
The footer shows the drive housing temperature (TEM), which tracks the motor
housing on this hardware. The drive also raises an `OVERTEMP` bit in `OST` if it
gets too hot.
 
1. Note the temperature at rest before starting (cold baseline).
2. Run a demanding sequence at the speed under test — e.g. ten full-range
   sweeps back to back, or a long queue — and watch the footer.
3. Record the temperature at the start, after 5 minutes, and after 10 minutes,
   and whether it plateaus or keeps climbing.
| Gearbox | Speed (rpm) | T cold (°C) | T @5 min (°C) | T @10 min (°C) | Plateau? | `OVERTEMP` seen? |
|---|---|---|---|---|---|---|
| | | | | | | |
 
**Interpretation:** a temperature that plateaus is fine; one that keeps climbing
means that duty cycle is too much for that gearbox/speed — reduce speed, gentle
the ramp, or add idle time between scans. Any `OVERTEMP` in the log is a hard
stop: back off immediately. Finer gearboxes (400, 1000) demand more torque and
will run hotter at the same nm/s — watch them most closely.
 
### 9b. Following error / lag (does the measurement keep up?)
 
At speed, two things can lag: the *drive* behind its commanded position
(a servo following error), and the *measurement* behind the true position (the
DAQ read pinned to a position that has already moved on). Both smear a line.
 
1. Scan the reference line as a slow **stepped** scan — this is the truth, the
   drive is stationary at each point.
2. Free Run the same line at each test speed.
3. Overlay the peaks. A free-run peak that sits *late* (shifted in the sweep
   direction) and grows more shifted at higher speed is lag — either the servo
   or the acquisition not keeping up.
| Gearbox | Speed (rpm) | Peak shift vs. stepped (pm) | Grows with speed? |
|---|---|---|---|
| | | | |
 
**Interpretation:** a shift that scales with speed and flips sign when you
reverse the sweep direction is acquisition lag (the reading is tied to a
position already left behind). A shift that does *not* flip with direction is
more likely a real servo following error. Note which; it decides whether the
fix is on the DAQ side (section 10) or the ramp side (section 8a). The
stepped-scan peak is unaffected by either, which is why it is the reference.
 
### 9c. Smallest step and slip, per gearbox
 
The finest usable step and the backlash both change with the gearbox. Rather
than repeat the full sections 4 and 6 prose here, the matrix (section 11)
collects them per setting. Use `step_resolution_test.py` for the encoder side:
 
```
python step_resolution_test.py COM3 --steps 0.01 0.001 0.0001 --moves 20 --csv steps_g200.csv
```
 
and an optical fine-step scan (section 6) for the grating side. At finer
gearboxes the same nm step is more encoder steps, so the encoder side gets
*easier* while the optical side is what to watch.
 
---
 
## 10. DAQ settings — getting more or better data
 
**Question:** are the NI-DAQ acquisition settings right, and can more or cleaner
data be pulled without changing the software?
 
The relevant constants (in `device_constants.py`, edited by hand — no code
change) are `DAQ_SAMPLES_PER_READ` (currently 10) and `DAQ_SAMPLE_RATE_HZ`
(currently 1000). One software "read" is that many hardware samples at that
rate, so today one read spans 10 ms of signal.
 
### 10a. Effect of the hardware read settings
 
Vary the two constants and observe the effect on a fixed scan of the reference
line (same step, same timebase, same speed):
 
| `DAQ_SAMPLES_PER_READ` | `DAQ_SAMPLE_RATE_HZ` | One read spans | Noise per point (mV) | Poll rate (Hz) | Notes |
|---|---|---|---|---|---|
| 10 | 1000 | 10 ms | | | baseline |
| 50 | 1000 | 50 ms | | | more averaging per read |
| 10 | 10000 | 1 ms | | | faster, noisier |
| 100 | 10000 | 10 ms | | | more samples, same span |
 
**Interpretation:** more samples per read, or a longer span, lowers the noise per
point but slows the poll loop — which matters for Free Run point density and for
lag (section 9b). The goal is the lowest noise that still keeps the poll rate
high enough for the fastest sweep you want to use. Record the trade at each
gearbox if it differs.
 
### 10b. Where should the averaging happen?
 
There are two places to average; this test establishes which is better for
this signal:
 
- **In the software scan loop** — the current approach: the DAQ delivers reads,
  the scan engine averages them per point (samples mode or time mode). Flexible,
  visible in the GUI, adjustable per queue entry.
- **In the DAQ itself** — configure the National Instruments task to average or
  oversample in hardware before it hands a value to the software. Potentially
  cleaner (hardware-timed, no Python jitter) but fixed, and set outside this
  application.
**Test:** measure the same line three ways and compare noise per point and total
time:
1. Software averaging, N samples (current default).
2. Fewer software samples but a larger `DAQ_SAMPLES_PER_READ` (more hardware
   averaging per read).
3. If the DAQ configuration allows it, hardware oversampling/averaging set on
   the device, with software averaging minimal.
| Method | Noise per point (mV) | Time per point (ms) | Notes |
|---|---|---|---|
| Software averaging (N=8) | | | current default |
| Hardware read = 50, software N=2 | | | |
| DAQ hardware averaging | | | if supported by the card |
 
**Interpretation:** if hardware averaging gives the same or lower noise for less
total time, it is worth moving some averaging into the DAQ — but that is a
future software change, not part of this test. Here we only *measure* which is
better and by how much, so the decision is informed. **Do not change the
software for this** — vary only the constants and, for method 3, the DAQ task
configuration outside the app.
 
---
 
## 11. Per-gearbox characterisation matrix
 
This is the part that is repeated for each gearbox. There is one
**pre-printed block per gearbox setting** below — the four known ratios plus
two blank blocks for any ratio not listed here. Fill in the block for each
gearbox you test; leave the rest empty.
 
Each block has two tables:
 
- a small **positioning/optics** table for the things that depend only on the
  gearbox, not on speed (scale error, smallest step, encoder error);
- a wider **per-speed** table where every speed-dependent property — including
  repeatability, backlash accuracy, throughput, temperature and lag — gets its
  own row. Fill one row per speed you run (2–3 speeds is the target; there is
  room for four).
The detailed sections above say *how* to obtain each number; here you only
record. The predicted `STEPS_PER_NM` is filled in for you as a starting point —
overwrite it with the value you actually calibrate.
 
> **Before starting any gearbox block:** switch the gearbox by hand, set the
> predicted `STEPS_PER_NM` in `device_constants.py` (section 5), retune and
> verify the ramp for this gearbox (section 8a — a finer gearbox needs a
> gentler ramp so the motor and gear train are not overloaded), do a Reference
> Run, and anchor on He D3. Only then take the numbers.
 
The per-speed columns, once, so you know what each block's wide table wants:
 
| Column | Meaning | Section |
|---|---|---|
| Speed (rpm) | the commanded SP for this row | — |
| Throughput (nm/s) | measured sweep rate at this speed | 8b |
| Go To repeat. (pm) | one-sided positioning spread, 10 moves at this speed | 3a |
| Bidir. repeat. (pm) | spread with alternating approach, at this speed | 3b |
| Backlash hit (pm) | how precisely the slip compensation lands, at this speed | 1b / 4 |
| Drift (pm/scan) | scan-to-scan walk over 10 scans at this speed | 3c |
| Motor T @10 min (°C) | drive housing temperature after sustained running | 9a |
| Lag (pm) | free-run peak shift vs. a slow stepped scan | 9b |
| OK? | sounds/feels healthy, no stall/whine/overtemp | 8a / 9a |
 
---
 
### Gearbox 100    ·  predicted `STEPS_PER_NM` = 180 882  ·  calibrated = <span class="fill"></span> · date <span class="fill"></span>
 
Ramp used: AC = <span class="fill"></span> DEC = <span class="fill"></span> SP(max) = <span class="fill"></span> rpm &nbsp; Runs cleanly? ☐ yes ☐ no
 
Gearbox-only (speed-independent):
 
| Scale error (pm/nm) | Offset after cal. (pm) | Smallest clean optical step (nm) | Encoder err. @0.001 nm (steps) | Backlash mean (nm) |
|---|---|---|---|---|
| | | | | |
 
Per speed:
 
| Speed (rpm) | Throughput (nm/s) | Go To rep. (pm) | Bidir. rep. (pm) | Backlash hit (pm) | Drift (pm/scan) | Motor T (°C) | Lag (pm) | OK? |
|---|---|---|---|---|---|---|---|---|
| | | | | | | | | |
| | | | | | | | | |
| | | | | | | | | |
| | | | | | | | | |
 
Notes: <span class="fill wide"></span>
 
---
 
### Gearbox 150    ·  predicted `STEPS_PER_NM` = 271 324  ·  calibrated = <span class="fill"></span> · date <span class="fill"></span>
 
Ramp used: AC = <span class="fill"></span> DEC = <span class="fill"></span> SP(max) = <span class="fill"></span> rpm &nbsp; Runs cleanly? ☐ yes ☐ no
 
Gearbox-only (speed-independent):
 
| Scale error (pm/nm) | Offset after cal. (pm) | Smallest clean optical step (nm) | Encoder err. @0.001 nm (steps) | Backlash mean (nm) |
|---|---|---|---|---|
| | | | | |
 
Per speed:
 
| Speed (rpm) | Throughput (nm/s) | Go To rep. (pm) | Bidir. rep. (pm) | Backlash hit (pm) | Drift (pm/scan) | Motor T (°C) | Lag (pm) | OK? |
|---|---|---|---|---|---|---|---|---|
| | | | | | | | | |
| | | | | | | | | |
| | | | | | | | | |
| | | | | | | | | |
 
Notes: <span class="fill wide"></span>
 
---
 
### Gearbox 200    ·  predicted `STEPS_PER_NM` = 361 765 (measured baseline)  ·  calibrated = <span class="fill"></span> · date <span class="fill"></span>
 
Ramp used: AC = <span class="fill"></span> DEC = <span class="fill"></span> SP(max) = <span class="fill"></span> rpm &nbsp; Runs cleanly? ☐ yes ☐ no
 
Gearbox-only (speed-independent):
 
| Scale error (pm/nm) | Offset after cal. (pm) | Smallest clean optical step (nm) | Encoder err. @0.001 nm (steps) | Backlash mean (nm) |
|---|---|---|---|---|
| | | | | |
 
Per speed:
 
| Speed (rpm) | Throughput (nm/s) | Go To rep. (pm) | Bidir. rep. (pm) | Backlash hit (pm) | Drift (pm/scan) | Motor T (°C) | Lag (pm) | OK? |
|---|---|---|---|---|---|---|---|---|
| | | | | | | | | |
| | | | | | | | | |
| | | | | | | | | |
| | | | | | | | | |
 
Notes: <span class="fill wide"></span>
 
---
 
### Gearbox 250    ·  predicted `STEPS_PER_NM` = 452 206  ·  calibrated = <span class="fill"></span> · date <span class="fill"></span>
 
Ramp used: AC = <span class="fill"></span> DEC = <span class="fill"></span> SP(max) = <span class="fill"></span> rpm &nbsp; Runs cleanly? ☐ yes ☐ no
 
Gearbox-only (speed-independent):
 
| Scale error (pm/nm) | Offset after cal. (pm) | Smallest clean optical step (nm) | Encoder err. @0.001 nm (steps) | Backlash mean (nm) |
|---|---|---|---|---|
| | | | | |
 
Per speed:
 
| Speed (rpm) | Throughput (nm/s) | Go To rep. (pm) | Bidir. rep. (pm) | Backlash hit (pm) | Drift (pm/scan) | Motor T (°C) | Lag (pm) | OK? |
|---|---|---|---|---|---|---|---|---|
| | | | | | | | | |
| | | | | | | | | |
| | | | | | | | | |
| | | | | | | | | |
 
Notes: <span class="fill wide"></span>
 
---
 
### Gearbox 400    ·  predicted `STEPS_PER_NM` = 723 530  ·  calibrated = <span class="fill"></span> · date <span class="fill"></span>
 
Ramp used: AC = <span class="fill"></span> DEC = <span class="fill"></span> SP(max) = <span class="fill"></span> rpm &nbsp; Runs cleanly? ☐ yes ☐ no
 
Gearbox-only (speed-independent):
 
| Scale error (pm/nm) | Offset after cal. (pm) | Smallest clean optical step (nm) | Encoder err. @0.001 nm (steps) | Backlash mean (nm) |
|---|---|---|---|---|
| | | | | |
 
Per speed:
 
| Speed (rpm) | Throughput (nm/s) | Go To rep. (pm) | Bidir. rep. (pm) | Backlash hit (pm) | Drift (pm/scan) | Motor T (°C) | Lag (pm) | OK? |
|---|---|---|---|---|---|---|---|---|
| | | | | | | | | |
| | | | | | | | | |
| | | | | | | | | |
| | | | | | | | | |
 
Notes: <span class="fill wide"></span>
 
---
 
### Gearbox 500    ·  predicted `STEPS_PER_NM` = 904 412  ·  calibrated = <span class="fill"></span> · date <span class="fill"></span>
 
Ramp used: AC = <span class="fill"></span> DEC = <span class="fill"></span> SP(max) = <span class="fill"></span> rpm &nbsp; Runs cleanly? ☐ yes ☐ no
 
Gearbox-only (speed-independent):
 
| Scale error (pm/nm) | Offset after cal. (pm) | Smallest clean optical step (nm) | Encoder err. @0.001 nm (steps) | Backlash mean (nm) |
|---|---|---|---|---|
| | | | | |
 
Per speed:
 
| Speed (rpm) | Throughput (nm/s) | Go To rep. (pm) | Bidir. rep. (pm) | Backlash hit (pm) | Drift (pm/scan) | Motor T (°C) | Lag (pm) | OK? |
|---|---|---|---|---|---|---|---|---|
| | | | | | | | | |
| | | | | | | | | |
| | | | | | | | | |
| | | | | | | | | |
 
Notes: <span class="fill wide"></span>
 
---
 
### Gearbox 1000   ·  predicted `STEPS_PER_NM` = 1 808 825  ·  calibrated = <span class="fill"></span> · date <span class="fill"></span>
 
Ramp used: AC = <span class="fill"></span> DEC = <span class="fill"></span> SP(max) = <span class="fill"></span> rpm &nbsp; Runs cleanly? ☐ yes ☐ no
 
Gearbox-only (speed-independent):
 
| Scale error (pm/nm) | Offset after cal. (pm) | Smallest clean optical step (nm) | Encoder err. @0.001 nm (steps) | Backlash mean (nm) |
|---|---|---|---|---|
| | | | | |
 
Per speed:
 
| Speed (rpm) | Throughput (nm/s) | Go To rep. (pm) | Bidir. rep. (pm) | Backlash hit (pm) | Drift (pm/scan) | Motor T (°C) | Lag (pm) | OK? |
|---|---|---|---|---|---|---|---|---|
| | | | | | | | | |
| | | | | | | | | |
| | | | | | | | | |
| | | | | | | | | |
 
Notes: <span class="fill wide"></span>
 
---
 
### Gearbox <span class="fill"></span> ·  predicted `STEPS_PER_NM` = <span class="fill"></span> · calibrated = <span class="fill"></span> · date <span class="fill"></span>
 
*(blank block for any other ratio — predicted `STEPS_PER_NM` = 361 765 × ratio ÷ 200)*
 
Ramp used: AC = <span class="fill"></span> DEC = <span class="fill"></span> SP(max) = <span class="fill"></span> rpm &nbsp; Runs cleanly? ☐ yes ☐ no
 
Gearbox-only (speed-independent):
 
| Scale error (pm/nm) | Offset after cal. (pm) | Smallest clean optical step (nm) | Encoder err. @0.001 nm (steps) | Backlash mean (nm) |
|---|---|---|---|---|
| | | | | |
 
Per speed:
 
| Speed (rpm) | Throughput (nm/s) | Go To rep. (pm) | Bidir. rep. (pm) | Backlash hit (pm) | Drift (pm/scan) | Motor T (°C) | Lag (pm) | OK? |
|---|---|---|---|---|---|---|---|---|
| | | | | | | | | |
| | | | | | | | | |
| | | | | | | | | |
| | | | | | | | | |
 
Notes: <span class="fill wide"></span>
 
---
 
### Gearbox <span class="fill"></span> ·  predicted `STEPS_PER_NM` = <span class="fill"></span> · calibrated = <span class="fill"></span> · date <span class="fill"></span>
 
*(blank block for any other ratio)*
 
Ramp used: AC = <span class="fill"></span> DEC = <span class="fill"></span> SP(max) = <span class="fill"></span> rpm &nbsp; Runs cleanly? ☐ yes ☐ no
 
Gearbox-only (speed-independent):
 
| Scale error (pm/nm) | Offset after cal. (pm) | Smallest clean optical step (nm) | Encoder err. @0.001 nm (steps) | Backlash mean (nm) |
|---|---|---|---|---|
| | | | | |
 
Per speed:
 
| Speed (rpm) | Throughput (nm/s) | Go To rep. (pm) | Bidir. rep. (pm) | Backlash hit (pm) | Drift (pm/scan) | Motor T (°C) | Lag (pm) | OK? |
|---|---|---|---|---|---|---|---|---|
| | | | | | | | | |
| | | | | | | | | |
| | | | | | | | | |
| | | | | | | | | |
 
Notes: <span class="fill wide"></span>
 
---
 
## 12. Using FAULHABER Motion Manager alongside this software
 
Motion Manager is FAULHABER's own PC tool for configuring and observing the
drive. It serves two purposes here that this application deliberately does not
do: **inspecting** the drive's stored configuration (especially the limit
switches), and **tuning/observing** the servo. It is a companion to the test
plan, not a replacement for the measurement software.
 
> **Read this before you touch anything.**
>
> **Do not write to the drive's EEPROM (SAVE / EEPSAV) unless you have decided
> to, on purpose, and understand it will change what our software sees on the
> next connect.** This application is built around the drive's *current* stored
> configuration — in particular it sends `HP0` on every connect precisely
> because the stored limit-switch polarity does not match this rig's wiring
> (see section 2 and the `SEND_LEGACY_HP0` note in `device_constants.py`). If
> you save a different polarity, a different operating mode, a different encoder
> setting or a different ramp into EEPROM, our software may stop moving the
> motor correctly, or move it the wrong way, until the constants are updated to
> match. Treat EEPROM as read-mostly.
>
> Two further cautions: the drive's own manual caps `EEPSAV` at roughly 10 000
> writes over its lifetime, so it is not something to do casually; and the
> commands `restore_all` and `fconfig` (sent from the terminal) reset the drive
> to factory/delivery state — recovering from that means re-deriving every
> constant we measured. Know where they are so you can avoid them.
 
### What is safe to do
 
Reading and RAM-only changes are safe — anything not followed by SAVE reverts
on the next power cycle:
 
- **Read the whole configuration.** *Terminal → Receive Parameter File* uploads
  an image of the stored configuration into an editor window you can save and
  print. This is the cleanest way to snapshot the drive before and after any
  change, and to compare (*Terminal → Compare Parameter File*). Do this once at
  the start as a baseline — if anything ever goes wrong, you have the original.
- **Browse live parameters.** The **Object Browser** shows the whole object
  dictionary; read-only entries are marked, the rest can be changed live (RAM).
  This is where the limit-switch and homing parameters live.
- **Send single commands.** The **Terminal** sends any command by hand — e.g.
  query the limit-switch masks and polarity, watch the status word, read the
  temperature — without saving anything.
### For the endstops specifically (section 2 support)
 
The limit-switch behaviour is set by a small group of commands. Useful to know
what they mean while reading the configuration; **inspect, do not save**:
 
| Command | Meaning |
|---|---|
| `HB` | which inputs are **hard-blocking** limit switches (bit mask) |
| `HD` | which **direction** each blocks |
| `HP` | **polarity** — whether a switch is active on the high or low input level. This is the one our `HP0` overrides at runtime |
| `HL` | hard-limit action (stop the motor at the edge) |
| `HN` | hard-notify (set a bit in the status word at the edge) |
| `HA` | home-arming (zero the position at the edge) |
 
A crucial detail confirmed in FAULHABER's manual: **the limit-switch homing
configuration (HA/HL/HN) is not stored by SAVE — after power-on all configured
limit switches are inactive again.** That is exactly why our software arms the
polarity on every connect rather than relying on a stored value, and it is why
"just save the right polarity once" is not as simple as it sounds. If you do
want the endstops armed independently of our software (the proper fix noted in
section 2), verify with FAULHABER's documentation for this specific controller
which parts persist and which do not, and test a full power-cycle afterwards.
 
### For tuning and observation (sections 8–9 support)
 
- **Graphic Analysis** records and plots the drive's own step response — target
  vs. actual position/velocity over time — the direct way to observe a
  following error at speed (section 9b) directly, rather than inferring it from
  a smeared optical peak.
- The **Configuration Wizard** walks through operating mode, motor/encoder and
  the ramp. Use it to *read* the current ramp and to experiment in RAM; commit
  a chosen ramp through our `device_constants.py` (`RAMP_*` + `USE_MOTION_RAMP`)
  rather than by saving it in the drive, so there is one place that owns it.
### Suggested Motion Manager checklist for this campaign
 
1. Connect (close our software first — one program per serial port).
2. *Receive Parameter File* → save as the **baseline snapshot** for this rig.
3. In the Object Browser / Terminal, read and note `HB`, `HD`, `HP`, and the
   ramp (`GAC`/`GDEC`/`GSP`). Record them next to section 2 and section 8.
4. If you run a Graphic Analysis step response, save the CSV and note the
   following error at each test speed.
5. **Leave without saving.** Confirm you have issued no SAVE/EEPSAV, and power
   cycle to be sure the drive is back to its stored state before reconnecting
   our software.
---
 
## Summary sheet
 
Fill this in at the end; it is the one-page benchmark result.
 
| Property | Result | Notes |
|---|---|---|
| Scale error (pm/nm from He D3) | | section 1a |
| Offset after calibration | | section 1a |
| Upper travel limit (nm) | | section 2a |
| Lower travel limit (nm) | | section 2a |
| Usable optical range (nm) | | section 2b |
| Go To repeatability (pm) | | section 3a |
| Bidirectional repeatability (pm) | | section 3b |
| Scan-to-scan drift (pm/scan) | | section 3c |
| Backlash at gearbox 200 (nm) | | section 4 |
| Smallest useful optical step (nm) | | section 6 |
| Free Run usable up to (rpm) | | section 7 |
| Throughput at 200/10000 (nm/s) | | section 8 |
| Ramp AC/DEC/SP per gearbox | | section 8a |
| Motor temp plateau at full duty (°C) | | section 9a |
| Lag onset speed (rpm) | | section 9b |
| Best DAQ read setting (samples/rate) | | section 10a |
| Averaging: software vs. hardware verdict | | section 10b |
| Calibrated `STEPS_PER_NM` per gearbox | | section 5 / matrix 11 |
| Characterisation matrix complete for | ☐100 ☐150 ☐200 ☐250 ☐400 ☐500 ☐1000 ☐___ | section 11 |
 
---
 
## Appendix A — Statistics and uncertainty
 
This appendix gives the formulae to turn a set of repeat readings into a
reported value with an uncertainty, following the GUM (JCGM 100:2008) and NIST
Technical Note 1297. Use it for every repeatability and accuracy figure in the
plan. Symbols: *n* readings *xᵢ*, mean *x̄*.
 
### A.1 Mean and spread
 
Arithmetic mean of *n* repeat readings:
 
<div class="formula">x̄ = (1/n) · Σ xᵢ &nbsp;&nbsp;(i = 1 … n)</div>
Spread (peak-to-peak), a quick robustness check, not a statistic:
 
<div class="formula">spread = x<sub>max</sub> − x<sub>min</sub></div>
### A.2 Sample standard deviation
 
The scatter of the individual readings (divisor *n*−1, the sample standard
deviation):
 
<div class="formula">s = √[ (1 / (n−1)) · Σ (xᵢ − x̄)² ]</div>
### A.3 Type A standard uncertainty (from the repeats)
 
The standard uncertainty of the **mean** — the standard error — is the sample
standard deviation divided by √*n*. This is what a repeatability test yields:
 
<div class="formula">u<sub>A</sub> = s / √n</div>
Note it falls with more readings: averaging ten runs halves the Type A
uncertainty relative to averaging two or three.
 
### A.4 Type B standard uncertainty (from other sources)
 
Contributions not evaluated from the repeats — encoder resolution, the
reference-line uncertainty, thermal drift — are Type B. Convert a half-width
limit *a* to a standard uncertainty by its distribution:
 
- Rectangular (a resolution or a "± max" spec, no central tendency):
  u_B = a/√3
- Triangular (a half-width with central tendency): u_B = a/√6
- An expanded uncertainty *U* quoted at *k* on an upstream certificate:
  u_B = U/k
Example: the arrival tolerance is 90 steps ≈ 0.25 pm. Treated as a rectangular
resolution limit, its standard uncertainty is 0.25/√3 ≈ 0.14 pm.
 
### A.5 Combined standard uncertainty
 
Independent contributions combine in quadrature (root-sum-of-squares — the law
of propagation of uncertainty):
 
<div class="formula">u<sub>c</sub> = √( u<sub>A</sub>² + u<sub>B1</sub>² + u<sub>B2</sub>² + … )</div>
If contributions are in different units, scale each by its sensitivity
coefficient *cᵢ* first: u_c = √( Σ (cᵢ·uᵢ)² ). For this plan the
contributions are all already in pm, so the coefficients are 1.
 
### A.6 Expanded uncertainty (what you report)
 
Multiply the combined standard uncertainty by the coverage factor *k*:
 
<div class="formula">U = k · u<sub>c</sub></div>
Use **k = 2** for approximately 95 % confidence (k = 1 → 68 %, k = 3 → 99.7 %).
Report the result as:
 
<div class="formula">value = x̄ ± U &nbsp;&nbsp;(k = 2)</div>
### A.7 Worked example (Go To repeatability, section 3a)
 
Ten peak measurements of the He D3 line, in nm:
 
```
587.9381  587.9379  587.9384  587.9377  587.9382
587.9380  587.9385  587.9378  587.9383  587.9381
```
 
| Step | Formula | Result |
|---|---|---|
| Mean | x̄ | 587.93810 nm |
| Spread | max − min | 0.80 pm |
| Sample std | s | 0.258 pm |
| Type A | u_A = s/√10 | 0.082 pm |
| Type B (tolerance 0.25 pm, rectangular) | u_B = 0.25/√3 | 0.144 pm |
| Combined | u_c = √(u_A² + u_B²) | 0.166 pm |
| Expanded (k = 2) | U = 2·u_c | 0.332 pm |
 
**Reported result:** peak = 587.93810 nm ± 0.33 pm (k = 2, ≈ 95 %).
 
Read it as: the ten runs agree to well under a picometre, and the dominant
uncertainty is not the scatter of the runs (0.082 pm) but the positioning
tolerance (0.144 pm), which indicates where to look if
the number needs to improve.
 
### A.8 Drift (section 3c)
 
For scan-to-scan drift, fit a straight line to peak position vs. scan number
and report the **slope** (pm per scan) with its own standard error. A slope
whose magnitude is smaller than its standard error is consistent with **no
drift**; report it as such rather than quoting a non-zero number that is within
the noise. The absolute-grid fix is expected to give exactly this — a slope
indistinguishable from zero.
 
