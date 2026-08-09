#!/usr/bin/env python3
"""
test-microbit.py — run the generated MicroPython, don't just look at it.

Every other target is checked by compiling its output: the AVR goldens go
through avr-gcc under -Werror, the 8051 ones through SDCC. MicroPython is
interpreted on the device, so there is no compiler to hold it to account and
"it looks right" would otherwise be the whole of the evidence.

So the generated program is executed here, against a stub `microbit` module,
with a clock that advances on every read and a budget that stops it. That
checks the things a syntax check cannot:

  - the cooperative scheduler actually interleaves, rather than the first
    generator running forever while the second never starts
  - a wait really suspends for about the right number of milliseconds
  - ACTIVE LOW comes out inverted at the pin, not at the pseudocode
  - `global` is declared wherever a script assigns a module-level variable,
    which MicroPython would otherwise turn into a silent local

The stub is deliberately dumb. It is not a micro:bit simulator; it is just
enough to let the control flow run and be observed.
"""

import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import stc_pseudocode as sp  # noqa: E402

passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  \033[32mok \033[0m {name} {detail}")
    else:
        failed += 1
        print(f"  \033[31mFAIL\033[0m {name} {detail}")
    return ok


class Budget(Exception):
    """Raised to stop a program whose whole point is to never terminate."""


def make_microbit(budget=40000, button_after=200):
    """A stub `microbit` module, plus the log of what the program did."""
    log = []
    state = {"clock": 0, "reads": 0}

    def running_time():
        state["reads"] += 1
        if state["reads"] > budget:
            raise Budget
        state["clock"] += 1          # 1 ms per observation
        return state["clock"]

    def sleep(ms):
        state["reads"] += 1
        if state["reads"] > budget:
            raise Budget
        state["clock"] += int(ms)

    class Pin:
        def __init__(self, number):
            self.number = number

        def write_digital(self, value):
            log.append(("write", self.number, int(value), state["clock"]))

        def write_analog(self, value):
            log.append(("analog", self.number, int(value)))

        def read_digital(self):
            return 0

        def read_analog(self):
            return 500

    class Button:
        def __init__(self, name):
            self.name = name

        def is_pressed(self):
            # Not pressed at first, so `wait until` genuinely has to yield
            # before it can proceed.
            return state["clock"] > button_after

    music = types.ModuleType("music")
    music.pitch = lambda hz, duration=-1, pin=None, wait=True: log.append(
        ("pitch", hz, getattr(pin, "number", None)))
    music.stop = lambda pin=None: log.append(("silence", getattr(pin, "number", None)))
    sys.modules["music"] = music

    module = types.ModuleType("microbit")
    for n in range(21):
        setattr(module, f"pin{n}", Pin(n))
    module.button_a = Button("a")
    module.button_b = Button("b")
    module.running_time = running_time
    module.sleep = sleep
    module.display = types.SimpleNamespace(off=lambda: log.append(("display_off",)),
                                           on=lambda: None)
    return module, log, state


def run(source, **kw):
    module, log, state = make_microbit(**kw)
    sys.modules["microbit"] = module
    namespace = {"__name__": "__main__"}
    try:
        exec(compile(source, "<generated>", "exec"), namespace)
    except Budget:
        pass
    finally:
        sys.modules.pop("microbit", None)
        sys.modules.pop("music", None)
    return log, state


TWO_SCRIPTS = """DEVICE MICROBIT:
  PIN led = P0 OUTPUT
  PIN spk = P1 OUTPUT ACTIVE LOW
  PIN btn = BUTTON_A INPUT

  WHEN started:
    FOREVER:
      toggle led
      wait 100 ms

  WHEN started:
    set hits to 0
    FOREVER:
      wait until btn
      change hits by 1
      turn on spk
      wait 50 ms
      turn off spk
"""

ONE_SCRIPT = """DEVICE MICROBIT:
  PIN led = P0 OUTPUT
  PIN dial = P2 ANALOG

  WHEN started:
    REPEAT 3:
      set led high
      wait dial ms
      set led low
      wait 10 ms
"""

DISPLAY_PIN = """DEVICE MICROBIT:
  PIN buzzer = P3 OUTPUT
  WHEN started:
    FOREVER:
      toggle buzzer
      wait 5 ms
"""

print("generated MicroPython, executed\n")

# --- it is at least valid Python -------------------------------------------
for name, src in (("two scripts", TWO_SCRIPTS), ("one script", ONE_SCRIPT),
                  ("display pin", DISPLAY_PIN)):
    code = sp.emit(sp.parse(src))
    try:
        compile(code, "<generated>", "exec")
        check(f"{name}: parses as Python", True)
    except SyntaxError as exc:
        check(f"{name}: parses as Python", False, f"{exc}")

# --- the scheduler actually interleaves ------------------------------------
log, state = run(sp.emit(sp.parse(TWO_SCRIPTS)))
writes = [entry for entry in log if entry[0] == "write"]
p0 = [w for w in writes if w[1] == 0]
p1 = [w for w in writes if w[1] == 1]

check("script 1 ran (P0 toggling)", len(p0) > 5, f"{len(p0)} writes")
check("script 2 ran (P1 driven)", len(p1) > 2, f"{len(p1)} writes")
check("the two scripts interleave, neither starves",
      len(p0) > 5 and len(p1) > 2
      and min(w[3] for w in p1) < max(w[3] for w in p0),
      "first P1 write at t=%d, last P0 at t=%d"
      % (min(w[3] for w in p1) if p1 else -1,
         max(w[3] for w in p0) if p0 else -1))

# ACTIVE LOW: `turn on spk` must reach the pin as 0.
check("ACTIVE LOW inverted at the pin, not in the pseudocode",
      any(w[2] == 0 for w in p1) and any(w[2] == 1 for w in p1),
      f"levels seen: {sorted({w[2] for w in p1})}")

# P0 is toggled, so consecutive writes must alternate.
levels = [w[2] for w in p0]
check("toggle alternates rather than repeating",
      all(a != b for a, b in zip(levels, levels[1:])),
      f"first few: {levels[:6]}")

# A 100 ms wait should take roughly 100 ms of stub clock between toggles.
gaps = [b[3] - a[3] for a, b in zip(p0, p0[1:])]
typical = sorted(gaps)[len(gaps) // 2] if gaps else 0
check("a 100 ms wait lasts about 100 ms", 60 <= typical <= 400,
      f"median gap {typical} ms")

# --- `global` really was declared ------------------------------------------
code = sp.emit(sp.parse(TWO_SCRIPTS))
check("the script assigning `hits` declares it global",
      "global hits" in code)

# --- single script, and the display-pin rule -------------------------------
log, _ = run(sp.emit(sp.parse(ONE_SCRIPT)))
writes = [w for w in log if w[0] == "write" and w[1] == 0]
# 1 + 6: every output pin is driven to its off state before the script runs,
# then REPEAT 3 writes high and low each time round.
check("single script runs to completion, once", len(writes) == 7,
      f"{len(writes)} writes (1 initial off + 3 x high/low)")
check("...and does not restart", [w[2] for w in writes] == [0, 1, 0, 1, 0, 1, 0],
      str([w[2] for w in writes]))

code = sp.emit(sp.parse(DISPLAY_PIN))
check("P3 turns the LED matrix off first", "display.off()" in code)
log, _ = run(code)
check("...and does it before driving the pin",
      log and log[0][0] == "display_off", str(log[:1]))

# --- PWM, tone, print and tables -------------------------------------------
print()
PERIPHERALS = """DEVICE MICROBIT:
  TABLE font = 0x00, 0x40, 0x64
  PIN buzz = P0 TONE
  PIN lamp = P1 PWM ACTIVE LOW
  WHEN started:
    print "ready"
    set i to 2
    set lamp to font[i] percent
    set buzz to 440 hz
    wait 10 ms
    set buzz to 0 hz
"""
code = sp.emit(sp.parse(PERIPHERALS))
try:
    compile(code, "<generated>", "exec")
    check("peripherals: parses as Python", True)
except SyntaxError as exc:
    check("peripherals: parses as Python", False, str(exc))

import io
import contextlib
buffer = io.StringIO()
with contextlib.redirect_stdout(buffer):
    log, _ = run(code)
printed = buffer.getvalue().split()

check("print reaches the console", "ready" in printed, str(printed))

analog = [entry for entry in log if entry[0] == "analog"]
# font[2] is 0x64 = 100, on an ACTIVE LOW load: the pin is high 0% of the time.
check("PWM duty inverts for an active-low load", analog == [("analog", 1, 0)],
      str(analog))

pitches = [entry for entry in log if entry[0] == "pitch"]
silences = [entry for entry in log if entry[0] == "silence"]
check("tone plays on its pin", pitches == [("pitch", 440, 0)], str(pitches))
check("0 Hz silences rather than playing 0", silences == [("silence", 0)],
      str(silences))
check("a table is indexed, not inlined", "font[i]" in code)
check("tables are tuples, not lists", "font = (0x00, 0x40, 0x64,)" in code)

# --- errors the target must refuse -----------------------------------------
print()
for decl, why in [
    ("PIN x = P21 OUTPUT", "P21 does not exist"),
    ("PIN x = P5 ANALOG", "P5 has no ADC"),
    ("PIN x = BUTTON_A OUTPUT", "a button cannot be an output"),
    ("PIN x = D13 OUTPUT", "Arduino spelling on a micro:bit"),
    ("PORT d = P0 OUTPUT", "a whole-port PORT has no micro:bit equivalent"),
]:
    source = f"DEVICE MICROBIT:\n  {decl}\n  WHEN started:\n    wait 1 ms\n"
    try:
        sp.parse(source)
        check(why, False, "was accepted")
    except sp.PseudocodeError as exc:
        check(why, True, f"-> {exc}")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
