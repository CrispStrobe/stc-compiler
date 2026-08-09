#!/usr/bin/env python3
"""
test-pinmap — a pin number is a package fact, not a range check.

Every board here numbers its pins consecutively, which makes `0 <= n <= max`
look like the whole rule. It is not. Inside the range there are pins that
exist but cannot do what the number implies, and the failure mode is the bad
one: `digitalWrite` to a pin with no digital buffer is accepted by the
compiler, accepted by the board, and does nothing at all. Nothing reports it.

So the rule this file guards is that a pin is refused *at parse time, naming
the board and the reason*, whenever the board cannot do what was asked --
and that the refusals are distinguishable, because "A6 does not exist" and
"A6 exists but is analog-only" send the reader to different places.

The four traps, all of them real silicon:

  Arduino Nano   A6, A7   The Nano carries the ATmega328P in TQFP/QFN, whose
                          ADC6/ADC7 channels reach the pad WITHOUT a digital
                          I/O buffer. Analog input only. The Uno's DIP package
                          does not bring them out at all -- so the same two
                          names are a different error on each board.
  ATmega328P     D5, D6   Timer 0, which is the millisecond tick every
                          generated program is scheduled on. Handing them to
                          PWM would stop time.
  micro:bit  P3,4,6,7,    Wired to the 5x5 LED matrix, which the display
             9,10         driver scans continuously. Not refused -- usable,
                          once `display.off()` has been emitted, which is
                          checked here rather than assumed.
  micro:bit  BUTTON_A/B   A button is an input and cannot be anything else.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import stc_pseudocode as ps                                    # noqa: E402

checks = failures = 0


def ok(cond, label, detail=""):
    global checks, failures
    checks += 1
    if not cond:
        failures += 1
    mark = "\x1b[32mok \x1b[0m" if cond else "\x1b[31mFAIL\x1b[0m"
    print(f"  {mark} {label}" + (f"   {detail}" if detail else ""))


def parse(device, body, head=""):
    return ps.parse(f"DEVICE {device}:\n{head}{body}"
                    "  WHEN started:\n    FOREVER:\n      wait 10 ms\n")


def accepts(device, decl, label):
    try:
        parse(device, f"  PIN x = {decl}\n")
        ok(True, label)
    except ps.PseudocodeError as exc:
        ok(False, label, f"refused: {exc}")


def refuses(device, decl, *fragments, label):
    try:
        parse(device, f"  PIN x = {decl}\n")
        ok(False, label, "ACCEPTED -- the board cannot do this")
    except ps.PseudocodeError as exc:
        message = str(exc)
        missing = [f for f in fragments if f not in message]
        ok(not missing, label,
           f"missing {missing!r} in: {message}" if missing
           else f'"{message.split(":", 1)[-1].strip()[:58]}"')


# --- 1. the Nano's analog-only pair ----------------------------------------
print("the Nano's A6/A7: present, and not digital")
for pin in ("A6", "A7"):
    for direction in ("OUTPUT", "INPUT"):
        refuses("ARDUINO-NANO", f"{pin} {direction}",
                pin, "analog", "Arduino Nano",
                label=f"Nano {pin} {direction} is refused, naming the board")
    accepts("ARDUINO-NANO", f"{pin} ANALOG",
            f"Nano {pin} ANALOG is the one thing it CAN do")

# The refusal must not have been bought by breaking the pins next to it.
for pin in ("A0", "A5"):
    accepts("ARDUINO-NANO", f"{pin} OUTPUT", f"Nano {pin} OUTPUT still works")
    accepts("ARDUINO-NANO", f"{pin} ANALOG", f"Nano {pin} ANALOG still works")
accepts("ARDUINO-NANO", "D13 OUTPUT", "Nano D13 OUTPUT still works")

# And the accepted analog form has to reach the generated code, or the
# refusal above is just a parser that says no in a nicer way.
code = ps.emit(ps.parse(
    "DEVICE ARDUINO-NANO:\n  PIN pot = A6 ANALOG\n"
    "  WHEN started:\n    FOREVER:\n      print pot\n      wait 10 ms\n"))
ok("analogRead(A6)" in code, "Nano A6 ANALOG emits analogRead(A6)")

# --- 2. the same two names, a different error on the Uno -------------------
print("\nthe Uno's DIP package does not have them at all")
for pin in ("A6", "A7"):
    refuses("ARDUINO-UNO", f"{pin} ANALOG", "A0-A5", "Arduino Uno",
            label=f"Uno {pin} ANALOG is refused as ABSENT, not as analog-only")

# The distinction is the point: a reader who sees the Nano's message goes to
# the package, one who sees the Uno's goes to the schematic. Same two pin
# names, two boards, two different places to look.
nano = uno = ""
try:
    parse("ARDUINO-NANO", "  PIN x = A6 OUTPUT\n")
except ps.PseudocodeError as exc:
    nano = str(exc)
try:
    parse("ARDUINO-UNO", "  PIN x = A6 OUTPUT\n")
except ps.PseudocodeError as exc:
    uno = str(exc)
ok(nano != uno and nano and uno,
   "the two A6 refusals do not say the same thing")

# --- 3. beyond the end of each board ---------------------------------------
print("\npast the last pin, on every board")
for device, decl, fragment in [
        ("ARDUINO-UNO",   "D14 OUTPUT",  "D0-D13"),
        ("ARDUINO-NANO",  "D14 OUTPUT",  "D0-D13"),
        ("ARDUINO-NANO",  "A8 ANALOG",   "A0-A7"),
        ("MICROBIT",      "P21 OUTPUT",  "P0-P20"),
        ("PICO",          "GP29 OUTPUT", "GP0-GP28"),
        ("ATMEGA328P",    "D19 OUTPUT",  "D0-D13")]:
    refuses(device, decl, fragment, label=f"{device} {decl}")

# --- 4. the AVR's timer 0 pins ---------------------------------------------
print("\nTimer 0 drives the millisecond tick, so its pins are not PWM")
for pin in ("D5", "D6"):
    refuses("ATMEGA328P", f"{pin} PWM", label=f"ATmega328P {pin} PWM is refused")
    accepts("ATMEGA328P", f"{pin} OUTPUT",
            f"ATmega328P {pin} OUTPUT is fine -- only PWM collides")

# --- 5. the micro:bit's shared pins: allowed, but the display is turned off -
print("\nthe micro:bit's matrix pins are shared, not forbidden")
DISPLAY_PINS = (3, 4, 6, 7, 9, 10)
for number in DISPLAY_PINS:
    accepts("MICROBIT", f"P{number} OUTPUT", f"micro:bit P{number} is allowed")

code = ps.emit(ps.parse(
    "DEVICE MICROBIT:\n  PIN led = P3 OUTPUT\n"
    "  WHEN started:\n    FOREVER:\n      toggle led\n      wait 10 ms\n"))
ok("display.off()" in code,
   "...and a program that uses one emits display.off()")

code = ps.emit(ps.parse(
    "DEVICE MICROBIT:\n  PIN led = P0 OUTPUT\n"
    "  WHEN started:\n    FOREVER:\n      toggle led\n      wait 10 ms\n"))
ok("display.off()" not in code,
   "...and one that does not, does not -- the display still works")

print("\nthe micro:bit's buttons are inputs and nothing else")
for direction in ("OUTPUT", "PWM"):
    refuses("MICROBIT", f"BUTTON_A {direction}", "button",
            label=f"micro:bit BUTTON_A {direction} is refused")
accepts("MICROBIT", "BUTTON_A INPUT", "micro:bit BUTTON_A INPUT is fine")

print("\nthe micro:bit's ADC is a subset of its pins")
refuses("MICROBIT", "P5 ANALOG", "no ADC",
        label="micro:bit P5 ANALOG is refused, naming the ADC pins")
accepts("MICROBIT", "P0 ANALOG", "micro:bit P0 ANALOG is fine")

print(f"\n  {checks - failures}/{checks} checks passed")
sys.exit(1 if failures else 0)
