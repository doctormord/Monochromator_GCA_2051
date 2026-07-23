#!/usr/bin/env python3
"""
read_faulhaber_config.py — Standalone FAULHABER full-config dump (read-only).

WHAT THIS SCRIPT DOES
----------------------
Reads and decodes the CURRENTLY ACTIVE configuration of the FAULHABER motion
controller off the rig -- everything needed to answer, with real numbers
instead of guesses:

  1. Is POS a genuine (encoder/Hall) position readback, or something else?
     -> GTYP, GMOD/CST (FAULHABER mode), GENCRES (encoder resolution)
  2. Are hardware limit switches (Endschalter) wired and configured, and on
     which inputs? Has one ever been tripped (even if it's released again)?
     -> IOC (hard-blocking/polarity/direction per input), HOC (homing
        sequence config), SWS (LATCHED switch-trip history -- unlike OST's
        IN1/IN2/IN3 which only show the CURRENT level)
  3. Is ANSW (asynchronous replies / command acknowledgments) enabled? The
     manual states the FACTORY/MOTION-MANAGER DEFAULT has these ON -- and
     this app never sends ANSW to force it off. If true, every LR/M/EN this
     app sends may get an unsolicited "OK\r\n" queued back, which -- combined
     with the RESYNC-on-timeout path re-probing POS -- is a plausible
     mechanism for two replies' digits landing in one read_until(b"\r") and
     being parsed as one bogus-but-valid large integer (a wild POS "spike").
     -> CST bits 1-2 decode ANSW directly.

All commands used here are GET-only (no SAVE/EEPSAV, nothing written to the
drive). Companion to tune_ramp.py (ramp/speed) and probe_move_timing.py
(poll timing) -- this one is about WHAT the drive is actually configured to
DO, not how fast.

Reference: FAULHABER RS232 Motion Control manual 7000.00029, chapters 3.3
(sensor/mode), 3.5 (Endschalter), 4.2 (ANSW), 7.2 (query commands).

USAGE
-----
    python3 read_faulhaber_config.py COM3
        -> one-shot full config dump (see above), then exits.

    python3 read_faulhaber_config.py COM3 --watch
        -> after the dump, polls the digital inputs LIVE and prints a line
           every time something changes -- press a physical endstop while
           this is running and watch which input (IN1/IN2/IN3/...) reacts.
           Shows both the LIVE level (OST) and the LATCHED trip flag (SWS,
           stays set even after you release the switch again). Ctrl+C to
           stop. Read-only, same as the rest of this script -- nothing is
           written to the drive, no motion is commanded.

Requires only pyserial.
"""

import sys
import time
import argparse

import serial


def send(ser, cmd: str, timeout: float = 1.0) -> str:
    """Send one ASCII command (CR-terminated) and return the CR-terminated
    reply, stripped. Minimal, no resync/retry logic -- this is a diagnostic
    tool, not the production protocol layer (see protocol_faulhaber.py)."""
    ser.reset_input_buffer()
    ser.reset_output_buffer()
    ser.write((cmd + "\r").encode("ascii"))
    ser.flush()
    ser.timeout = timeout
    return ser.read_until(b"\r").decode(errors="ignore").strip()


def get_raw(ser, cmd, label, timeout=1.0):
    raw = send(ser, cmd, timeout=timeout)
    print(f"  {label:38s} ({cmd:10s}) = {raw!r}")
    return raw


def get_int(ser, cmd, label, unit="", timeout=1.0):
    raw = send(ser, cmd, timeout=timeout)
    try:
        val = int(raw)
        print(f"  {label:38s} ({cmd:10s}) = {val}{unit}")
        return val
    except Exception:
        print(f"  {label:38s} ({cmd:10s}) = <no/invalid reply: {raw!r}>")
        return None


# ---------------------------------------------------------------------
# Bit-field decoders (from the manual's tables 4/5/6/7/8/9)
# ---------------------------------------------------------------------

_ANSW_NAMES = {0: "ANSW0 (silent, no acks/async)", 1: "ANSW1 (async replies)",
              2: "ANSW2 (async replies + command acks)", 3: "ANSW3 (debug)"}
_SOR_NAMES = {0: "SOR0 (RS232)", 1: "SOR1 (analog voltage)", 2: "SOR2 (PWM)",
             3: "SOR3 (current limit)", 4: "SOR4 (current limit + dir via polarity)"}
_MODE_NAMES = {0: "CONTMOD", 1: "STEPMOD", 2: "APCMOD", 3: "ENCMOD/HALLSPEED",
              4: "ENCMOD/ENCSPEED", 5: "GEARMOD", 6: "VOLTMOD", 7: "IXRMOD"}
_FAULTPIN_FUNC = {0: "ERROUT", 1: "ENCOUT", 2: "DIGOUT", 3: "DIRIN", 4: "REFIN"}
_INPUT_NAMES = ["AnIn", "Fault-Pin", "3rd input", "4th input (MCDC)", "5th input (MCDC)"]


def decode_cst(val):
    print(f"\n  --- CST decoded (0x{val:04X} / {val}) ---")
    answ = (val >> 1) & 0b11
    sor = (val >> 3) & 0b111
    mode = (val >> 7) & 0b111
    print(f"    ANSW (answer mode)      : {_ANSW_NAMES.get(answ, answ)}")
    print(f"    SOR (speed source)      : {_SOR_NAMES.get(sor, sor)}")
    print(f"    FAULHABER mode          : {_MODE_NAMES.get(mode, mode)}")
    print(f"    Power stage (EN/DI)     : {'Enabled (EN)' if (val >> 10) & 1 else 'Disabled (DI)'}")
    print(f"    Position controller     : {'ON' if (val >> 11) & 1 else 'OFF'}")
    print(f"    Analog direction        : {'ADR' if (val >> 12) & 1 else 'ADL'}")
    print(f"    Position limits (APL)   : {'ACTIVE' if (val >> 13) & 1 else 'inactive'}")
    print(f"    Sine commutation        : {'no block-comm at high speed' if (val >> 14) & 1 else 'block-comm allowed'}")
    print(f"    Network mode (NET)      : {'Multiplex active' if (val >> 15) & 1 else 'standalone'}")
    if answ != 0:
        print(f"    *** ANSW is NOT 0 -- unsolicited/acknowledgment traffic IS possible.")
        print(f"        This app never sends ANSW0. See script docstring for why that")
        print(f"        matters for the POS-spike investigation. ***")


def decode_ioc(val):
    print(f"\n  --- IOC decoded (0x{val:08X} / {val}) ---")
    hb = val & 0xFF
    hp = (val >> 8) & 0xFF
    hd = (val >> 16) & 0xFF
    out_state = (val >> 24) & 1
    io_level = (val >> 25) & 1
    faultpin = (val >> 26) & 0b111
    print(f"    Hard-Blocking (HB) active on inputs 1-5 : {format(hb, '05b')} (bit0=In1..bit4=In5)")
    for i in range(5):
        if hb & (1 << i):
            print(f"      -> Input {i+1} ({_INPUT_NAMES[i]}): HARD-BLOCKING ENABLED "
                  f"(this input IS configured as a physical limit switch)")
    print(f"    Hard Polarity (HP) rising-edge on 1-5   : {format(hp, '05b')}")
    # Spell out what HP means per input -- this decides how the OST level bits
    # must be READ, and getting it backwards inverts every limit check.
    # Manual 3.5.3.1: HP bit 1 = rising edge / HIGH level valid,
    #                 HP bit 0 = falling edge / LOW level valid.
    for i in range(5):
        if hb & (1 << i):
            if hp & (1 << i):
                print(f"      -> Input {i+1}: ACTIVE HIGH -- endstop is ENGAGED while its "
                      f"OST bit {8+i} is SET")
            else:
                print(f"      -> Input {i+1}: ACTIVE LOW -- endstop is ENGAGED while its "
                      f"OST bit {8+i} is CLEARED (bit SET = switch free)")
    print(f"    Hard Direction (HD) CW-locked on 1-5    : {format(hd, '05b')}")
    # HD only matters for inputs that also have HB enabled. Manual 3.5.3.1:
    # HD bit 1 = clockwise (Rechtslauf) blocked, HD bit 0 = counter-clockwise
    # (Linkslauf) blocked. A pair of end-of-travel switches should normally
    # block OPPOSITE directions -- one stops travel at each end.
    hb_inputs = [i for i in range(5) if hb & (1 << i)]
    for i in hb_inputs:
        d = "CW (Rechtslauf) blocked" if (hd & (1 << i)) else "CCW (Linkslauf) blocked"
        print(f"      -> Input {i+1} blocks: {d}")
    if len(hb_inputs) >= 2:
        dirs = {bool(hd & (1 << i)) for i in hb_inputs}
        if len(dirs) == 1:
            blocked = "CW (Rechtslauf)" if dirs.pop() else "CCW (Linkslauf)"
            print(f"    *** WARNING: all hard-blocking inputs block the SAME direction")
            print(f"        ({blocked}). For a pair of end-of-travel switches this means")
            print(f"        only ONE end is actually protected -- the drive can be run")
            print(f"        straight through the other one in the opposite direction.")
            print(f"        Verify on the rig which physical end each input belongs to")
            print(f"        before changing HD (changing it needs a write + SAVE, which")
            print(f"        this script deliberately never does). ***")
    print(f"    Digital output (DIGOUT) level           : {'High' if out_state else 'Low'}")
    print(f"    Digital input level standard            : {'24V (PLC)' if io_level else '5V (TTL)'}")
    print(f"    Fault-Pin function                      : {_FAULTPIN_FUNC.get(faultpin, faultpin)}")
    if faultpin != 4:
        print(f"    NOTE: Fault-Pin is NOT set to REFIN -- if a limit switch is wired")
        print(f"          to the Fault-Pin specifically, it will NOT be evaluated as a")
        print(f"          reference/limit input in this configuration.")
    if hb == 0:
        print(f"    NOTE: HB=0 on ALL inputs -- no input is configured for Hard-Blocking.")
        print(f"          Physical endstops (if wired) may still report their raw level via")
        print(f"          OST bits 8-10 (IN1/IN2/IN3, already used by this app for the")
        print(f"          post-move fault check) without being 'blocking' limit switches.")


def decode_hoc(val):
    print(f"\n  --- HOC decoded (0x{val:08X} / {val}) ---")
    sha = val & 0xFF
    shn = (val >> 8) & 0xFF
    shl = (val >> 16) & 0xFF
    poweron = (val >> 24) & 1
    print(f"    SHA (zero position on edge), inputs 1-5 : {format(sha, '05b')}")
    print(f"    SHN (RS232 notify on edge), inputs 1-5  : {format(shn, '05b')}")
    print(f"    SHL (stop motor on edge), inputs 1-5    : {format(shl, '05b')}")
    print(f"    Power-On Homing Sequence                : {'ENABLED' if poweron else 'disabled'}")
    if sha == 0 and shn == 0 and shl == 0:
        print(f"    NOTE: no homing sequence configured on any input -- GOHOSEQ would be a no-op.")


def decode_sws(val):
    print(f"\n  --- SWS decoded (0x{val:08X} / {val}) -- LATCHED switch history ---")
    tripped = (val >> 24) & 0xFF
    print(f"    Endschalter EVER tripped since last reset (bits 24-31): {format(tripped, '08b')}")
    for i in range(5):
        if tripped & (1 << i):
            print(f"      -> Input {i+1} ({_INPUT_NAMES[i]}): HAS TRIPPED (even if released again now)")
    if tripped == 0:
        print(f"      -> no input has latched a trip since it was last cleared/reconfigured.")
        print(f"      NOTE: SWS bits 0-23 are the HA/HN/HL homing-sequence settings, and")
        print(f"            bits 24-31 latch which of THOSE switches has already fired.")
        print(f"            Verified at the rig: with HOC = 0 (no homing sequence armed on")
        print(f"            any input) pressing a hard-blocking endstop by hand does NOT")
        print(f"            set anything here -- SWS stayed 0 across three runs while both")
        print(f"            endstops were pressed in turn. So SWS is NOT a usable endstop")
        print(f"            history on this configuration; the live OST level is the only")
        print(f"            signal. Use --watch to see it move.")


def decode_ost(val, hb=None, hp=None):
    """Decode OST. If the IOC-derived hard-blocking mask (hb) and polarity
    mask (hp) are supplied, the digital-input bits are additionally reported
    as ENGAGED/FREE rather than only as a raw level.

    WHY THIS MATTERS: OST bits 8-12 are "Zustand Eingang 1..5" -- the raw
    electrical LEVEL of the input (manual Tab. 8), NOT a "switch tripped"
    flag. With the usual HP=0 configuration a LOW level is the valid endstop
    condition, so a PRESSED endstop CLEARS its bit. Printing only the set bits
    as "ACTIVE" (the previous behaviour of this function) reads exactly
    backwards to a human: at the rig, both endstops free shows as
    0x0500 = "Input1 ACTIVE + Input3 ACTIVE"."""
    print(f"\n  --- OST decoded (0x{val:04X} / {val}) -- for reference, app already logs this ---")
    names = ["Homing running", "Program sequence running", "Stopped (DELAY)",
             "Stopped (NOTIFY)", "Current limit active", "DEVIATION error",
             "Overvoltage", "Overtemperature", "Input1 level", "Input2 level",
             "Input3 level", "Input4 level", "Input5 level"]
    for i, name in enumerate(names):
        if val & (1 << i):
            print(f"    bit{i:2d}  {name}: high")
    # Endstop interpretation for the inputs that are actually configured as
    # hard-blocking limit switches.
    if hb:
        print(f"    -- endstop interpretation (from IOC: HB={format(hb,'05b')}, "
              f"HP={format(hp or 0,'05b')}) --")
        for i in range(5):
            if not (hb & (1 << i)):
                continue
            level_high = bool(val & (1 << (8 + i)))
            active_high = bool((hp or 0) & (1 << i))
            engaged = (level_high == active_high)
            print(f"      Input {i+1}: {'ENGAGED (endstop pressed)' if engaged else 'free'}"
                  f"  [level {'HIGH' if level_high else 'LOW'}, "
                  f"active-{'high' if active_high else 'low'}]")


def watch_inputs(ser, poll_interval=0.1):
    """Live-poll OST (live input level, bits 8-12) and SWS (latched trip
    flags, bits 24-31) and print a line whenever either changes. Meant to be
    run while the operator physically presses an endstop -- makes it obvious
    which input reacts to which physical switch. Read-only, Ctrl+C to stop."""
    print("\n=== Live input watch -- press an endstop now (Ctrl+C to stop) ===")
    print(f"{'time':>8s}  {'OST live (In1..In5)':<22s}  {'SWS latched-trip (In1..In5)':<28s}  changed")

    def read_bits(cmd, shift, width=5):
        raw = send(ser, cmd, timeout=0.5)
        try:
            val = int(raw)
        except Exception:
            return None
        return (val >> shift) & ((1 << width) - 1)

    last_live = None
    last_latched = None
    t0 = time.time()
    try:
        while True:
            live = read_bits("OST", 8)     # bits 8-12 = In1..In5 current level
            latched = read_bits("SWS", 24)  # bits 24-31 = In1..In5(+) ever tripped
            if live is None or latched is None:
                time.sleep(poll_interval)
                continue
            if live != last_live or latched != last_latched:
                changed = []
                if last_live is not None:
                    diff = live ^ last_live
                    for i in range(5):
                        if diff & (1 << i):
                            state = "ACTIVE" if (live & (1 << i)) else "released"
                            changed.append(f"In{i+1}={state}")
                if last_latched is not None and latched != last_latched:
                    newly = latched & ~last_latched
                    for i in range(5):
                        if newly & (1 << i):
                            changed.append(f"In{i+1} LATCHED (first trip)")
                t = time.time() - t0
                print(f"{t:8.2f}s  {format(live, '05b'):<22s}  {format(latched, '08b'):<28s}  "
                      f"{', '.join(changed) if changed else '(initial read)'}")
                last_live, last_latched = live, latched
            time.sleep(poll_interval)
    except KeyboardInterrupt:
        print("\n=== Watch stopped. ===")
        print("Reminder: SWS latched bits stay set until the input is reconfigured "
              "(HB/HP/etc. re-sent) -- a 'LATCHED' In you saw once will keep showing "
              "as latched=1 even after release, that's expected/documented behaviour, "
              "not a bug.")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("port")
    p.add_argument("--baud", type=int, default=9600)
    p.add_argument("--watch", action="store_true",
                   help="After the config dump, live-watch the digital inputs "
                        "(press an endstop to see which one reacts). Ctrl+C to stop.")
    p.add_argument("--watch-only", action="store_true",
                   help="Skip the full config dump, go straight to live input watch.")
    args = p.parse_args()

    print(f"Connecting to {args.port} @ {args.baud} baud...")
    ser = serial.Serial(args.port, args.baud, timeout=1.0)
    import time as _t
    _t.sleep(0.3)

    try:
        if not args.watch_only:
            print("\n=== Identity ===")
            get_raw(ser, "GTYP", "Controller type")
            get_raw(ser, "GSER", "Serial number")
            get_raw(ser, "VER", "Firmware version")

            print("\n=== Question 1: sensor/mode (is POS a real position readback?) ===")
            cst = get_int(ser, "CST", "Configuration status (raw)")
            if cst is not None:
                decode_cst(cst)
            get_raw(ser, "GMOD", "Get Mode (redundant check vs CST)")
            get_int(ser, "GENCRES", "Encoder resolution (ENCRES)", " [4x pulses/rev]")
            get_int(ser, "GKN", "Speed constant KN", " [rpm/V] (motor ID hint)")
            get_int(ser, "GRM", "Motor resistance RM", " [mOhm] (motor ID hint)")
            get_int(ser, "GSTW", "Step width (STW)", " (only meaningful in STEPMOD/GEARMOD)")
            get_int(ser, "GSTN", "Steps per rev (STN)", " (only meaningful in STEPMOD/GEARMOD)")

            print("\n=== Question 2: hardware limit switches (Endschalter) ===")
            ioc = get_int(ser, "IOC", "I/O configuration (raw)")
            if ioc is not None:
                decode_ioc(ioc)
            hoc = get_int(ser, "HOC", "Homing configuration (raw)")
            if hoc is not None:
                decode_hoc(hoc)
            get_int(ser, "GHOSP", "Homing speed (HOSP)", " [rpm, signed = direction]")
            sws = get_int(ser, "SWS", "Switch status / latched trips (raw)")
            if sws is not None:
                decode_sws(sws)
            get_int(ser, "GPL", "Positive SOFTWARE limit (LL)", " [steps] (APL range, not a physical switch)")
            get_int(ser, "GNL", "Negative SOFTWARE limit (LL)", " [steps]")

            print("\n=== Position-loop tuning (context for the DEVIATION fault seen at the rig) ===")
            get_int(ser, "GDEV", "Deviation threshold (DEV)")
            get_int(ser, "GCORRIDOR", "Target corridor / position window (CORRIDOR)", " [steps]")
            get_int(ser, "GPP", "Position controller P-term (PP)")
            get_int(ser, "GPD", "Position controller D-term (PD)")

            print("\n=== Live snapshot ===")
            get_int(ser, "POS", "Actual position")
            get_int(ser, "TPOS", "Target position")
            ost = get_int(ser, "OST", "Operation status (raw)")
            if ost is not None:
                # Hand the endstop configuration read further up into the OST
                # decode, so the input bits can be reported as ENGAGED/free
                # instead of just as raw levels (see decode_ost's docstring --
                # reading them as levels is how "both endstops free" ends up
                # looking like "Input1 ACTIVE + Input3 ACTIVE").
                _hb = (ioc & 0xFF) if ioc is not None else None
                _hp = ((ioc >> 8) & 0xFF) if ioc is not None else None
                decode_ost(ost, hb=_hb, hp=_hp)
            get_int(ser, "TEM", "Housing temperature",
                    " [C] (housing, not winding -- coil temp is modelled internally)")

            print("\n=== Config dump done. Nothing was written to the drive (all GET-only). ===")

        if args.watch or args.watch_only:
            watch_inputs(ser)

    finally:
        ser.close()


if __name__ == "__main__":
    main()
