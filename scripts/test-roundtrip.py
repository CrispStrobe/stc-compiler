#!/usr/bin/env python3
"""
test-roundtrip.py — round-trip transparency for the pseudocode front end.

Modelled on sb3-creator's `test/transparency.test.mjs`, including the point it
makes about what a weaker version would miss: converging to *some* stable text
is not enough, because a degraded output ("everything collapsed into one
sprite") is a stable fixed point too. So every hop is compared against the
ORIGINAL, not merely against the previous hop.

Hops here are:

    pseudocode --parse--> AST --emit_pseudocode--> pseudocode  (ps)
    pseudocode --parse--> AST --emit_c--> C                    (c, one-way)

`c` is emit-only -- there is no C front end and there is not meant to be -- so
it cannot be a hop on its own. It is checked differently: the C emitted from
hop N must equal the C emitted from the original. If a hop loses or mutates
anything, the compiled program changes, and that is the failure that matters.
"""

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import stc_pseudocode as sp  # noqa: E402

PROGRAMS = {
    "blink": sp.EXAMPLE,
    "bare": """WHEN started:
  set n to 0
  FOREVER:
    change n by 1
""",
    "pins": """DEVICE STC12C5A60S2:
  CLOCK 12000000
  PIN a = P1.0 OUTPUT
  PIN b = P2.7 OUTPUT ACTIVE LOW
  PIN c = P3.3 INPUT
  PIN d = P3.4 INPUT ACTIVE LOW
  WHEN started:
    FOREVER:
      set a high
      turn on b
      toggle a
      IF c THEN:
        set a low
      ELSE:
        turn off b
      wait until d
""",
    "procedures": """DEVICE STC12C5A60S2:
  CLOCK 11059200
  PIN led = P1.7 OUTPUT ACTIVE LOW
  DEFINE pulse (ms):
    turn on led
    wait ms ms
    turn off led
    wait ms ms
  DEFINE burst (n) (ms):
    REPEAT n:
      pulse ms
  WHEN started:
    FOREVER:
      burst 3, 80
      burst 1, 400
""",
    "analog": """DEVICE STC12C5A60S2:
  CLOCK 22118400
  PIN led = P1.0 OUTPUT ACTIVE LOW
  PIN pot = P1.3 ANALOG
  WHEN started:
    FOREVER:
      set v to pot
      IF v < 100 THEN:
        set v to 100
      turn on led
      wait v ms
      turn off led
      wait v ms
""",
    # Precedence is where a naive re-emitter quietly changes meaning: if the
    # right operand is not parenthesised one level tighter, `a - (b - c)`
    # re-parses as `(a - b) - c`.
    "precedence": """WHEN started:
  set a to 7
  set b to 3
  set c to 2
  FOREVER:
    set d to a - (b - c)
    set e to (a + b) * (a - b)
    set f to a * b + a / b - a % b
    set g to a / (b * c)
    IF a > b AND (b > c OR a = c) THEN:
      change a by 1
    IF not (a = b) THEN:
      change b by -1
    WHILE a > 100:
      set a to a - 100
    REPEAT UNTIL a < 0:
      change a by -1
""",
    "nesting": """WHEN started:
  set n to 0
  FOREVER:
    REPEAT 3:
      REPEAT 4:
        IF n > 5 THEN:
          WHILE n > 0:
            change n by -1
        ELSE:
          change n by 1
    wait 1 ms
""",
    # Two scripts: the C back end switches to the cooperative scheduler
    # (Timer 0 tick + one state machine per WHEN block). The pseudocode side
    # must stay transparent regardless.
    "two-scripts": """DEVICE STC12C5A60S2:
  CLOCK 11059200
  PIN led1 = P1.0 OUTPUT ACTIVE LOW
  PIN led2 = P1.1 OUTPUT ACTIVE LOW
  PIN button = P3.2 INPUT ACTIVE LOW
  WHEN started:
    FOREVER:
      toggle led1
      wait 300 ms
  WHEN started:
    FOREVER:
      wait until button
      toggle led2
      REPEAT 4:
        wait 50 ms
      wait until not button
""",
    "three-scripts": """WHEN started:
  set beats to 0
  FOREVER:
    change beats by 1
    wait 100 ms
WHEN started:
  FOREVER:
    WHILE beats < 10:
      wait 20 ms
    set beats to 0
WHEN started:
  REPEAT 5:
    wait 1 seconds
  stop
""",
    # A 12T part: no PxM registers, no AUXR, Timer 0 math identical -- the
    # drop-in-socket scenario where software delays would run 6-12x fast is
    # exactly what the timer-based emission avoids.
    "stc89": """DEVICE STC89C52RC:
  CLOCK 11059200
  PIN led = P1.0 OUTPUT ACTIVE LOW
  WHEN started:
    FOREVER:
      toggle led
      wait 500 ms
""",
    "stc15": """DEVICE STC15F2K60S2:
  CLOCK 11059200
  PIN led = P1.0 OUTPUT ACTIVE LOW
  PIN pot = P1.3 ANALOG
  WHEN started:
    FOREVER:
      wait pot ms
      toggle led
""",

    # A target off the 8051 entirely: no registers, no runtime of our own, and
    # a 32-bit millis(). Straight-line, so the whole script lands in setup()
    # and loop() stays empty -- a script runs once, as it does in Scratch.
    "arduino": """DEVICE ARDUINO-UNO:
  CLOCK 16000000
  PIN led = D13 OUTPUT
  PIN btn = D2 INPUT ACTIVE LOW
  PIN pot = A0 ANALOG
  WHEN started:
    wait until btn
    REPEAT 4:
      toggle led
      wait pot ms
    IF pot > 512 THEN:
      turn on led
    ELSE:
      turn off led
""",

    # The same board again, this time as bare silicon: no Arduino core, ports
    # written directly, and a Timer-0 tick we set up ourselves. This is the
    # form the service can actually compile. `PB4` is deliberately spelled as
    # a port name and must come back canonicalised to its board label, D12.
    "avr": """DEVICE ATMEGA328P:
  CLOCK 16000000
  PIN slow = D13 OUTPUT
  PIN fast = PB4 OUTPUT ACTIVE LOW
  PIN btn = D2 INPUT ACTIVE LOW
  PIN pot = A0 ANALOG
  WHEN started:
    wait until btn
    REPEAT 3:
      toggle slow
      wait pot ms
    IF pot > 512 THEN:
      turn on fast
    ELSE:
      turn off fast
""",

    # A target off C entirely. MicroPython has no goto, so the cooperative
    # scheduler is generators rather than a Duff's device -- the case the
    # target interface was drawn to allow.
    "microbit": """DEVICE MICROBIT:
  CLOCK 16000000
  PIN led = P0 OUTPUT
  PIN spk = P1 OUTPUT ACTIVE LOW
  PIN btn = BUTTON_A INPUT
  PIN dial = P2 ANALOG
  WHEN started:
    FOREVER:
      toggle led
      wait dial ms
  WHEN started:
    set hits to 0
    FOREVER:
      wait until btn
      change hits by 1
      turn on spk
      wait 50 ms
      turn off spk
""",

    # NAME decides what the generated file is called. It round-trips like any
    # other header line, and the strict character set is not tidiness: the
    # string ends up in a Content-Disposition header and as a filename.
    "named": """DEVICE ARDUINO-UNO:
  NAME blink
  CLOCK 16000000
  PIN led = D13 OUTPUT
  WHEN started:
    FOREVER:
      toggle led
      wait 500 ms
""",

    # An event hat on a micro:bit button. The generated task polls for the
    # edge, which is the shape the C targets get too -- but as a generator.
    "microbit-hat": """DEVICE MICROBIT:
  CLOCK 16000000
  PIN btn = BUTTON_A INPUT
  PIN led = P0 OUTPUT
  WHEN started:
    FOREVER:
      wait 500 ms
  WHEN btn pressed:
    set hits to 0
    change hits by 1
    turn on led
""",

    "microbit-once": """DEVICE MICROBIT:
  CLOCK 16000000
  PIN led = P0 OUTPUT
  WHEN started:
    REPEAT 3:
      set led high
      wait 100 ms
      set led low
      wait 100 ms
""",

    "avr-scripts": """DEVICE ATMEGA328P:
  CLOCK 16000000
  PIN slow = D13 OUTPUT
  PIN fast = D12 OUTPUT ACTIVE LOW
  WHEN started:
    FOREVER:
      toggle slow
      wait 500 ms
  WHEN started:
    FOREVER:
      toggle fast
      wait 300 ms
""",

    # The same board with two scripts, which is where the timebase type
    # matters: these deadlines are `unsigned long` because millis() is, and
    # the wraparound compare has to widen with them.
    "arduino-scripts": """DEVICE ARDUINO-NANO:
  CLOCK 16000000
  PIN slow = D13 OUTPUT
  PIN fast = D12 OUTPUT ACTIVE LOW
  WHEN started:
    FOREVER:
      toggle slow
      wait 500 ms
  WHEN started:
    REPEAT 3:
      toggle fast
      wait 150 ms
    FOREVER:
      toggle fast
      wait 300 ms
""",
}

# Hop orders. Only one text hop exists today, so depth is what varies; the
# structure is kept so adding a Python or block hop later needs no rework.
ORDERS = [["ps"], ["ps", "ps"], ["ps", "ps", "ps"], ["ps"] * 5, ["ps"] * 8]

passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
    else:
        failed += 1
        print(f"  \033[31mFAIL\033[0m  {name}  {detail}")
    return ok


def hop(text, kind):
    if kind == "ps":
        return sp.emit_pseudocode(sp.parse(text))
    raise ValueError(kind)


print("round-trip transparency\n")
for name, source in PROGRAMS.items():
    original_ps = sp.emit_pseudocode(sp.parse(source))
    original_c = sp.emit(sp.parse(source))
    ok = True

    for order in ORDERS:
        text = source
        for step, kind in enumerate(order, 1):
            text = hop(text, kind)
            label = f"{name}: {'/'.join(order)} step {step}"
            if not check(f"{label} preserves the pseudocode", text == original_ps):
                ok = False
                for want, got in zip(original_ps.splitlines(), text.splitlines()):
                    if want != got:
                        print(f"        want {want!r}\n         got {got!r}")
                        break
                break
            if not check(f"{label} preserves the C", sp.emit(sp.parse(text)) == original_c):
                ok = False
                break
        if not ok:
            break

    # Parsing the canonical form must give back an identical AST, not merely
    # identical text -- text equality could hide a field the emitter ignores.
    if ok:
        check(f"{name}: AST survives the round-trip",
              sp.parse(original_ps) == sp.parse(source))

    mark = "\033[32mok \033[0m" if ok else "\033[31mBAD\033[0m"
    print(f"  {mark} {name:12} {len(original_ps.splitlines()):3} lines pseudocode, "
          f"{len(original_c.splitlines()):3} lines C")

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
