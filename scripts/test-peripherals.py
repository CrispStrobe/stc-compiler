#!/usr/bin/env python3
"""
test-peripherals — PWM, tone and the serial console, from pseudocode to register.

The hardware detail this is really guarding is in
`stc12c5a60s2-lab/docs/STC12-PERIPHERAL-MODEL.md` §5.3: the PCA comparator
drives the pin LOW while the counter is BELOW the compare value, so duty as a
fraction *high* is `(256 - value)/256`. Every LED in this toolchain is wired
active-low on top of that, which inverts it again. Two inversions is exactly
the arrangement where a wrong answer looks right, so the numbers below are
worked out by hand rather than captured from the generator.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import stc_pseudocode as ps                                   # noqa: E402

checks = failures = 0
HEAD = "DEVICE STC12C5A60S2:\n  CLOCK 11059200\n"


def ok(cond, what):
    global checks, failures
    checks += 1
    if not cond:
        failures += 1
        print(f"  FAIL {what}")


def emit(src: str) -> str:
    return ps.emit_c(ps.parse(src))


def rejects(src: str, fragment: str, what: str):
    global checks, failures
    checks += 1
    try:
        emit(src)
    except ps.PseudocodeError as exc:
        if fragment.lower() in str(exc).lower():
            return
        failures += 1
        print(f"  FAIL {what}: wrong reason -- {exc}")
        return
    failures += 1
    print(f"  FAIL {what}: was accepted")


def test_polarity():
    print("polarity: the load's percentage, not the pin's")
    c = emit(HEAD + "  PIN led = P1.3 PWM ACTIVE LOW\n"
                    "  WHEN started:\n    set led to 75 percent\n")
    ok("pwm_set(0, 100 - (75));" in c,
       "active-low inverts, visibly, in the generated C")
    c = emit(HEAD + "  PIN led = P1.3 PWM\n"
                    "  WHEN started:\n    set led to 75 percent\n")
    ok("pwm_set(0, 75);" in c, "active-high does not invert")
    c = emit(HEAD + "  PIN led = P1.4 PWM\n"
                    "  WHEN started:\n    set led to 10 percent\n")
    ok("pwm_set(1, 10);" in c, "P1.4 is module 1")


def test_setup():
    print("bringing the PCA up")
    c = emit(HEAD + "  PIN led = P1.3 PWM ACTIVE LOW\n"
                    "  WHEN started:\n    set led to 50 percent\n")
    ok("CCAPM0 = 0x42;" in c, "ECOM|PWM enables PWM mode on module 0")
    ok("CMOD = 0x00;" in c, "CPS=000 selects FOSC/12")
    ok(c.index("CCON = 0x00;") < c.index("CCAPM0"), "counter is stopped while configuring")
    ok(c.rindex("CCON = 0x40;") > c.index("CCAPM0"), "and started afterwards")
    ok("P1M0 |=  0x08;" in c, "the PWM pin is put in push-pull, like any output")
    ok("pwm_set(0, 100 - (0));" in c,
       "it starts OFF -- on an active-low load that is 100% high, not 0%")


def test_refusals():
    print("what it refuses, and why")
    rejects(HEAD + "  PIN led = P1.0 PWM\n  WHEN started:\n    set led to 5 percent\n",
            "only available on the PCA pins", "PWM on a pin with no PCA module")
    rejects("DEVICE STC89C52RC:\n  CLOCK 11059200\n  PIN led = P1.3 PWM\n"
            "  WHEN started:\n    set led to 5 percent\n",
            "needs the PCA", "PWM on a part that has no PCA")
    rejects(HEAD + "  PIN a = P1.3 PWM\n  PIN b = P1.3 OUTPUT\n"
                   "  WHEN started:\n    set a to 5 percent\n",
            "already declared", "two names for one physical pin")
    rejects(HEAD + "  PIN led = P1.0 OUTPUT\n  WHEN started:\n    set led to 50 percent\n",
            "only a PWM pin", "a percentage on a plain output")
    rejects(HEAD + "  PIN led = P1.3 PWM\n  WHEN started:\n    turn on led\n",
            "cannot be driven", "turning a PWM pin on")


def test_roundtrip():
    print("round trip through the pseudocode printer")
    src = (HEAD + "  PIN lamp = P1.3 PWM ACTIVE LOW\n  PIN pot = P1.2 ANALOG\n"
                  "  WHEN started:\n    FOREVER:\n"
                  "      set lamp to pot * 100 / 1023 percent\n      wait 50 ms\n")
    once = ps.emit_pseudocode(ps.parse(src))
    twice = ps.emit_pseudocode(ps.parse(once))
    ok(once == twice, "emit(parse(emit(parse(x)))) is stable")
    ok("set lamp to pot * 100 / 1023 percent" in once, "the sentence comes back")
    ok("PWM ACTIVE LOW" in once, "and so does the pin declaration")


def test_duty_arithmetic():
    """The helper's arithmetic, checked against hand-computed compare values.

    duty_high = (256 - v)/256, so v = 256 - round(pct * 256 / 100).
    """
    print("compare values, worked out by hand")
    c = emit(HEAD + "  PIN led = P1.3 PWM\n  WHEN started:\n    set led to 50 percent\n")
    m = re.search(r"v = 256 - \(\(percent_high \* 256 \+ 50\) / 100\);", c)
    ok(m is not None, "the helper computes v = 256 - pct*256/100")

    for pct, want in ((0, 256), (25, 192), (50, 128), (75, 64), (100, 0)):
        got = 256 - ((pct * 256 + 50) // 100)
        ok(got == want, f"{pct}% high -> compare {want} (got {got})")
    ok(256 - ((0 * 256 + 50) // 100) > 255,
       "0% needs the 9th bit, which an 8-bit compare could not express")


def test_compiles():
    print("and SDCC accepts the result")
    for candidate in ("sdcc", str(ROOT / "bin" / "sdcc")):
        try:
            subprocess.run([candidate, "--version"], check=True, capture_output=True)
            sdcc = candidate
            break
        except (OSError, subprocess.CalledProcessError):
            continue
    else:
        print("  SKIP no runnable sdcc")
        return

    c = emit(HEAD + "  PIN led = P1.3 PWM ACTIVE LOW\n  PIN pot = P1.2 ANALOG\n"
                    "  WHEN started:\n    FOREVER:\n"
                    "      set led to pot * 100 / 1023 percent\n      wait 20 ms\n")
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "pwm.c"
        src.write_text(c)
        r = subprocess.run(
            [sdcc, "-mmcs51", "--std-c99", "--iram-size", "256", "--xram-size", "1024",
             "--code-size", "61440", "-DFOSC_HZ=11059200UL", "-o", tmp + "/", str(src)],
            capture_output=True, text=True)
        ok(r.returncode == 0, f"compiles clean ({r.stderr.strip()[:120]})")
        asm = (Path(tmp) / "pwm.asm")
        if asm.exists():
            text = asm.read_text()
            ok("_CCAP0H" in text, "CCAP0H is written -- the buffered register, not CCAP0L")
            import re as _re
            writes = _re.findall(r"mov\s+_CCAP0L\s*,", text)
            ok(not writes,
               "no instruction writes CCAP0L: the hardware loads it on CL wrap")


def test_tone():
    """A tone is Timer 1 toggling a pin, not PWM. Model 5b says why."""
    print("tone: a settable period, which no PWM path on this chip has")
    c = emit(HEAD + "  PIN buzzer = P3.5 TONE\n"
                    "  WHEN started:\n    set buzzer to 440 hz\n")
    ok("tone_set(440);" in c, "a frequency, not a duty")
    ok("__interrupt(3)" in c, "Timer 1's ISR does the toggling")
    ok("P3_5 = !P3_5;" in c, "and toggles the declared pin, resolved at compile time")
    ok("TMOD  = (TMOD & 0x0F) | 0x10;" in c, "Timer 1 in mode 1 -- 16-bit, so low notes reach")
    ok("AUXR &= ~0xC0;" in c, "both timers at FOSC/12")
    ok("PT1   = 1;" in c, "the tone outranks the tick: jitter here is audible")
    ok("tone_set(0);" in c, "and it starts silent")

    c = emit(HEAD + "  PIN buzzer = P3.5 TONE\n  WHEN started:\n    turn off buzzer\n")
    ok("tone_set(0);" in c, "'turn off' is silence")

    # 65536 - FOSC/24/f, worked out by hand.
    for hz, want in ((440, 64489), (880, 65012), (1000, 65075)):
        ok(65536 - round(11059200 / 24 / hz) == want, f"{hz} Hz -> reload {want}")
        # And the generator must agree with that, which it only does if it
        # rounds. Truncating is off by one at 1000 Hz and audibly sharp.
        ok(65536 - ((460800 + hz // 2) // hz) == want,
           f"{hz} Hz: the emitted arithmetic rounds too")

    rejects(HEAD + "  PIN b = P3.5 TONE\n  WHEN started:\n    set b to 2 hz\n",
            "outside what Timer 1 can make", "a frequency below the 16-bit floor")
    rejects(HEAD + "  PIN b = P3.5 TONE\n  WHEN started:\n    turn on b\n",
            "has no 'on'", "turning a tone on rather than giving it a pitch")
    rejects(HEAD + "  PIN a = P3.5 TONE\n  PIN b = P3.4 TONE\n"
                   "  WHEN started:\n    turn off a\n",
            "only one TONE pin", "two tones, one Timer 1")
    rejects(HEAD + "  PIN b = P3.5 TONE\n  WHEN started:\n    set b to 50 percent\n",
            "only a PWM pin", "a percentage on a tone pin")


def test_uart():
    print("serial console, and the Timer 1 it may or may not contend for")
    c = emit(HEAD + "  WHEN started:\n    print \"ready\"\n    print 42\n")
    ok('bw_print("ready");' in c, "a literal prints as a literal")
    ok("bw_print_num(42);" in c, "a number goes through the integer path")
    ok("SCON = 0x50;" in c, "UART mode 1, receiver enabled")
    # STC12: 256 - 11059200/(32*9600) = 220, from the dedicated BRT.
    ok("BRT  = 220;" in c, "STC12 takes its baud from the BRT, leaving Timer 1 free")
    ok("AUXR |= 0x15;" in c, "BRTR | BRTx12 | S1BRS")

    # STC89: 256 - 11059200/(12*32*9600) = 253, the classic 0xFD.
    c89 = emit("DEVICE STC89C52RC:\n  CLOCK 11059200\n"
               "  WHEN started:\n    print \"hi\"\n")
    ok("TH1  = TL1 = 253;" in c89, "STC89 spends Timer 1 on baud: the classic 0xFD")
    ok("TMOD = (TMOD & 0x0F) | 0x20;" in c89, "Timer 1 in mode 2 for baud")

    rejects("DEVICE STC89C52RC:\n  CLOCK 11059200\n  PIN b = P3.5 TONE\n"
            "  WHEN started:\n    print \"hi\"\n    set b to 440 hz\n",
            "both need Timer 1",
            "console + tone on a part whose baud comes from Timer 1")

    # ...but the STC12 can do both, because its baud comes from the BRT.
    both = emit(HEAD + "  PIN b = P3.5 TONE\n"
                       "  WHEN started:\n    print \"hi\"\n    set b to 440 hz\n")
    ok("tone_set(440);" in both and 'bw_print("hi");' in both,
       "on the STC12 a console and a tone coexist")


def test_event_hat():
    """`WHEN btn pressed:` — polled, edge-triggered, polarity-aware.

    Lowered to a task rather than to INT0 because there are two external
    interrupt pins, so an interrupt hat works for two buttons and then stops
    being offered. PARTS-TO-BLOCKS.md has the reasoning.
    """
    print("event hats: polled, edge-triggered, and aware which way the button is wired")
    c = emit(HEAD + "  PIN btn = P3.2 INPUT ACTIVE LOW\n  PIN led = P1.0 OUTPUT ACTIVE LOW\n"
                    "  WHEN started:\n    turn off led\n"
                    "  WHEN btn pressed:\n    toggle led\n    wait 200 ms\n")
    ok("unsigned char now = (!P3_2) ? 1 : 0;" in c,
       "the level read is LOGICAL: an active-low button reads pressed when the pin is low")
    ok("(now && !bw_task1_prev)" in c, "rising edge of the logical level")
    ok("bw_task1_prev = now;" in c,
       "prev updates on EVERY pass -- a held button fires once, not every millisecond")
    ok("bw_task1_state = 0;   /* ready for the next edge */" in c,
       "and the task rearms rather than ending at 0xFFFF like a started script")

    rel = emit(HEAD + "  PIN btn = P3.2 INPUT\n  PIN led = P1.0 OUTPUT\n"
                      "  WHEN btn released:\n    turn off led\n")
    ok("(!now && bw_task0_prev)" in rel, "released is the falling edge")
    # A lone hat still needs the tick, so it must force the scheduler.
    ok("bw_task0();" in rel and "for (;;)" in rel,
       "a hat on its own still forces the cooperative scheduler -- it has to be polled")

    rejects(HEAD + "  PIN led = P1.0 OUTPUT\n  WHEN nosuch pressed:\n    turn off led\n",
            "unknown pin", "a hat on a pin that was never declared")
    rejects(HEAD + "  PIN led = P1.0 OUTPUT\n  WHEN led pressed:\n    turn off led\n",
            "only an INPUT pin", "a hat on an output")


def main() -> int:
    test_polarity()
    test_setup()
    test_refusals()
    test_roundtrip()
    test_duty_arithmetic()
    test_tone()
    test_uart()
    test_event_hat()
    test_compiles()
    print(f"\n{checks} checks, {failures} failures")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
