"""
daq.py — Measurement acquisition (NI-DAQ + simulator) and averaging.

WHAT THIS MODULE DOES
---------------------
Three layers of signal acquisition, bottom to top:

  1. read_pmt_voltage()      — ONE read: takes DAQ_SAMPLES_PER_READ hardware
                               samples and averages them. Without an NI-DAQ
                               driver it returns a simulator value (noise
                               around 0.2 V), so the app stays runnable
                               without hardware.
  2. read_pmt_voltage_avg()  — average multiple reads, either over a fixed
                               count (samples) or over a time window
                               (window_sec). Respects pause/stop.
  3. acquire_measurement()   — ONE scan point: pre-settle -> average -> rest
                               dwell. THE single source of truth for point
                               acquisition; scan_engine.scan_worker and
                               do_resume both call this function (prevents
                               the AVG bug from ever coming back).

GUI values (averaging mode, sample count, fraction, pre-settle, DAQ channel)
are read via the ui registry from app_context (ui_get), NOT via direct widget
access -- so this module imports no tkinter.

Origin: extracted from the monolith (function bodies unchanged; only widget
access switched to ui_get).

IMPORTANT (doc policy, see HANDOVER.md): docstrings/comments are preserved.
"""

import time
import random
import re

from app_context import state, log, ui_get
from device_constants import DAQ_SAMPLES_PER_READ, DAQ_SAMPLE_RATE_HZ

# Optional NI-DAQ: automatically disable if the driver is missing ->
# read_pmt_voltage() then falls back to the simulator.
try:
    import nidaqmx
    from nidaqmx.constants import AcquisitionType, TerminalConfiguration
    DAQ_AVAILABLE = True
except Exception:
    nidaqmx = None
    AcquisitionType = None
    TerminalConfiguration = None
    DAQ_AVAILABLE = False

# Rate-limit for DAQ read-error logging in read_pmt_voltage(). A hardware/driver
# fault there previously returned 0.0 SILENTLY -> a scan would fill with flat
# zeros and nothing in the log said why. We now log the error, but read_pmt_
# voltage() runs in a tight averaging loop (N reads per point), so a persistent
# fault would flood the console; this timestamp throttles it to ~once/2 s.
_last_daq_read_err_log = 0.0
_DAQ_READ_ERR_LOG_INTERVAL_S = 2.0


def _get_presettle_sec():
    """Pre-settle time (s) from the GUI field 'Pre-settle (ms)'. If the GUI is
    missing or the value is invalid -> 0.0."""
    try:
        return max(0.0, float(ui_get('presettle_ms_var', '0')) / 1000.0)
    except Exception:
        return 0.0


def _get_avg_fraction():
    """Averaging fraction (%) from the GUI field 'Avg fraction (%)', clamped
    to 0..100. If the GUI is missing/invalid -> 100.0."""
    try:
        return max(0.0, min(100.0, float(ui_get('avg_fraction_var', '100'))))
    except Exception:
        return 100.0


def _daq_channel_path():
    """
    Build NI-DAQ channel string from current UI state, e.g. "Dev1/ai1".
    Falls back safely if fields are missing.
    """
    try:
        dev = (state.get('daq_dev') or 'Dev1').strip()
    except Exception:
        dev = 'Dev1'
    try:
        ai = (state.get('daq_ai') or 'ai1').strip()
    except Exception:
        ai = 'ai1'
    # sanitize
    try:
        if not re.match(r'^ai\d+$', ai):
            ai = 'ai1'
        if not dev:
            dev = 'Dev1'
    except Exception:
        pass
    return f"{dev}/{ai}"

def read_pmt_voltage(chan=None, samples=DAQ_SAMPLES_PER_READ, rate=DAQ_SAMPLE_RATE_HZ):
    """Return one averaged voltage using NI-DAQ if available; else simulator.

    'samples' hardware samples are taken at 'rate' Hz and averaged into a
    single value. This is the low-level per-read averaging; the higher-level
    scan averaging (multiple reads / time window) lives in
    read_pmt_voltage_avg() and acquire_measurement()."""
    if chan is None:
        try:
            chan = _daq_channel_path()
        except Exception:
            chan = "Dev1/ai1"

    if not DAQ_AVAILABLE or nidaqmx is None:
        return _simulated_pmt_voltage()

    try:
        with nidaqmx.Task() as task:
            task.ai_channels.add_ai_voltage_chan(
                chan, min_val=0.0, max_val=10.0,
                terminal_config=TerminalConfiguration.RSE
            )
            if int(samples) > 1:
                task.timing.cfg_samp_clk_timing(int(rate), sample_mode=AcquisitionType.FINITE, samps_per_chan=int(samples))
                vals = task.read(number_of_samples_per_channel=int(samples))
                if isinstance(vals, list) and vals:
                    return float(sum(vals)/len(vals))
                return float(vals) if isinstance(vals, (int,float)) else 0.0
            else:
                return float(task.read())
    except Exception as e:
        # Previously swallowed silently (return 0.0). Keep the 0.0 return so
        # scan/averaging math never crashes on a transient glitch, but surface
        # the cause -- otherwise a wiring/driver fault looks like a genuine 0 V
        # signal. Throttled so a persistent fault can't flood the log.
        global _last_daq_read_err_log
        now = time.time()
        if now - _last_daq_read_err_log >= _DAQ_READ_ERR_LOG_INTERVAL_S:
            _last_daq_read_err_log = now
            log(f"[DAQ] read failed on {chan}: {e} -- returning 0.0 V", "error")
        return 0.0

def _simulated_pmt_voltage() -> float:
    """Wavelength-dependent fake reading for operation WITHOUT an NI-DAQ.

    WHERE/WHEN: read_pmt_voltage() calls this when no NI driver is present (e.g.
    on a Mac, or in SIM mode with the virtual motor). WHY shaped and not flat:
    state['current_nm'] is set to exactly the wavelength being measured at each
    scan step, so an nm-dependent shape produces a RECOGNIZABLE curve while
    scanning (baseline + a few emission lines: H-beta 486, Na-D 589, H-alpha
    656 nm) instead of pure noise -- this lets the whole path (plot, averaging,
    CSV) be checked meaningfully. Without a known nm it falls back to the old
    noise value. Affects ONLY the simulator; the real NI-DAQ read routine is
    unchanged."""
    try:
        nm = float(state.get('current_nm'))
    except Exception:
        return 0.2 + 0.05 * random.random()

    def _gauss(center, width, amp):
        return amp * 2.718281828 ** (-((nm - center) / width) ** 2)

    signal = 0.15                      # baseline
    signal += _gauss(486.1, 6.0, 0.45)  # H-beta
    signal += _gauss(589.3, 8.0, 0.70)  # Na-D
    signal += _gauss(656.3, 5.0, 1.00)  # H-alpha (reference line for backlash_cal)
    signal += 0.02 * random.random()    # measurement noise
    return signal


def sleep_with_pause(seconds: float):
    """Sleep up to 'seconds' while honoring pause/stop."""
    end_t = time.time() + max(0.0, float(seconds))
    while time.time() < end_t:
        if state.get("stop_flag"):
            break
        if state.get("is_paused"):
            time.sleep(0.05)
            continue
        time.sleep(min(0.05, max(0.0, end_t - time.time())))

def read_pmt_voltage_avg(samples: int = None, window_sec: float = None):
    """Average DAQ reading over N samples; N from UI if not provided.
    This is a tight loop of read_pmt_voltage(); it respects stop/pause.
    Returns the arithmetic mean (or a single read if interrupted)."""
    # If window_sec is provided, average over that duration (seconds)
    if window_sec is not None and window_sec > 0:
        t0 = time.time()
        total = 0.0
        count = 0
        while (time.time() - t0) < float(window_sec) and not state.get("stop_flag"):
            while state.get("is_paused") and not state.get("stop_flag"):
                time.sleep(0.05)
            v = read_pmt_voltage()
            total += float(v)
            count += 1
        if count <= 0:
            return float(read_pmt_voltage())
        return total / count

    # Otherwise: average a fixed number of samples
    try:
        if samples is None:
            samples = int(ui_get('avg_samples_var', '8'))
    except Exception:
        samples = 1
    samples = max(1, int(samples))

    total = 0.0
    count = 0
    for _ in range(samples):
        if state.get("stop_flag"):
            break
        while state.get("is_paused") and not state.get("stop_flag"):
            time.sleep(0.05)
        v = read_pmt_voltage()
        total += float(v)
        count += 1
    if count <= 0:
        return float(read_pmt_voltage())
    return total / count

def acquire_measurement(dwell_s: float) -> float:
    """
    Acquire ONE averaged data point using the current GUI averaging settings.

    This is the SINGLE SOURCE OF TRUTH for point acquisition. scan_worker and
    do_resume both call it, so their averaging behaviour can never drift
    apart again (previously scan_worker measured with a single raw
    read_pmt_voltage() and ignored the AVG controls entirely, while only
    do_resume averaged -- that's why AVG looked broken on a normal scan).

    Per-point timing budget (all consumed BEFORE the move to the next point):

        |<-- pre-settle -->|<-- averaging window / N samples -->|<-- rest -->|
        |<---------------------------- dwell_s --------------------------->|

    Modes (avg_mode_var):
        'samples' : average a FIXED number of DAQ reads (avg_samples_var),
                    independent of the clock -> constant noise floor per point.
        'time'    : average continuously for AVG_FRACTION_PCT % of the dwell
                    left after pre-settle -> averaging time scales with dwell.

    Returns the averaged PMT voltage (float).
    """
    dwell_s = max(0.0, float(dwell_s))

    presettle = min(_get_presettle_sec(), dwell_s)
    if presettle > 0:
        sleep_with_pause(presettle)

    t_meas = time.time()
    if ui_get('avg_mode_var', 'samples') == 'time':
        frac = _get_avg_fraction() / 100.0
        window = max(0.0, min(dwell_s - presettle, dwell_s * frac))
        v = read_pmt_voltage_avg(window_sec=window)
    else:
        v = read_pmt_voltage_avg()
    meas_dur = time.time() - t_meas

    sleep_with_pause(max(0.0, dwell_s - presettle - meas_dur))
    return float(v)
