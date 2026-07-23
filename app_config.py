"""
app_config.py — Persistent settings (JSON singleton), PS-2600A style.

WHAT THIS MODULE DOES
---------------------
Holds the settings that should survive a program restart in a single JSON
file next to the program. Replaces the earlier _load_settings/_save_settings
provisional code from the monolith and generalizes it to the pattern from
the PS-2600A project:

  - DEFAULT_CONFIG defines ALL persistent keys with their defaults.
  - On load, missing keys are filled in from DEFAULT_CONFIG (so a new key
    added in a newer program version doesn't crash on an old JSON file).
  - A missing OR corrupt JSON falls back to defaults, not a crash.
  - New persistent settings are added here in DEFAULT_CONFIG, nowhere else.

WHAT DOES NOT BELONG HERE
-------------------------
Fixed hardware/timing constants (STEPS_PER_NM, timeouts, ...) live in
device_constants.py — the user doesn't change those at runtime and they are
not persisted. app_config is ONLY for persistent values the user can change
at runtime (currently: the measured backlash/slip value).

DELIBERATELY NOT PERSISTENT: state['last_move_direction'] (the current
backlash seating direction). It reflects the ACTUAL mechanical state, which a
closed program cannot know — so it stays session-only and the Reference Run
remains a per-session action.

IMPORTANT (doc policy, see HANDOVER.md): docstrings are preserved.
"""

import os
import json

from app_context import log


# Storage location: next to this module (= next to the program). A fixed,
# predictable path, no hidden user folder.
SETTINGS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "vrs41_settings.json")


# All persistent keys + defaults in ONE place. New persistent setting?
# -> add a key here, nowhere else.
DEFAULT_CONFIG = {
    # Mechanically measured backlash/slip in nm. Used on direction reversal
    # for compensation (see motion.reversal_compensation_steps).
    # Default matches the value currently measured on the rig.
    "backlash_slip_nm": 0.30,

    # Last known software-tracked wavelength (nm). Restored into the
    # "Current λ" field at startup so a restart doesn't lose track of where
    # the monochromator was left. CAVEAT: this is NOT a hardware readback --
    # the device has no absolute position sensor, so if the motor was moved
    # by hand (or by another program) while this app was closed, the
    # restored value is wrong. Always confirm with a Reference Run / a known
    # line before trusting it for precision work.
    "last_current_nm": None,

    # Connection defaults (GUI Port/Baud fields).
    "port": None,
    "baud": None,

    # Scan fields.
    "scan_start_nm": None,
    "scan_end_nm": None,
    "scan_step_nm": None,
    "scan_wait_ms": "0",   # dwell, entered/displayed in ms in the GUI

    # Free Run (continuous scan) max speed override, in rpm (SP command).
    # None = use device_constants.RAMP_SP (the rig-confirmed normal speed).
    # HARD-CAPPED at RAMP_SP in the GUI/validation, never higher -- that
    # constant is the value actually verified safe/sane on this rig
    # (tune_ramp.py), not the drive's raw 30000 manual maximum. This field
    # can only go SLOWER than normal, never faster.
    "free_run_sp_rpm": None,

    # Move / jog.
    "jog_step_nm": None,

    # Averaging controls.
    "avg_mode": None,
    "avg_samples": None,
    "avg_fraction_pct": None,
    "presettle_ms": None,

    # Export controls.
    "export_dir": None,
    "export_pattern": None,
    "autosave_csv": False,

    # DAQ channel selection.
    "daq_dev": None,
    "daq_ai": None,

    # Backlash calibration dialog (gui_main.CalibrationDialog). None = use
    # the CAL_*_DEFAULT constants in device_constants.py until the user
    # changes a field here.
    "cal_center_nm": None,
    "cal_span_nm": None,
    "cal_step_nm": None,
    "cal_dwell_s": None,
}


def _read_raw() -> dict:
    """Read the raw JSON. Missing file OR broken content -> {} (callers then
    fill in from DEFAULT_CONFIG). Never raises."""
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def load() -> dict:
    """Return the full configuration: DEFAULT_CONFIG, overridden by whatever
    is in the JSON. This means missing keys always have a sensible default.
    Called e.g. during GUI setup to pre-fill the slip field."""
    cfg = dict(DEFAULT_CONFIG)     # copy of the defaults
    cfg.update(_read_raw())        # persisted values on top
    return cfg


def get(key, default=None):
    """Read a single config value. Order: JSON -> DEFAULT_CONFIG -> the
    default passed in."""
    cfg = load()
    if key in cfg:
        return cfg[key]
    return DEFAULT_CONFIG.get(key, default)


def save(**updates) -> None:
    """Merge the given key/value pairs into the JSON and write it back.
    Never raises — a failed save is logged, but must not interrupt a running
    measurement. Example:
        app_config.save(backlash_slip_nm=0.31)
    """
    try:
        data = _read_raw()
        data.update(updates)
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        log(f"[SETTINGS] Save failed: {e}", "warn")
