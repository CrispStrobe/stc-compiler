#!/usr/bin/env python3
"""
test-tables-ports — whole-port I/O, lookup tables, and the parts library.

These are the two features `stc12c5a60s2-lab/docs/DIALECT-COVERAGE.md` measured
as blocking five of sixteen demos in an outside corpus, and nothing else. The
acceptance test is therefore not a unit test at all: it is whether
`02_7_segment` from that corpus can be said in the dialect at all, and whether
the font comes out byte-identical.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import stc_pseudocode as ps                                    # noqa: E402

checks = failures = 0
HEAD = "DEVICE STC12C5A60S2:\n  CLOCK 11059200\n"

# The font from treideme/stc89c52-demos 02_7_segment (Apache-2.0), transcribed
# from its binary literals. If the dialect cannot reproduce these sixteen bytes
# exactly, it cannot drive a seven-segment display.
DEMO_FONT = [0x3F, 0x06, 0x5B, 0x4F, 0x66, 0x6D, 0x7D, 0x07,
             0x7F, 0x6F, 0x77, 0x7C, 0x39, 0x5E, 0x79, 0x71]


def ok(cond, what):
    global checks, failures
    checks += 1
    if not cond:
        failures += 1
        print(f"  FAIL {what}")


def rejects(src, fragment, what):
    global checks, failures
    checks += 1
    try:
        ps.emit_c(ps.parse(src))
    except ps.PseudocodeError as exc:
        if fragment.lower() not in str(exc).lower():
            failures += 1
            print(f"  FAIL {what}: wrong reason -- {exc}")
        return
    failures += 1
    print(f"  FAIL {what}: was accepted")


def test_the_demo():
    print("02_7_segment, said in the dialect")
    font = ", ".join(f"0b{v:08b}" for v in DEMO_FONT)
    src = (HEAD + f"  TABLE font = {font}\n  PORT segments = P0 OUTPUT\n"
                  "  WHEN started:\n    FOREVER:\n      set i to 0\n"
                  "      REPEAT 16:\n        set segments to font[i] + 128\n"
                  "        wait 400 ms\n        set segments to 0\n"
                  "        change i by 1\n")
    prog = ps.parse(src)
    ok(list(prog.tables["font"]) == DEMO_FONT,
       "the font survives parsing byte for byte, written in binary")

    c = ps.emit_c(prog)
    ok("static const __code unsigned char bw_tab_font[]" in c,
       "the table is const and in code space, not in the 256 bytes of RAM")
    ok(", ".join(f"0x{v:02X}" for v in DEMO_FONT) in c,
       "and reaches the C as the same sixteen bytes")
    ok("P0 = (unsigned char)(bw_tab_font[bw_clamp(i, 15)] + 128);" in c,
       "one store to the whole port, with the computed index clamped")
    ok("P0M0 |=  0xFF;" in c, "all eight bits of the port go push-pull")

    once = ps.emit_pseudocode(prog)
    ok(ps.emit_pseudocode(ps.parse(once)) == once, "round trip is stable")
    ok("TABLE font = 0x3F, 0x06," in once, "the table comes back")
    ok("PORT segments = P0 OUTPUT" in once, "and so does the port")
    ok("set segments to font[i] + 128" in once, "and the statement reads the same")


def test_indexing():
    print("indexing, and the failure a display would show as data")
    c = ps.emit_c(ps.parse(HEAD + "  TABLE t = 10, 20, 30\n  PORT d = P0 OUTPUT\n"
                                  "  WHEN started:\n    set d to t[2]\n"))
    ok("bw_tab_t[2]" in c, "a constant index resolves to a plain subscript")
    ok("bw_clamp(" not in c.replace("static unsigned char bw_clamp(", ""),
       "and emits no clamp CALL -- it costs nothing at run time")
    rejects(HEAD + "  TABLE t = 1, 2, 3\n  PORT d = P0 OUTPUT\n"
                   "  WHEN started:\n    set d to t[9]\n",
            "outside the table", "a constant index past the end")
    rejects(HEAD + "  TABLE t = 1, 2\n  WHEN started:\n    print t\n",
            "read it as", "a table used without an index")
    rejects(HEAD + "  TABLE t = 1, x, 3\n  WHEN started:\n    print 1\n",
            "not a constant", "a table entry that is not a constant")
    rejects(HEAD + "  TABLE t = 1, 300\n  WHEN started:\n    print 1\n",
            "holds bytes", "a table entry wider than a byte")


def test_ports():
    print("ports, and the overlaps that would fight")
    c = ps.emit_c(ps.parse(HEAD + "  PORT k = P2 INPUT ACTIVE LOW\n  PORT d = P0 OUTPUT\n"
                                  "  WHEN started:\n    FOREVER:\n      set d to k\n"))
    ok("P0 = (unsigned char)((unsigned char)~P2);" in c,
       "an active-low port inverts on read, once, visibly")

    rejects(HEAD + "  PIN a = P0.3 OUTPUT\n  PORT d = P0 OUTPUT\n"
                   "  WHEN started:\n    set d to 1\n",
            "one bit at a time", "a PORT over a pin already declared inside it")
    rejects(HEAD + "  PORT d = P0 OUTPUT\n  PIN a = P0.3 OUTPUT\n"
                   "  WHEN started:\n    set d to 1\n",
            "whole port", "a PIN inside a port already declared whole")
    rejects(HEAD + "  PORT d = P0 OUTPUT\n  PORT e = P0 OUTPUT\n"
                   "  WHEN started:\n    set d to 1\n",
            "already declared", "the same port declared twice")
    rejects(HEAD + "  PORT k = P2 INPUT\n  WHEN started:\n    set k to 1\n",
            "cannot be written", "writing an input port")


def test_parts():
    """A 74HC595 -- the one corpus part whose timing cannot be got wrong.

    PARTS-MODEL.md admits a driver when correctness depends on the ORDER of
    edges rather than their duration. This part is specified into the tens of
    megahertz, so there is no delay to get wrong; the demo's NOP()s are
    conservative padding, not a requirement. That is why it can be written
    correctly without a bench, and why 1-Wire cannot.
    """
    print("74HC595: eight outputs for three pins")
    src = (HEAD + "  TABLE font = 0x3F, 0x06, 0x5B\n"
           "  PART display = 74HC595 DATA P3.4 CLOCK P3.6 LATCH P3.5 ACTIVE LOW\n"
           "  WHEN started:\n    set d to 0\n    FOREVER:\n"
           "      set display to font[d]\n      wait 500 ms\n")
    prog = ps.parse(src)
    c = ps.emit_c(prog)
    ok("bw_part_display((unsigned char)~(bw_tab_font[bw_clamp(d, 2)]));" in c,
       "a PART takes the same `set ... to ...` a PORT takes, polarity included")
    ok("P3_4 = (value & 0x80) ? 1 : 0;" in c, "MSB first, so the byte reads left to right")
    ok(c.index("P3_6 = 1;") < c.index("P3_5 = 1;"),
       "all eight bits are shifted before the latch transfers them")
    ok("P3M0 |=  0x70;" in c, "its three pins are outputs: P3.4, P3.5, P3.6")
    # No delay anywhere in the driver: that is the admission criterion, not an
    # oversight, so assert it rather than leaving it to be "fixed" later.
    driver = c[c.index("bw_part_display(unsigned char"):]
    driver = driver[:driver.index("\n}")]
    ok("delay" not in driver and "NOP" not in driver and "_nop_" not in driver,
       "and the driver contains NO delay -- order-dependent, not duration-dependent")

    once = ps.emit_pseudocode(prog)
    ok(ps.emit_pseudocode(ps.parse(once)) == once, "round trip is stable")
    ok("PART display = 74HC595 DATA P3.4 CLOCK P3.6 LATCH P3.5 ACTIVE LOW" in once,
       "the declaration comes back verbatim")

    print("  and its pins are claimed")
    rejects(HEAD + "  PART x = 74HC595 DATA P3.4 CLOCK P3.6 LATCH P3.5\n"
                   "  PIN a = P3.4 OUTPUT\n  WHEN started:\n    set x to 1\n",
            "claimed by the part", "a PIN on a pin the part claimed")
    rejects(HEAD + "  PIN a = P3.4 OUTPUT\n"
                   "  PART x = 74HC595 DATA P3.4 CLOCK P3.6 LATCH P3.5\n"
                   "  WHEN started:\n    set x to 1\n",
            "a PART claims its pins", "a PART over an already declared pin")
    rejects(HEAD + "  PART x = 74HC595 DATA P3.4 CLOCK P3.4 LATCH P3.5\n"
                   "  WHEN started:\n    set x to 1\n",
            "same pin twice", "data and clock on one pin")
    rejects(HEAD + "  PORT p = P3 OUTPUT\n"
                   "  PART x = 74HC595 DATA P3.4 CLOCK P3.6 LATCH P3.5\n"
                   "  WHEN started:\n    set x to 1\n",
            "inside the whole port", "a PART inside a declared PORT")
    rejects(HEAD + "  PART x = 74HC595 DATA P3.4 CLOCK P3.6 LATCH P3.5\n"
                   "  PART y = 74HC595 DATA P3.4 CLOCK P3.7 LATCH P3.3\n"
                   "  WHEN started:\n    set x to 1\n",
            "already claimed", "two parts sharing a pin")


def test_literals():
    print("number bases, because a font written in decimal is a font written wrong")
    prog = ps.parse(HEAD + "  TABLE t = 0b00111111, 0x3F, 63\n  WHEN started:\n    print 1\n")
    ok(prog.tables["t"] == [0x3F, 0x3F, 0x3F], "binary, hex and decimal agree")


def test_compiles():
    print("and SDCC accepts it")
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

    font = ", ".join(f"0b{v:08b}" for v in DEMO_FONT)
    c = ps.emit_c(ps.parse(
        HEAD + f"  TABLE font = {font}\n  PORT segments = P0 OUTPUT\n"
               "  WHEN started:\n    FOREVER:\n      set i to 0\n"
               "      REPEAT 16:\n        set segments to font[i]\n"
               "        wait 400 ms\n        change i by 1\n"))
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "seg.c"
        src.write_text(c)
        r = subprocess.run(
            [sdcc, "-mmcs51", "--std-c99", "--iram-size", "256", "--xram-size", "1024",
             "--code-size", "61440", "-DFOSC_HZ=11059200UL", "-o", tmp + "/", str(src)],
            capture_output=True, text=True)
        ok(r.returncode == 0, f"compiles clean ({r.stderr.strip()[:120]})")


def main() -> int:
    test_the_demo()
    test_indexing()
    test_ports()
    test_parts()
    test_literals()
    test_compiles()
    print(f"\n{checks} checks, {failures} failures")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
