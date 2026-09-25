"""test_avr_default_clock — hand-written AVR C is built for the part's clock.

CompileReq.fosc defaults to 11059200, the 8051's crystal, and the AVR route
used to pass it straight through: a C program sent for an ATmega328P without
a clock was compiled with -DF_CPU=11059200UL, so <util/delay.h> on a real
16 MHz Uno ran every delay 31% short. Now the part's own clock applies unless
the request names one.

The check is made by the COMPILER, not by the response: each program #errors
unless F_CPU is exactly the expected value. Reading the response's own
`f_cpu` would only prove the service agrees with itself.
"""
import asyncio

import pytest

import app


def build(code, target, **kw):
    return asyncio.run(app.compile_source(
        app.CompileReq(code=code, language="c", target=target, **kw)))


def expects(hz):
    return (f"#include <avr/io.h>\n#if F_CPU != {hz}UL\n#error F_CPU is not {hz}\n#endif\n"
            "int main(void) { for (;;); }\n")


@pytest.mark.parametrize("target,hz", [("atmega328p", 16000000), ("atmega168p", 16000000),
                                       ("atmega2560", 16000000), ("attiny85", 8000000),
                                       ("attiny88", 8000000)])
def test_no_fosc_means_the_parts_clock(target, hz):
    r = build(expects(hz), target)
    assert r["success"], r.get("error")
    assert r["f_cpu"] == hz


def test_the_old_default_would_have_been_refused():
    # The control: the same check against 11.0592 MHz fails, so the test above
    # separates the fixed behaviour from the old one.
    r = build(expects(16000000), "atmega328p", fosc=11059200)
    assert not r["success"] and "F_CPU is not 16000000" in r["error"]


def test_a_named_clock_is_honoured():
    r = build(expects(1000000), "attiny85", fosc=1000000)
    assert r["success"], r.get("error")
    assert r["f_cpu"] == 1000000


def test_source_that_sets_its_own_clock_wins():
    code = "#define F_CPU 12000000UL\n" + expects(12000000)
    r = build(code, "atmega328p")
    assert r["success"], r.get("error")
    assert r["f_cpu"] is None


def test_a_program_that_only_uses_f_cpu_still_gets_one():
    # `F_CPU` appearing in the source is not the source setting it. This one
    # only computes with it; it used to get no clock at all.
    code = ("#include <avr/io.h>\nstatic const unsigned long ticks = F_CPU / 1000UL;\n"
            "int main(void) { volatile unsigned long t = ticks; (void)t; for (;;); }\n")
    r = build(code, "atmega328p")
    assert r["success"], r.get("error")
    assert r["f_cpu"] == 16000000
