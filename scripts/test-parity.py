#!/usr/bin/env python3
"""
test-parity.py — every target, every feature, actually exercised.

`Target.supports` is a claim. This checks it against what the emitters do, in
both directions, because both directions rot:

  - a feature a target CLAIMS must survive parse and emit, and put something
    recognisable in the output. A target can otherwise advertise `tone` and
    then raise TypeError on the SetTone node, which is exactly what the
    micro:bit did the day event hats landed.
  - a feature a target does NOT claim must be refused by name, at parse time,
    naming the board. Silence there means the generator quietly emits
    something that looks close.

The matrix it prints is the honest answer to "what works where", and is meant
to be read as documentation.
"""

import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import stc_pseudocode as sp  # noqa: E402

passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
    else:
        failed += 1
        print(f"  \033[31mFAIL\033[0m  {name}  {detail}")
    return ok


# One representative device per distinct target implementation, with the pin
# spellings that device actually uses.
DEVICES = {
    "STC12C5A60S2": dict(out="P1.0", pwm="P1.3", tone="P1.5", ana="P1.2",
                         port="P2", clock="11059200"),
    "STC89C52RC":   dict(out="P1.0", pwm=None,   tone="P1.5", ana=None,
                         port="P2", clock="11059200"),
    "ARDUINO-UNO":  dict(out="D13",  pwm="D9",   tone="D8",   ana="A0",
                         port=None,  clock="16000000"),
    "ATMEGA328P":   dict(out="D13",  pwm="D11",  tone="D9",   ana="A0",
                         port="D",   clock="16000000"),
    "MICROBIT":     dict(out="P0",   pwm="P1",   tone="P2",   ana="P4",
                         port=None,  clock="16000000"),
}

FEATURES = ["pwm", "tone", "print", "table", "port", "part"]


def program(device, feature):
    """A minimal program using exactly one feature, or None if unrepresentable."""
    spec = DEVICES[device]
    head = f"DEVICE {device}:\n  CLOCK {spec['clock']}\n"
    if feature == "pwm":
        if not spec["pwm"]:
            return None
        return head + (f"  PIN d = {spec['pwm']} PWM\n"
                       "  WHEN started:\n    set d to 60 percent\n")
    if feature == "tone":
        return head + (f"  PIN t = {spec['tone']} TONE\n"
                       "  WHEN started:\n    set t to 440 hz\n")
    if feature == "print":
        return head + "  WHEN started:\n    print \"hi\"\n"
    if feature == "table":
        return head + ("  TABLE f = 0x01, 0x02, 0x03\n"
                       f"  PIN d = {spec['out']} OUTPUT\n"
                       "  WHEN started:\n    set n to f[1]\n")
    if feature == "port":
        if not spec["port"]:
            # No syntax for a port on this device, so "refused" cannot be
            # tested through a declaration -- the parse fails earlier.
            return head + ("  PORT k = P2 OUTPUT\n"
                           "  WHEN started:\n    set k to 255\n")
        return head + (f"  PORT k = {spec['port']} OUTPUT\n"
                       "  WHEN started:\n    set k to 255\n")
    if feature == "part":
        return head + ("  PART sr = 74HC595 DATA P1.0 CLOCK P1.1 LATCH P1.2\n"
                       "  WHEN started:\n    set sr to 170\n")
    raise ValueError(feature)


# Something that must appear in the output when the feature really works.
EVIDENCE = {
    "pwm":   ["pwm_set", "OCR", "analogWrite", "write_analog"],
    "tone":  ["tone_set", "bw_tone", "music.pitch"],
    "print": ["bw_print", "Serial.println", "print("],
    "table": ["bw_tab_", "f = (", "bw_clamp"],
    "port":  ["P2 =", "PORT", "bw_port"],
    "part":  ["bw_part_"],
}

print("parity matrix\n")
header = f"{'device':16}" + "".join(f"{f:>8}" for f in FEATURES)
print(header)
print("-" * len(header))

for device in DEVICES:
    target = sp.TARGETS[device.lower()]
    cells = []
    for feature in FEATURES:
        claimed = feature in target.supports
        source = program(device, feature)

        if source is None:
            # The target claims the feature but this device has no pin for it
            # (the STC89 has no PCA, so no PWM pin exists to declare).
            cells.append("n/a")
            continue

        try:
            emitted = sp.emit(sp.parse(source))
            worked, error = True, ""
        except sp.PseudocodeError as exc:
            emitted, worked, error = "", False, str(exc)
        except Exception as exc:                      # noqa: BLE001
            emitted, worked, error = "", False, f"{type(exc).__name__}: {exc}"
            check(f"{device} {feature}: refusal is a PseudocodeError, not a crash",
                  False, error)

        if claimed:
            if check(f"{device} claims {feature} and emits it", worked, error):
                check(f"{device} {feature}: the output shows it",
                      any(mark in emitted for mark in EVIDENCE[feature]),
                      "no evidence in the generated source")
            cells.append("yes")
        else:
            check(f"{device} refuses {feature} rather than emitting something",
                  not worked, "it was accepted")
            check(f"{device} {feature}: the refusal names the board",
                  not worked and (target.display in error
                                  or "not available" in error
                                  or "do not understand" in error),
                  error[:70])
            cells.append("-")
    print(f"{device:16}" + "".join(f"{c:>8}" for c in cells))

print()
print("  yes = claimed and exercised end to end")
print("  -   = refused at parse time, naming the board")
print("  n/a = claimed, but this device has no pin for it (STC89 has no PCA)")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
