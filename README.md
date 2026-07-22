# VRS41 Monochromator Controller

Qt6 (PyQt6 + pyqtgraph) GUI for a FAULHABER-driven monochromator.

## Install
    python3 -m venv .venv && source .venv/bin/activate
    pip install -r requirements.txt

## Run
    python3 main.py

## Test without hardware (SIM mode)
Port = `SIM`, click Connect. No motor / NI-DAQ required.

## Backlash / slip calibration
"Calibrate…" button in BACKLASH section (PS-2600A-style dialog).

## Settings persistence
Almost every entered field is saved to vrs41_settings.json and restored on
next start (see app_config.DEFAULT_CONFIG for the full list). Restored
wavelength is a software estimate, not a hardware readback.

## Motion ramp / max speed (AC/DEC/SP)
Confirmed + tuned live at the rig via tune_ramp.py (standalone diagnostic,
independent of the app — reads GAC/GDEC/GSP, optionally tests new values
with a small move, never touches SAVE/EEPSAV).

Currently active (device_constants.py, USE_MOTION_RAMP=True):
  RAMP_AC = 50, RAMP_DEC = 100, RAMP_SP = 10000
Applied by protocol_faulhaber.init_motor() on every connect (RAM only).
Previous DEC was 30000 (drive default/factory) — effectively no brake ramp,
causing an abrupt stop; DEC=100 fixed that.

To re-tune: `python3 tune_ramp.py COM3` (read-only) or
`python3 tune_ramp.py COM3 --set "ac=.. dec=.. sp=.." --move 2000` (RAM-only
test with a small move).

## Files
- `main.py`, `gui_main.py`, `gui_main_tk.py` (Tk reference, unused)
- `scan_engine.py`, `motion.py`, `protocol_faulhaber.py`, `daq.py`
- `app_context.py`, `app_config.py`, `device_constants.py`
- `sim_hardware.py`, `backlash_cal.py`, `tune_ramp.py`
- `HANDOVER.md` / `BACKLOG.md`

Note: the retired monolith `monochromator_python_code_vrs41.py` is NOT part
of this package (kept only in the project as historical reference).
