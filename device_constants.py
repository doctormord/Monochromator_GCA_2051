"""
device_constants.py — VRS41 monochromator: fixed calibration/timing constants.

PURPOSE OF THIS FILE
---------------------
All values here are either (a) hardware calibration values (e.g. steps/nm),
(b) timing/tolerance constants for the motion logic, or (c) connection
defaults. They are NOT changeable via the GUI at runtime and are NOT
persisted in the settings JSON — that's what app_config.py is for
(it holds values the user changes during operation, e.g. the slip value).

Comes from the monolith `monochromator_python_code_vrs41.py`, where these
values were scattered across several hundred lines (see origin comments
below). This file is Stage B, step 1 of the module split (see
BACKLOG.md) — pure constants, no behaviour, hence low risk.

IMPORTANT (see doc policy in HANDOVER.md): any future change to this file
must preserve the origin/usage comments. New constants get the same comment
depth (what/where used/why this value).
"""

# =====================================================================
# CALIBRATION (hardware-specific, MUST be verified on a different rig)
# =====================================================================

# Steps per nanometer of the motor gearing. Determines the nm <-> motor-steps
# conversion in motion.steps_for_delta_nm() / motion.nm_to_steps().
# Origin: monolith line ~335 ("IMPORTANT: steps-per-nm calibration.
# Adjust if your rig differs.").
STEPS_PER_NM = 361765

# Move-direction sign: when True, a positive nm change sends NEGATIVE LR
# steps to the controller (rig-specific wiring/gear orientation). Used in
# motion.steps_for_delta_nm().
# Origin: monolith line ~520 ("LAB TRY: forward λ uses negative LR on
# this rig"). Trivially reversible (a simple sign flip) if the rig is ever
# wired differently — nothing to worry about, just verify.
INVERT_DIRECTION = True

# Additionally send HP0/V0 after every move (make the drive "safe").
# Used in practically every move function (goto/scan/resume) after a
# successful wait_until_position().
SAFE_AFTER_MOVE = True


# =====================================================================
# SERIAL CONNECTION (defaults for the GUI Port/Baud fields)
# =====================================================================
DEFAULT_PORT = "COM3"   # changeable in the GUI, this is only the suggestion
DEFAULT_BAUD = 9600     # baud rate of the Faulhaber controller

# --- Serial I/O hardening (actively used in send_cmd()/protocol layer) ---
# On timeout: drain the buffer and resync with a status command (POS)
# instead of simply propagating the error.
RESYNC_ON_TIMEOUT = True
# Short pause after flushing a write, so the controller can "catch its
# breath" before the next query arrives.
WRITE_SETTLE_S = 0.01  # s

# --- Currently UNUSED (found during extraction, referenced only at their
#     own definition site). Not deleted, only flagged. ---
ECHO_FILTER = False     # unused -- intended for controllers that echo commands
CMD_TIMEOUT = 1.0       # s -- unused, not to be confused with POLL_TIMEOUT (which IS active)
MOVE_TIMEOUT = 60.0     # s -- unused, not to be confused with MIN_/MAX_MOVE_TIMEOUT (which ARE active)


# =====================================================================
# TIMING / POLLING
# =====================================================================

# Polling interval for motion wait loops (how often POS/statusword is
# re-read while waiting for a target to be reached).
POLL_INTERVAL = 0.05  # s

# Short read timeout for serial poll traffic (POS queries etc.), so that
# stop/pause stay responsive even if a single query happens to hang.
POLL_TIMEOUT = 0.10  # s

# Re-check cadence for pause/stop loops that wait on user action
# (e.g. during a jog or reference-run wait). New in this pass,
# conceptually replaces scattered hardcoded time.sleep(0.05) calls
# (the hardcoded spots in the monolith have not all been switched to
# this constant yet -- see BACKLOG P3, "make GUI-agnostic").
PAUSE_POLL_S = 0.05  # s


# =====================================================================
# TIMEOUT CALCULATION FOR MOVES (motion.timeout_for_nm / timeout_for_target_steps)
# =====================================================================

# Estimated seconds per nm of motion, the basis for the dynamic timeout
# calculation in motion.timeout_for_nm(). Not a measured value but a
# safety estimate -- if timeouts feel too tight/too generous in practice,
# adjust here (no need to recalibrate STEPS_PER_NM).
SEC_PER_NM = 10.0

# Fixed seconds added to every timeout calculation (communication/
# processing overhead, independent of distance).
TIMEOUT_OVERHEAD_S = 5.0

# Lower and upper cap for the computed move-timeout duration, so that
# neither mini-moves get too short a timeout (spurious timeout) nor a
# very long move blocks forever if something hangs.
MIN_MOVE_TIMEOUT = 10.0   # s
MAX_MOVE_TIMEOUT = 900.0  # s

# --- Currently UNUSED (found during extraction, referenced nowhere
#     except their own definition -- see doc policy: don't delete,
#     just flag clearly). timeout_for_nm() actually computes with
#     SEC_PER_NM/TIMEOUT_OVERHEAD_S above, NOT with these three. ---
TIMEOUT_BASE = 3.0      # s (base) -- unused
TIMEOUT_PER_NM = 1.0    # s per nm -- unused
TIMEOUT_MARGIN = 2.5    # safety factor -- unused
BASE_TIMEOUT_MIN = 8.0  # s -- unused (not to be confused with MIN_MOVE_TIMEOUT, which IS active)

# Whether [TIMEOUT] messages are suppressed in the log (pure UI cosmetics).
SUPPRESS_TIMEOUT_MESSAGES = True


# =====================================================================
# MOTION RAMP / MAX SPEED (AC/DEC/SP)
# =====================================================================
# FAULHABER RS232 manual 7000.00029 (Kap. 3.8.1 "Rampengenerator" + Kap.
# 7.1.3): confirmed for this rig's motor family -- "32xx...BX4CS" in that
# manual covers the fitted 3242G024BX4. The ramp generator applies in ALL
# operating modes except VOLTMOD/current-limit control, INCLUDING the
# position mode (LA/LR/M) this codebase uses -- not just velocity mode (V).

# Master switch: apply AC/DEC/SP once per connect (protocol_faulhaber.
# init_motor(), via configure_ramp()). Values below were read+tested live at
# the rig via tune_ramp.py (DEC was previously 30000 -- effectively no brake
# ramp at all, causing the abrupt stop the user reported; AC/SP were already
# reasonable). Confirmed OK by the user on 2026-07-22.
USE_MOTION_RAMP = True

# Acceleration ramp [1/s^2], manual range 0...30000. None = don't send AC.
RAMP_AC = 50
# Deceleration ramp [1/s^2], manual range 0...30000. None = don't send DEC.
# Was 30000 (= max, ~instant stop) before this fix.
RAMP_DEC = 100
# Maximum speed [rpm], manual range 0...30000. None = don't send SP.
# Rarely reached on short scan-step moves (ramp-limited long before SP), but
# caps longer moves (Reference Run, large jumps).
RAMP_SP = 10000


# =====================================================================
# POSITION TOLERANCE ("when does a target count as reached?")
# =====================================================================

# Base value in motor steps, immediately replaced below by a floor
# (see POS_TOL_STEPS). Left here as a constant because the floor
# assumes a starting value exists (origin artifact from the
# monolith, functionally irrelevant since it's overwritten right away).
_POS_TOL_STEPS_BASE = 5

# Effective position tolerance: at least 1% of STEPS_PER_NM, so that on
# rigs with a very fine gearing ratio a too-tight tolerance doesn't cause
# spurious timeouts. Used in motion.wait_until_position() as the default
# for the 'tol' parameter.
POS_TOL_STEPS = max(int(0.01 * STEPS_PER_NM), int(_POS_TOL_STEPS_BASE))

# How many consecutive polls must lie within tolerance before a position
# counts as "stably reached" (debounce against single outlier reads).
POS_STABLE_COUNT = 3

# --- Currently UNUSED (found during extraction). Was intended in the
#     monolith as a "resume-specific, looser tolerance", but is read
#     nowhere -- wait_until_position() on resume uses the same
#     POS_TOL_STEPS as a fresh scan. Not deleted, only flagged. ---
POS_TOL_STEPS_RESUME = max(POS_TOL_STEPS, int(float(STEPS_PER_NM) * 0.001))  # unused


# =====================================================================
# BACKLASH / SLIP COMPENSATION
# =====================================================================
# Mental model (per session, carried over unchanged from the monolith --
# the logic itself will move to motion.py later, see BACKLOG Stage B
# step 4; until then THIS is the complete, authoritative explanation,
# not just a pointer):
#
#   1) Once after connecting, press the "Reference Run" button. This
#      travels a short, fixed distance in ONE fixed direction
#      (REFERENCE_DIRECTION). This "takes up" the mechanical play in
#      that direction and tells the software with certainty which
#      direction the mechanism is currently seated in ->
#      state['last_move_direction'] = REFERENCE_DIRECTION.
#   2) From then on EVERY goto/scan/resume move checks whether the
#      requested direction differs from state['last_move_direction'].
#      If so (a direction reversal), an extra SLIP_NM nm is added into
#      the ongoing move (in the direction of travel) to take up the
#      play, before the actual precision move continues. On the same
#      direction, nothing is added.
#   3) The slip value itself is NOT measured automatically -- it lives
#      in the GUI field "Slip / Backlash (nm)" (measured mechanically;
#      currently ~0.3 nm after the mechanical fix). The VALUE survives
#      a restart (app_config.py / settings JSON); the direction state
#      from step 1 does NOT -- which is why the Reference Run remains
#      a per-session action.
#
# The numeric values for this:

# Distance of the manual "Reference Run" (nm). Must be clearly larger than
# the real backlash (currently ~0.3 nm after the mechanical fix), but short
# enough to be quick -- this is explicitly NOT limit-switch homing
# (that takes ~10 min on this rig and is impractical for routine use).
REFERENCE_MOVE_NM = 3.0

# Sign convention of the Reference Run direction. Was -1 ("to the left")
# before rig verification; confirmed on this rig to need +1 -- flipped
# accordingly. Trivially reversible: just flip the sign again if a
# different rig/wiring needs the opposite direction.
REFERENCE_DIRECTION = 1


# =====================================================================
# BACKLASH CALIBRATION DIALOG DEFAULTS (gui_main.CalibrationDialog)
# =====================================================================
# Startup defaults for the "Calibrate…" dialog's input fields, same pattern
# as the AVG_*_DEFAULT constants below: these are the factory defaults;
# once the user edits a field, app_config.DEFAULT_CONFIG (cal_center_nm/
# cal_span_nm/cal_step_nm/cal_dwell_s) takes over and persists the choice
# across restarts. Change these here if you want a different reference line
# by default (e.g. Na-D 589.3, H-beta 486.1) -- see HANDOVER.md for how the
# calibration sweep uses them.
CAL_CENTER_NM_DEFAULT = "656.300"  # H-alpha
CAL_SPAN_NM_DEFAULT = "2.0"        # nm, sweep is center +/- span
CAL_STEP_NM_DEFAULT = "0.2"        # nm
CAL_DWELL_S_DEFAULT = "0.2"        # seconds (calibration dialog dwell is in
                                    # seconds, unlike the main SCAN dwell
                                    # field which is in ms -- not unified,
                                    # flag if that should change)


# =====================================================================
# MEASUREMENT / AVERAGING (see daq.acquire_measurement for the logic)
# =====================================================================

# GUI default values for the averaging controls at program start.
AVG_MODE_DEFAULT = "samples"  # 'samples' = average a fixed number of reads;
                               # 'time' = average for a fraction of the dwell time
AVG_SAMPLES_DEFAULT = "8"        # number of reads in 'samples' mode
AVG_FRACTION_PCT_DEFAULT = "100"  # % of the remaining time in 'time' mode
PRESETTLE_MS_DEFAULT = "0"       # settle time after a move, before measuring (ms)

# For a SINGLE read_pmt_voltage() call: how many hardware samples the
# NI-DAQ takes and at what rate. This is the lowest averaging level
# (within a single "read"), not to be confused with the higher-level
# averaging over multiple reads/time windows in acquire_measurement().
DAQ_SAMPLES_PER_READ = 10    # hardware samples per read
DAQ_SAMPLE_RATE_HZ = 1000    # sample rate (Hz)


# =====================================================================
# SCAN-LOOP TOLERANCES
# =====================================================================

# Float tolerance for the "has the end of the scan range been reached?"
# comparison in scan_engine.scan_worker(). NOTE: do_resume() in the current
# monolith still uses a hardcoded 1e-12 at the corresponding spot instead
# of this constant -- deliberately not yet unified (see BACKLOG P3), to be
# handled together with the scan_engine.py extraction.
SCAN_EPSILON_NM = 1e-9


# =====================================================================
# STATUSWORD (CiA-402, SDO 0x6041) + OST (Faulhaber ASCII) CONSTANTS
# =====================================================================
# These used to be scattered FOUR TIMES across the monolith (STATUS_FAULT_BIT/
# STATUS_TR_BIT with identical values in four places, together with three
# partially overlapping audit functions). Now consolidated here ONCE,
# centrally.
#
# IMPORTANT: only the CONSTANTS were consolidated (identical values, safe).
# The three audit FUNCTIONS (postmove_status_audit, postmove_limit_audit,
# post_move_statusword_audit) remain separate in motion.py -- they have
# different enable flags and partially overlapping purposes; merging them
# could silently disable a safety check and needs sign-off (see BACKLOG P3).

# --- CiA-402 statusword bits (SDO 0x6041) ---
STATUS_FAULT_BIT          = 1 << 3   # Fault
STATUS_WARNING_BIT        = 1 << 7   # Warning (informational only)
STATUS_TR_BIT             = 1 << 10  # Target Reached
STATUS_INTERNAL_LIMIT_BIT = 1 << 11  # "internal limit active" (if the drive supports it)
STATUS_INT_LIMIT_BIT      = 1 << 11  # alias (used under both names in the monolith)
STATUS_ACK_BIT            = 1 << 12  # Set-Point Acknowledge

# --- Statusword integration: switches/timing ---
USE_SDO_FOR_AUDIT        = False  # SDO/0x6041 audits off; use OST only
USE_STATUSWORD           = False  # leave False until verified in the lab
STATUSWORD_NODE          = 1      # typical node ID; adjust if needed
STATUS_ACK_ARM_TIMEOUT_S = 0.5    # time to see bit-12 ACK / bit-10 drop
STATUS_TR_DEBOUNCE_POLLS = 2      # consecutive polls TR must stay high for
STATUS_POLL_S            = 0.05   # 50 ms

# --- Audit enable flags (control the three audit functions in motion.py) ---
USE_POSTMOVE_STATUSWORD_AUDIT = False  # postmove_status_audit()
USE_POSTMOVE_LIMIT_AUDIT      = True   # postmove_limit_audit() (OST-only)
POST_MOVE_STATUS_AUDIT        = True   # post_move_statusword_audit()

# --- Soft limits (optional; None = disabled) ---
SOFT_LIMIT_MIN_NM    = None
SOFT_LIMIT_MAX_NM    = None
SOFT_LIMIT_MARGIN_NM = 0.0

# --- FAULHABER OST bits (RS232 manual, table 8) ---
OST_HOMING_RUNNING       = 0x0001   # bit0
OST_PROG_RUNNING         = 0x0002   # bit1
OST_STOPPED_DELAY        = 0x0004   # bit2
OST_STOPPED_NOTIFY       = 0x0008   # bit3 (program-stop notify; NOT a fault)
OST_CURRENT_LIMIT_ACTIVE = 0x0010   # bit4
OST_DEVIATION_ERROR      = 0x0020   # bit5
OST_OVERVOLTAGE          = 0x0040   # bit6
OST_OVERTEMPERATURE      = 0x0080   # bit7
OST_STATUS_INPUT1        = 0x0100   # bit8  (digital input 1)
OST_STATUS_INPUT2        = 0x0200   # bit9  (digital input 2)
OST_STATUS_INPUT3        = 0x0400   # bit10 (digital input 3)
OST_POSITION_ATTAINED    = 0x10000  # bit16
OST_LIMIT_CONT_CURRENT   = 0x20000  # bit17

# OST masks (used by read_ost/wait/audit)
OST_ERROR_MASK = (OST_DEVIATION_ERROR | OST_OVERVOLTAGE | OST_OVERTEMPERATURE)
OST_FAULT_MASK = OST_ERROR_MASK  # alias (previously separate, now resolved)
OST_INPUT_MASK = (OST_STATUS_INPUT1 | OST_STATUS_INPUT2 | OST_STATUS_INPUT3)

# --- Legacy OST masks, CURRENTLY DISABLED (value 0x0000). Used by
#     postmove_limit_audit() in an OR; with 0x0000 they contribute nothing.
#     Not deleted (doc policy), only clearly flagged -- if the rig ever
#     provides real hard/internal-limit bits, enter the matching values
#     here. ---
OST_HARD_LIMIT_MASK     = 0x0000  # disabled (e.g. 256 for hard/limit reached)
OST_INTERNAL_LIMIT_MASK = 0x0000  # disabled (e.g. 1024 for internal software limit)

# --- OST log throttling (protocol_faulhaber.read_ost) ---
LOG_OST_VERBOSE    = True   # False = mute periodic OST logs
OST_LOG_THROTTLE_S = 0.5    # minimum interval between OST logs (s)
