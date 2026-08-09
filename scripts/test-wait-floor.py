#!/usr/bin/env python3
"""
test-wait-floor — a wait too short for the target is refused, not rounded away.

`wait 0.0004 seconds` used to emit a delay of zero. Not a warning, not an
error: the wait simply was not there, the loop ran at full speed, and the only
way to find out was to watch the board do the wrong thing. It was found from
the other end — the C reader could not round-trip the program, because by the
time it saw the code there was nothing left to read back.

The boundary is the floor *inclusive*, which is the part that catches people:
Python rounds halves to even, so `round(0.5) == 0` and `wait 0.5 ms` — an
entirely reasonable scan dwell — vanished along with the smaller ones.

What is asserted here is the refusal AND its shape: that it names the line, the
device, and a number the user can act on; that `wait 0` still means a yield;
that the floor is read from the target rather than assumed; and that nothing
above the floor changed.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import stc_pseudocode as ps                                    # noqa: E402

checks = failures = 0


def ok(cond, what):
    global checks, failures
    checks += 1
    if not cond:
        failures += 1
        print(f"  FAIL {what}")


def program(wait: str, device: str = "STC12C5A60S2") -> str:
    return (f"DEVICE {device}:\n"
            "  CLOCK 11059200\n"
            "  PIN led1 = P1.0 OUTPUT ACTIVE LOW\n"
            "  WHEN started:\n"
            "    FOREVER:\n"
            "      turn on led1\n"
            f"      wait {wait}\n"
            "      turn off led1\n")


def refuses(wait, *fragments, device="STC12C5A60S2", what=""):
    """The program is rejected, and for the stated reason."""
    global checks, failures
    checks += 1
    try:
        ps.emit_c(ps.parse(program(wait, device)))
    except ps.PseudocodeError as exc:
        missing = [f for f in fragments if f.lower() not in str(exc).lower()]
        if missing:
            failures += 1
            print(f"  FAIL {what or wait}: reason omits {missing} -- {exc}")
        return
    failures += 1
    print(f"  FAIL {what or wait}: was accepted")


def emits(wait, expected, device="STC12C5A60S2"):
    """The program compiles, and produces exactly this delay call."""
    global checks, failures
    checks += 1
    try:
        c = ps.emit_c(ps.parse(program(wait, device)))
    except ps.PseudocodeError as exc:
        failures += 1
        print(f"  FAIL wait {wait}: refused but should compile -- {exc}")
        return
    calls = [l.strip() for l in c.splitlines()
             if "delay_ms(" in l and "static" not in l]
    if not any(expected in call for call in calls):
        failures += 1
        print(f"  FAIL wait {wait}: expected {expected!r}, got {calls}")


def test_the_bug():
    print("the wait that disappeared")
    # The original report, in both units it can be written in.
    refuses("0.0004 seconds", "0.4 ms", "no wait at all")
    refuses("0.4 ms", "0.4 ms", "no wait at all")

    # The banker's-rounding case: exactly half a millisecond also rounded to
    # zero, so the refusal has to be inclusive of the floor's half.
    refuses("0.0005 seconds", "0.5 ms", "no wait at all")
    refuses("0.5 ms", "0.5 ms", "no wait at all")


def test_the_refusal_is_useful():
    print("the refusal says where, what, and what to do instead")
    try:
        ps.emit_c(ps.parse(program("0.4 ms")))
    except ps.PseudocodeError as exc:
        ok(exc.line == 7, f"it names the line the wait is on (got {exc.line})")
        ok("STC12C5A60S2" in str(exc), "it names the device, not just 'the target'")
        ok("1 ms or more" in str(exc), "it says what would work instead")
    else:
        ok(False, "0.4 ms was accepted")


def test_what_must_still_work():
    print("everything at or above the floor is untouched")
    emits("0.6 ms", "delay_ms(1)")            # rounds up, but to a real wait
    emits("1 ms", "delay_ms(1)")
    emits("0.001 seconds", "delay_ms(1)")
    emits("0.15 seconds", "delay_ms(150)")
    emits("400 ms", "delay_ms(400)")

    # A deliberate zero is a yield to the scheduler, not a mistake. Refusing it
    # would break the commonest way to say "let the other scripts run".
    emits("0 seconds", "delay_ms(0)")
    emits("0 ms", "delay_ms(0)")


def test_negative():
    print("a negative wait")
    # `-1` parses as Unary('-', Num(1)), not as a negative literal, so this only
    # gets caught if the constant folder looks through the unary minus. Left
    # unfolded it reaches the runtime path and wraps to about 64.5 seconds.
    refuses("-1 seconds", "negative", what="-1 seconds")
    refuses("-0.0001 seconds", "negative", what="-0.0001 seconds")


def test_the_floor_belongs_to_the_target():
    print("the floor is the target's fact, not the walker's")
    for key, target in ps.TARGETS.items():
        ok(isinstance(getattr(target, "wait_floor_ms", None), (int, float)),
           f"{key} declares a numeric wait_floor_ms")
        ok(bool(getattr(target, "wait_floor_reason", "")),
           f"{key} says what sets its floor")

    # The refusal quotes the target's own name, so a board with a different
    # floor would produce a different message without touching `ms_of`.
    for device, display in (("ARDUINO-UNO", "Arduino Uno"),
                            ("ATMEGA328P", "ATmega328P")):
        refuses("0.4 ms", display, device=device,
                what=f"{device} refuses in its own name")


def test_a_variable_wait_is_left_alone():
    print("a computed wait is not a constant and cannot be checked here")
    # Nothing to fold, so nothing to refuse: the value is not known until it
    # runs. Asserting this deliberately, so that a later change that starts
    # rejecting variable waits has to say so.
    src = ("DEVICE STC12C5A60S2:\n  CLOCK 11059200\n"
           "  PIN led1 = P1.0 OUTPUT ACTIVE LOW\n"
           "  WHEN started:\n    set n to 5\n    FOREVER:\n"
           "      turn on led1\n      wait n ms\n      turn off led1\n")
    try:
        c = ps.emit_c(ps.parse(src))
        ok("delay_ms(" in c, "a variable wait still compiles")
    except ps.PseudocodeError as exc:
        ok(False, f"a variable wait was refused: {exc}")


test_the_bug()
test_the_refusal_is_useful()
test_what_must_still_work()
test_negative()
test_the_floor_belongs_to_the_target()
test_a_variable_wait_is_left_alone()

print(f"\n{checks} checks, {failures} failures")
sys.exit(1 if failures else 0)
