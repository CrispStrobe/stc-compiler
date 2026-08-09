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

# --- event hats fire on the EDGE, not while held ---------------------------
print()
HAT = """DEVICE MICROBIT:
  PIN btn = BUTTON_A INPUT
  PIN led = P0 OUTPUT
  WHEN started:
    FOREVER:
      wait 50 ms
  WHEN btn pressed:
    set hits to 0
    change hits by 1
    turn on led
"""
code = sp.emit(sp.parse(HAT))
try:
    compile(code, "<generated>", "exec")
    check("event hat: parses as Python", True)
except SyntaxError as exc:
    check("event hat: parses as Python", False, str(exc))

check("event hat: polls its pin", "_prev" in code and "button_a.is_pressed()" in code)
check("event hat: `global` sits at function level, not inside the if",
      "\n    global hits" in code)

# The stub button goes not-pressed -> pressed exactly once, so a correct edge
# detector fires exactly once no matter how many times it is polled.
log, _ = run(code)
fires = [w for w in log if w[0] == "write" and w[1] == 0 and w[2] == 1]
check("event hat: fires once on the edge, not while held", len(fires) == 1,
      f"{len(fires)} firings")

# A script with no wait and no loop yields nowhere; without a bare yield the
# `def` is a plain function and the scheduler calls next() on its None.
NO_WAIT = """DEVICE MICROBIT:
  PIN led = P0 OUTPUT
  WHEN started:
    FOREVER:
      wait 50 ms
  WHEN started:
    turn on led
"""
code = sp.emit(sp.parse(NO_WAIT))
check("a script that never waits is still a generator",
      "yield   # runs once" in code)
log, _ = run(code)          # would raise TypeError if it were not
check("...and the scheduler runs it without dying",
      any(w[0] == "write" and w[1] == 0 and w[2] == 1 for w in log))

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

# --- the Pico: same lowering, a different runtime ------------------------
print()

def make_machine(budget=40000):
    """A stub `machine` + `time`, and the log of what the program did."""
    log = []
    state = {"clock": 0, "reads": 0}

    class Budgeted(Exception):
        pass

    def ticks_ms():
        state["reads"] += 1
        if state["reads"] > budget:
            raise Budget
        state["clock"] += 1
        return state["clock"]

    class Pin:
        OUT, IN, PULL_UP, PULL_DOWN = 1, 0, 2, 3

        def __init__(self, number, mode=None, pull=None):
            self.number = number
            self.level = 0
            log.append(("pin", number, mode))

        def value(self, level=None):
            if level is None:
                return self.level
            self.level = int(level)
            log.append(("write", self.number, self.level, state["clock"]))
            return None

    class ADC:
        def __init__(self, number):
            self.number = number
            log.append(("adc", number))

        def read_u16(self):
            return 32768                       # mid-scale; 512 after scaling

    class PWM:
        def __init__(self, pin):
            self.number = pin.number
            log.append(("pwm", pin.number))

        def freq(self, hz):
            log.append(("freq", self.number, hz))

        def duty_u16(self, duty):
            log.append(("duty", self.number, duty))

    machine = types.ModuleType("machine")
    machine.Pin, machine.ADC, machine.PWM = Pin, ADC, PWM
    timemod = types.ModuleType("time")
    timemod.ticks_ms = ticks_ms
    timemod.ticks_add = lambda a, b: a + b
    timemod.ticks_diff = lambda a, b: a - b
    timemod.sleep_ms = lambda ms: state.__setitem__("clock", state["clock"] + int(ms))
    sys.modules["machine"] = machine
    sys.modules["time"] = timemod
    return log, state


def run_pico(source, **kw):
    log, state = make_machine(**kw)
    real_time = sys.modules.get("time")
    try:
        exec(compile(source, "<generated>", "exec"), {"__name__": "__main__"})
    except Budget:
        pass
    finally:
        sys.modules.pop("machine", None)
        import importlib
        sys.modules["time"] = importlib.import_module("time") if real_time is None else real_time
    return log, state


# Two scripts, so the cooperative path runs: a single script would lower its
# waits to time.sleep_ms and never reach the deadline arithmetic that has to
# survive ticks_ms wrapping.
PICO = """DEVICE PICO:
  PIN led = GP25 OUTPUT
  PIN dim = GP15 PWM ACTIVE LOW
  PIN buzz = GP16 TONE
  PIN pot = GP26 ANALOG
  WHEN started:
    FOREVER:
      toggle led
      set dim to 60 percent
      wait pot ms
  WHEN started:
    FOREVER:
      set buzz to 440 hz
      wait 50 ms
"""

PICO_ONE_SCRIPT = """DEVICE PICO:
  PIN led = GP25 OUTPUT
  WHEN started:
    REPEAT 2:
      toggle led
      wait 10 ms
"""
code = sp.emit(sp.parse(PICO))
try:
    compile(code, "<generated>", "exec")
    check("pico: parses as Python", True)
except SyntaxError as exc:
    check("pico: parses as Python", False, str(exc))

check("pico: constructs its pin objects before use",
      "_pin25 = Pin(25, Pin.OUT)" in code and "_adc26 = ADC(26)" in code)
check("pico: the 16-bit ADC is scaled to the 0-1023 every other target reports",
      "read_u16() >> 6" in code)
check("pico: uses ticks_diff, because ticks_ms wraps",
      "time.ticks_diff" in code and "time.ticks_add" in code)
check("pico: a single script still lowers a wait to sleep_ms",
      "time.sleep_ms" in sp.emit(sp.parse(PICO_ONE_SCRIPT)))
check("pico: no _level dict -- value() reads the output latch back",
      "_level" not in code)

log, _ = run_pico(code)
writes = [e for e in log if e[0] == "write" and e[1] == 25]
duties = [e for e in log if e[0] == "duty" and e[1] == 15]
# Filtered by pin: the PWM pin also gets a freq() call, for its carrier.
freqs = [e for e in log if e[0] == "freq" and e[1] == 16]
check("pico: the LED toggles", len(writes) > 4, f"{len(writes)} writes")
check("pico: toggling alternates",
      all(a[2] != b[2] for a, b in zip(writes, writes[1:])),
      str([w[2] for w in writes[:6]]))
# 60% duty on an ACTIVE LOW load: the pin is high 40% of the time.
check("pico: PWM duty inverts for an active-low load",
      duties and duties[0][2] == (100 - 60) * 65535 // 100,
      str(duties[:1]))
check("pico: the PWM carrier is set once, separately from any tone",
      any(e[0] == "freq" and e[1] == 15 and e[2] == 1000 for e in log))
check("pico: a tone sets its own frequency and half duty",
      freqs and freqs[0][2] == 440
      and any(e[0] == "duty" and e[1] == 16 and e[2] == 32768 for e in log),
      str(freqs[:1]))

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
