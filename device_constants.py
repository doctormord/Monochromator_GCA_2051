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

# --- "HP0" is NOT a motion command (rig + manual finding) ----------------
# The app has sent "HP0" since the monolith days alongside EN/V0, evidently in
# the belief that it was a "halt/hold position" style safety command. It is
# not. In FAULHABER manual DE_7000_00029 (sections 3.5.3.1 / 7.1.5) HP has
# exactly ONE meaning, verified by searching every occurrence in the manual:
#
#     HP <Bitmaske>  = Hard Polarity -- which edge/level of each LIMIT SWITCH
#                      input counts as valid.
#                      bit=1: rising edge / HIGH level valid
#                      bit=0: falling edge / LOW level valid
#
# So "HP0" writes the limit-switch polarity mask to 0 for ALL five inputs --
# a configuration write to the safety hardware, executed on every connect,
# every stop and every recover. It is RAM-only here (the app never SAVEs), but
# it silently overrides whatever polarity the drive was commissioned with
# until the next power-cycle.
#
# Consequence if the drive's stored polarity is NOT 0: the hard-blocking
# limit switches are armed on the WRONG level for the rest of the session,
# i.e. the drive thinks a switch is engaged while it is free and vice versa.
# That is a plausible (NOT yet proven) contributor to a move running past an
# end -- see BACKLOG.
#
# DEFAULT False: stop writing limit-switch configuration the app never meant
# to write. Nothing motion-related is lost -- HP is not a motion command.
# Set True to restore the historical behaviour verbatim.
#
# HOW TO FIND OUT WHAT THE DRIVE ACTUALLY WANTS (one clean experiment):
# power-cycle the controller and run read_faulhaber_config.py BEFORE starting
# the app. The IOC/HP line then shows the EEPROM value. If it is 00000 the
# app's HP0 was a harmless no-op all along; if it is anything else, the app
# has been flipping the endstop polarity every session.
#
# ===== 2026-07-24: BACK TO True -- THE EXPERIMENT ABOVE HAS BEEN RUN =====
# Setting this to False stopped the app writing HP0, and the drive then used
# its own STORED polarity for the first time. The result at the rig: the motor
# would only travel in ONE direction. Every move the other way ended in
# "[STOP] Motion interrupted" / "[ERROR] Goto failed after auto-recover",
# and calibration became impossible.
#
# That is exactly what the stored configuration predicts:
#   IOC:  HB = 00101  -> inputs 1 and 3 ARE hard-blocking limit switches
#         HD = 00101  -> both of them block CLOCKWISE travel
#   OST:  0x0500      -> both inputs sit at HIGH level in normal operation
# With a stored HP bit of 1 ("rising edge / HIGH level valid") the drive reads
# those permanently-HIGH inputs as "both endstops pressed" and blocks clockwise
# travel forever. Writing HP0 flips the interpretation to "LOW level valid",
# the inputs read as free, and both directions work.
#
# So the drive's stored polarity does not match this rig's wiring, and the
# app's HP0 was silently compensating for it all along. Sending it is
# therefore CORRECT here, not the accident it looked like -- the misleading
# part was only ever the name (HP is limit-switch polarity, not "hold
# position"), which is now documented above.
#
# PROPER FIX, when the rig is next accessible: set the polarity on the drive
# itself and SAVE it, then this flag can go back to False and the limit
# switches will be armed correctly even before the app connects -- which is
# the state you actually want for a safety function. Until then, leave True.
SEND_LEGACY_HP0 = True


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

# --- Answer mode (ANSW) sent by protocol_faulhaber.init_motor() -----------
# ANSW decode (from FAULHABER manual DE_7000_00029, same table as
# read_faulhaber_config.py):
#   0 = silent      -- no command acknowledgments, no unsolicited/async replies
#   1 = async replies
#   2 = async replies + command acknowledgments
#   3 = debug
# We send ANSW0 (silent): the whole protocol layer already assumes the drive
# never volunteers anything, and silent mode is what makes that true.
# IMPORTANT CONSEQUENCE, and the reason NO_REPLY_COMMANDS below exists: in
# silent mode SET commands (EN/LR/M/AC/...) are NOT acknowledged at all. Any
# code that waits for a reply after them waits for the full serial timeout,
# every single time. Set to None to send no ANSW command at all (leave the
# drive in whatever mode it is currently in).
# NOTE: ANSW is a RAM setting here (this app never SAVEs/EEPSAVs), so it
# persists on the drive until it is power-cycled -- which is why a mode change
# made in one session is still active in the next one.
MOTOR_ANSW_MODE = 0

# Commands the drive does NOT reply to in silent mode (ANSW0) -- matched on
# the LETTER PREFIX of the command, so "LR-180882" matches "LR", "AC50"
# matches "AC", and so on. send_cmd() writes these fire-and-forget and returns
# immediately instead of blocking for a reply that will never arrive.
#
# WHY: with MOTOR_ANSW_MODE = 0 every one of these previously burned
# timeout (1.0 s) + resync probe (~0.2 s) ~= 1.2 s of pure dead waiting, and a
# single Go To sends about a dozen of them -> ~14 s of delay per move, plus a
# wall of misleading "[RESYNC] Timed out" lines for behaviour that is entirely
# correct and expected in silent mode. This is the "No-Reply-Kommandoset in
# send_cmd" item from BACKLOG P1; the rig log that identified exactly which
# commands stay silent is what it was waiting for.
#
# QUERY commands (POS, OST, GAC, GDEC, GSP, GMOD, ...) are deliberately NOT in
# this set -- they DO answer in silent mode (confirmed on the rig: POS/OST
# replies came back normally while every set command timed out), so they keep
# the normal write-then-read path including RESYNC on a genuine timeout.
NO_REPLY_COMMANDS = frozenset({
    "EN",     # enable drive
    "DI",     # disable drive
    "ST",     # stop
    "HP",     # Hard Polarity -- limit-switch polarity bitmask (NOT a motion
              # command; see SEND_LEGACY_HP0 above)
    "V",      # e.g. V0 -- velocity setpoint
    "M",      # start move
    "LR",     # load relative target
    "LA",     # load absolute target
    "AC",     # acceleration ramp
    "DEC",    # deceleration ramp
    "SP",     # speed setpoint
    "ANSW",   # answer mode itself
    "HO",     # set home / zero position
})

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
#
# RAISED 0.10 -> 0.25 after the 2026-07-23 rig free-run log. Evidence: during
# a fast sweep the drive returned POS FRAGMENTS ('1', '103862' where the real
# position was a 7-8 digit number). Fragments mean the reply was still in
# flight when the app moved on -- i.e. the drive was answering LATER than the
# read window allowed, not failing to answer. Waiting a bit longer for a
# complete reply is strictly better than orphaning it and then having its
# leftover digits show up as the "answer" to the next query.
# NEEDS A RIG COMPARISON: run the same free run twice (same speed) before and
# after and compare the outlier count and the point count. If outliers persist
# unchanged, the cause is not reply latency and this should be reverted rather
# than raised further.
POLL_TIMEOUT = 0.25  # s (was 0.10)

# Treat a reply that is NOT CR-terminated as a failed read instead of parsing
# it. pyserial's read_until() hands back whatever arrived when it times out,
# without the terminator -- so half of a POS reply used to become a shorter but
# perfectly parseable (and completely wrong) position. See
# protocol_faulhaber.send_cmd. Set False to restore the old lenient behaviour.
REQUIRE_CR_TERMINATED_REPLY = True

# Longest PAUSE tolerated BETWEEN two bytes of one reply before the reply is
# considered dead. This is NOT the total wait -- that is the per-call timeout
# (POLL_TIMEOUT for polling traffic). The two are separate on purpose:
#
# pyserial's read_until() abandons a reply the first time a single read(1)
# comes back empty, so ONE pause was enough to throw away everything received
# so far. The 2026-07-23 rig log is full of that: 'POS' replies cut to '768',
# '752', '734' -- the leading digits of the live position. At 9600 baud a
# character takes ~1 ms, so this was never a bandwidth limit; the drive
# stopped mid-message for longer than the read window and the rest was lost.
#
# protocol_faulhaber._read_reply_until_cr() now keeps waiting through such
# gaps until either CR arrives or the total deadline expires.
REPLY_GAP_TOLERANCE_S = 0.08  # s

# Settle time before the FIRST write after a read that timed out or came back
# truncated. In that situation the drive still owes us a reply which may not
# have started arriving yet, so checking in_waiting proves nothing -- waiting
# this long and then draining makes sure the orphaned reply is discarded whole
# instead of being cut in half by the next write (which is what turns it into
# a parseable-but-wrong position). Longer than WRITE_SETTLE_S on purpose: it
# only costs anything on the rare recovery path, not on every command.
SERIAL_RECOVERY_SETTLE_S = 0.05  # s

# Re-check cadence for pause/stop loops that wait on user action
# (e.g. during a jog or reference-run wait). New in this pass,
# conceptually replaces scattered hardcoded time.sleep(0.05) calls
# (the hardcoded spots in the monolith have not all been switched to
# this constant yet -- see BACKLOG P3, "make GUI-agnostic").
PAUSE_POLL_S = 0.05  # s

# How long stop_action() waits for a running scan/free-run worker to actually
# leave its loop before re-arming the drive (recover_after_stop(), which
# clears stop_flag). MUST be longer than one worker iteration, or the flag can
# be cleared before the worker ever looks at it -- that was the "Stop does
# nothing, press it several times" bug: stop_flag was only True for the ~20 ms
# that safe_stop() takes, while the worker's sleep loops only check it every
# PAUSE_POLL_S (50 ms). Bounded so a wedged worker cannot block re-arming
# forever.
STOP_JOIN_TIMEOUT_S = 10.0  # s


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
# FREE RUN RESOLUTION ESTIMATE (scan_engine.free_run_scan_action)
# =====================================================================
# Measured via probe_move_timing.py on THIS rig, 2026-07-22 (see
# BACKLOG.md / the probe_report_*.md files for the full raw data): average
# motor steps between two consecutive POS+DAQ polls, for 90000-step moves
# (both directions, ~19-20 Hz measured poll rate), at three tested SP
# (max-speed) settings.
#
# NOT a validated physical model -- do not extrapolate a "steps per rpm"
# formula from this. Two things the raw data showed clearly:
#   1) Resolution scales MUCH less than proportionally with SP: a 10x lower
#      SP (10000 -> 1000) only gave ~1.6x finer spacing, not 10x. For these
#      move distances the AC/DEC ramp (not user-tunable in the GUI) turned
#      out to dominate average velocity far more than SP does -- the moves
#      mostly never reach a true SP-limited cruise plateau.
#   2) Run-to-run variance AT A FIXED SP was up to ~2x in the raw data
#      (e.g. SP=2000: 1406 to 2812 steps/poll across repeats of the same
#      90000-step move). Any number derived from this table is an
#      order-of-magnitude estimate for the GUI info label, not a guarantee
#      -- shown to the user with that caveat (see gui_main.py).
# Re-run probe_move_timing.py with --sp if a specific SP needs a real
# (measured, not interpolated) number.
FREE_RUN_SP_STEPS_PER_POLL = [
    (1000, 1653),
    (2000, 2155),
    (10000, 2658),
]


# =====================================================================
# POSITION TOLERANCE ("when does a target count as reached?")
# =====================================================================

# Base value in motor steps, immediately replaced below by a floor
# (see POS_TOL_STEPS). Left here as a constant because the floor
# assumes a starting value exists (origin artifact from the
# monolith, functionally irrelevant since it's overwritten right away).
_POS_TOL_STEPS_BASE = 5

# Effective position tolerance, used in motion.wait_until_position() as the
# default for the 'tol' parameter.
#
# MEASURED ON THE RIG 2026-07-24 (step_resolution_test.py, 10 moves per step
# size at 0.01 / 0.001 / 0.0001 nm): the drive lands on target with a final
# error of 0-1 encoder counts EVERY time, at every step size. It is a closed
# loop on the same encoder POS reports, so exact arrival is what it can do.
# The tightest candidate tolerance that succeeded on all 30 moves was 20
# steps -- the drive's own GCORRIDOR.
#
# The old value was `0.01 * STEPS_PER_NM` = 3617 steps = 10 pm, which is
# EXACTLY one 0.01 nm scan step and TEN TIMES a 0.001 nm step. A move
# therefore counted as arrived before it had meaningfully started, and the
# individual points of a fine scan did not sit where their label said.
#
# 90 steps (0.25 pm) chosen rather than the measured minimum of 20:
#   * 90x margin over the worst error actually observed (1 step),
#   * 4.5x the drive's own 20-step control corridor, so the app is not
#     demanding tighter positioning than the servo itself targets,
#   * still 4x finer than a 0.001 nm step (362 steps) and 40x finer than a
#     0.01 nm step, so the tolerance is no longer the dominant error.
#
# Going FINER than 0.001 nm needs a smaller value again (0.0001 nm is only 36
# steps, so 90 would be 2.5x the step). Re-run step_resolution_test.py before
# doing that -- and note the caveat below.
#
# WHAT THIS MEASUREMENT DOES *NOT* PROVE: it reads POS, i.e. the ENCODER.
# It shows the servo positions superbly; it says nothing about whether the
# grating actually moved. At 36 steps (0.0001 nm) stiction in the drive train
# could leave the optics stationary while the encoder reaches target. That
# question needs an OPTICAL test: scan a known line with fine steps and check
# that the peak is genuinely resolved rather than smeared.
POS_TOL_STEPS = 90

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
# He I (D3) at 587.5618 nm -- the reference line used on this rig. Close to
# the Na-D doublet region the instrument is usually parked at, so the
# calibration does not require a long traverse. (Was 656.300 = H-alpha.)
CAL_CENTER_NM_DEFAULT = "587.5618"  # He I D3
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
# =====================================================================
# UI THEME -- all colours in one place
# =====================================================================
# Every colour the GUI uses lives here so the look can be adjusted by hand
# without hunting through gui_main.py. Values are CSS hex strings.
#
# SCOPE NOTE: these are consumed by gui_main.py only. They sit in this file
# because it is the project's single "edit these by hand" module, not because
# they are device properties.
#
# CHANGES TAKE EFFECT ON RESTART: the stylesheet is built once at import and
# most widgets bake their colours in at construction time.

# --- surfaces -------------------------------------------------------------
C_BG          = "#0e1116"   # window background
C_PANEL       = "#161b22"   # panel / section surfaces
C_PANEL2      = "#1c232d"   # input fields
C_BORDER      = "#2a323d"   # borders, separators, grid lines

# --- text -----------------------------------------------------------------
C_TEXT        = "#e6edf3"   # normal text and values
C_MUTED       = "#8b949e"   # section headers, units, secondary text

# --- accents and states ---------------------------------------------------
C_ACCENT      = "#3ad0c8"   # teal: primary action, active values
C_ACCENT_D    = "#1f8f89"   # darker teal: pressed/hover on primary buttons
C_ON_ACCENT   = "#06110f"   # text ON a teal button (must contrast with C_ACCENT)
C_WARN        = "#d8a657"   # warnings
C_ERROR       = "#e5534b"   # errors, danger buttons, alarm threshold
C_OK          = "#3fb950"   # success, "connected", normal signal level
C_AMBER       = "#d29922"   # AI monitor warn band (between WARN_V and ALARM_V)

# --- disabled / inactive --------------------------------------------------
C_DISABLED_FG = "#4b535d"   # disabled button text
C_DISABLED_BD = "#232a33"   # disabled button border

# --- plot -----------------------------------------------------------------
C_LEGEND_BG   = (20, 26, 34, 200)   # legend backdrop, R,G,B,A -- semi-transparent
                                    # so traces underneath stay visible

# Colours assigned to scan traces, in order, cycling when exhausted.
# EXPANDED FROM 8 TO 16: with only eight, a ninth scan reused the first
# colour and the two became indistinguishable in the legend -- and comparing
# ten repeat scans is a routine measurement here (see BACKLOG drift series).
# Chosen numerically for separability ON THE DARK BACKGROUND, not by eye:
# every colour has >= 5.1:1 contrast against C_BG, the closest PAIR is 26.6
# CIELAB deltaE apart (the previous 8-colour set had a pair at 16.1 -- sand
# vs orange were genuinely easy to confuse), and CONSECUTIVE entries are at
# least 53 deltaE apart so two scans run back to back never look alike.
# Saturation is deliberately capped: a purely distance-optimal set came out
# neon (#25f725, #e225f7) and clashed with the rest of the interface.
# If you edit these by hand, keep those three properties in mind.
PLOT_PALETTE = [
    "#3ad0c8",   #  1 teal (matches C_ACCENT)
    "#eb5e71",   #  2 red
    "#5eeb9b",   #  3 mint
    "#dd5eeb",   #  4 magenta
    "#ebd196",   #  5 sand
    "#5e7feb",   #  6 blue
    "#88bd4b",   #  7 olive
    "#eb915e",   #  8 orange
    "#5ebceb",   #  9 sky
    "#bd79a2",   # 10 mauve
    "#ebe15e",   # 11 yellow
    "#7984bd",   # 12 slate blue
    "#79bd8d",   # 13 sage
    "#eb5eb2",   # 14 pink
    "#5eeb5e",   # 15 green
    "#bd8679",   # 16 clay
]

# =====================================================================
# LIVE AI MONITOR (status-bar readout + bar graph, gui_main._refresh_ai)
# =====================================================================
# Continuously shows the current analog-input voltage so the PMT/HV can be
# adjusted without starting a scan.
#
# THREAD/HARDWARE NOTE: an NI-DAQ read creates and tears down a Task on the
# channel. Two threads doing that on the SAME channel at the same time makes
# NI-DAQmx fail with a resource-reservation error. The monitor therefore
# ACTIVELY polls only while idle; during a scan it just displays the value
# the scan's own read already produced (state['last_pmt_v']), costing nothing.
# =====================================================================
# SIMULATOR: mechanical backlash (sim_hardware.FakeFaulhaber)
# =====================================================================
# The simulator used to have ZERO backlash -- "M" added the commanded delta to
# POS exactly, and the fake PMT signal was derived from the APP's own
# current_nm bookkeeping rather than from any simulated mechanics. Nothing
# position-related could therefore be reproduced off the rig: running the
# backlash calibration against SIM returned ~0.002 nm, which was pure centroid
# noise on the simulated signal (verified: median 0.00199 nm over 200 runs with
# a true backlash of exactly 0).
#
# With this set > 0 the simulator models real slack: on a direction REVERSAL
# the first SIM_BACKLASH_NM worth of travel turns the motor (and is counted by
# POS, exactly as a real encoder would) WITHOUT moving the grating. The
# simulated signal follows the grating, not the encoder -- so backlash
# compensation, the calibration routine and scan drift all become testable
# without hardware.
#
# 0.082 nm is the value measured on the rig, which makes SIM behave like the
# real instrument. Set to 0.0 to get the old ideal-mechanics simulator back.
SIM_BACKLASH_NM = 0.082

# --- Simulator motion timing -------------------------------------------
# The simulated drive used to execute "M" INSTANTLY: POS jumped straight to
# the target. Free Run polls position+signal continuously DURING one long
# move, so in SIM the sweep was already over before the loop could sample it
# and the plot got exactly ONE point. Stepped scans were unaffected (each
# step is its own move), which is why this went unnoticed.
#
# Both figures below are derived from the rig, not invented: the 2026-07-23
# speed sweep ran identical 5 nm free runs at 1000-10000 rpm and logged the
# point count at the measured ~19.5 Hz poll rate.
#   * Below ~2000 rpm the duration scales linearly at 61 steps/s per rpm.
#   * Above that the ramp dominates and the sweep never got faster than about
#     280 000 steps/s (5 nm in ~6.5 s), so the speed is capped there.
# Artificial delay on every simulated POS reply, in seconds.
# WHY: the fake port answers instantly, so a Free Run in SIM polled as fast as
# Python could loop -- 27 000 points for a 1 nm sweep, where the rig produces
# about 20 per second. That is not a useful preview and it bloats the plot and
# the CSV. On the instrument the rate is set by the DAQ task setup plus the
# serial round trip (~19-20 Hz measured). 5 ms here gives a few hundred hertz:
# still faster than the rig, so tests stay quick, but the same order of
# magnitude of data rather than three orders too many. Raise it towards 0.05
# to reproduce the rig's point density exactly.
SIM_POLL_LATENCY_S = 0.005

SIM_STEPS_PER_S_PER_RPM = 61.0
SIM_MAX_STEPS_PER_S = 280000.0

AI_MONITOR_ENABLED = True
AI_MONITOR_HZ = 4.0          # GUI refresh rate while idle (3-5 Hz is plenty)

# Colour thresholds for the bar graph / value label, in volts.
#   v <  AI_WARN_V   -> green
#   AI_WARN_V <= v < AI_ALARM_V -> yellow
#   v >= AI_ALARM_V  -> red
AI_WARN_V = 6.0
AI_ALARM_V = 7.0

# Full-scale value of the bar graph (the DAQ channel is configured
# 0..10 V in daq.read_pmt_voltage).
AI_BAR_MAX_V = 10.0

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
# --- OST digital input bits: THESE ARE RAW LEVELS, NOT "TRIPPED" FLAGS -----
# Manual Tab. 8: bits 8/9/10 are "Zustand Eingang 1/2/3" -- the electrical
# LEVEL of the input, nothing more.
#
# On THIS rig the limit switches are ACTIVE LOW, measured directly (three
# read_faulhaber_config.py runs while pressing each endstop by hand):
#     OST 0x0500  = bit8+bit10 set  -> BOTH endstops FREE (idle/normal state)
#     OST 0x0400  = bit8 cleared    -> Input 1 endstop PRESSED
#     OST 0x0100  = bit10 cleared   -> Input 3 endstop PRESSED
# This matches the drive's own configuration: IOC reports Hard Polarity
# HP = 00000, and per the manual HP bit 0 means "falling edge / LOW level
# valid" -- so LOW is the engaged state.
#
# => AN ENDSTOP IS ENGAGED WHEN ITS BIT IS *CLEARED*, NOT WHEN IT IS SET.
#
# Any limit check must therefore test for a 1->0 transition. Testing for
# "bit became set" is exactly backwards and would (a) ignore every real
# endstop hit and (b) fire when a switch is RELEASED. See the dormant guard
# in motion._pos_wait_impl_fallback and BACKLOG for why that guard is still
# deliberately not activated.
OST_ENDSTOP_ACTIVE_LOW = True   # rig-measured, see above

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
#     Not deleted (doc policy), only clearly flagged. ---
#
# WARNING -- the old example values in the comments below were INVERTED and
# have been corrected to a warning instead: "256 = hard limit reached" and
# "1024 = internal software limit" would be bit8 (Input 1) and bit10
# (Input 3) SET. Per OST_ENDSTOP_ACTIVE_LOW above, those bits being SET means
# both endstops are FREE. Entering them here would make postmove_limit_audit()
# report a limit fault after EVERY normal move -- the rig's normal idle OST is
# 0x0500, i.e. both bits set (visible in the app's own "[AUDIT] Post-move OST
# OK: 0x0500" log lines).
#
# A working limit check cannot be expressed as a simple OR-mask at all,
# because the fault condition here is a bit being CLEAR. It needs an explicit
# active-low comparison. Deliberately NOT built speculatively -- see BACKLOG.
OST_HARD_LIMIT_MASK     = 0x0000  # disabled (e.g. 256 for hard/limit reached)
OST_INTERNAL_LIMIT_MASK = 0x0000  # disabled (e.g. 1024 for internal software limit)

# --- OST log throttling (protocol_faulhaber.read_ost) ---
LOG_OST_VERBOSE    = True   # False = mute periodic OST logs
OST_LOG_THROTTLE_S = 0.5    # minimum interval between OST logs (s)
