"""test_assemble — smoke tests for POST /assemble.

Per toolchain: a valid blink assembles with listing; a syntax error
yields a line-accurate error.
"""
import unittest

import assemble

# ---- 8051 (sdas8051 + sdld) -----------------------------------------------

BLINK_8051 = """\
.module blink
.area CODE (ABS)
.org 0x0000
    ljmp start
.org 0x0100
start:
    clr 0x90
    setb 0x90
    sjmp start
"""

BAD_8051 = "bad instruction here"


class Test8051Assemble(unittest.TestCase):
    def test_blink_assembles(self):
        r = assemble.assemble_8051(BLINK_8051)
        self.assertTrue(r["success"], r.get("log") or r.get("errors"))

    def test_listing_present(self):
        r = assemble.assemble_8051(BLINK_8051)
        self.assertIsNotNone(r.get("listing"))
        self.assertEqual(r["listing"]["v"], 1)

    def test_listing_contains_start(self):
        r = assemble.assemble_8051(BLINK_8051)
        self.assertIn("start", r["listing"]["asm"])

    def test_hex_nonempty(self):
        r = assemble.assemble_8051(BLINK_8051)
        self.assertGreater(r["bytes"], 0)

    def test_syntax_error_line_accurate(self):
        r = assemble.assemble_8051(BAD_8051)
        self.assertFalse(r["success"])
        self.assertGreater(len(r["errors"]), 0)
        self.assertEqual(r["errors"][0]["line"], 1)
        self.assertIn("error", r["errors"][0]["message"].lower())


# ---- 6502 (ca65 + ld65) ---------------------------------------------------

BLINK_6502 = """\
.segment "CODE"
.proc main
    lda #$FF
    sta $6002
    lda #$01
loop:
    sta $6000
    jmp loop
.endproc
.segment "VECTORS"
.word $0000
.word main
.word $0000
"""

BAD_6502 = "bad instruction here"


class Test6502Assemble(unittest.TestCase):
    def _cfg(self):
        import os
        return os.path.join(os.path.dirname(__file__), "eater.cfg")

    def test_blink_assembles(self):
        r = assemble.assemble_6502(BLINK_6502, self._cfg())
        self.assertTrue(r["success"], r.get("log") or r.get("errors"))

    def test_listing_present(self):
        r = assemble.assemble_6502(BLINK_6502, self._cfg())
        self.assertIsNotNone(r.get("listing"))
        self.assertEqual(r["listing"]["v"], 1)

    def test_listing_contains_main(self):
        r = assemble.assemble_6502(BLINK_6502, self._cfg())
        self.assertIn("main", r["listing"]["asm"])

    def test_linemap_nonempty(self):
        r = assemble.assemble_6502(BLINK_6502, self._cfg())
        self.assertGreater(len(r["listing"]["lineMap"]), 0)

    def test_labels_present(self):
        r = assemble.assemble_6502(BLINK_6502, self._cfg())
        self.assertIsNotNone(r.get("labels"))
        self.assertIn("al ", r["labels"])

    def test_binary_nonempty(self):
        r = assemble.assemble_6502(BLINK_6502, self._cfg())
        self.assertGreater(r["bytes"], 0)

    def test_syntax_error_line_accurate(self):
        r = assemble.assemble_6502(BAD_6502, self._cfg())
        self.assertFalse(r["success"])
        self.assertGreater(len(r["errors"]), 0)
        self.assertEqual(r["errors"][0]["line"], 1)


# ---- AVR (avr-gcc -x assembler-with-cpp) -----------------------------------

BLINK_AVR = """\
#include <avr/io.h>
.global main
main:
    sbi _SFR_IO_ADDR(DDRB), 5
loop:
    sbi _SFR_IO_ADDR(PORTB), 5
    cbi _SFR_IO_ADDR(PORTB), 5
    rjmp loop
"""

BAD_AVR = "bad instruction here"


class TestAvrAssemble(unittest.TestCase):
    def _avr(self):
        import os
        from app import stage_avr
        bin_dir = stage_avr()
        if bin_dir is None:
            self.skipTest("no AVR toolchain")
        deps = os.path.join(os.path.dirname(bin_dir), "lib-deps")
        env = dict(os.environ)
        if os.path.isdir(deps):
            env["LD_LIBRARY_PATH"] = deps + os.pathsep + env.get("LD_LIBRARY_PATH", "")
        return bin_dir, env

    def test_blink_assembles(self):
        bin_dir, env = self._avr()
        r = assemble.assemble_avr(BLINK_AVR, "atmega328p", bin_dir, env)
        self.assertTrue(r["success"], r.get("log") or r.get("errors"))

    def test_listing_present(self):
        bin_dir, env = self._avr()
        r = assemble.assemble_avr(BLINK_AVR, "atmega328p", bin_dir, env)
        self.assertIsNotNone(r.get("listing"))
        self.assertEqual(r["listing"]["v"], 1)

    def test_listing_contains_loop(self):
        bin_dir, env = self._avr()
        r = assemble.assemble_avr(BLINK_AVR, "atmega328p", bin_dir, env)
        self.assertIn("loop", r["listing"]["asm"])

    def test_hex_nonempty(self):
        bin_dir, env = self._avr()
        r = assemble.assemble_avr(BLINK_AVR, "atmega328p", bin_dir, env)
        self.assertGreater(r["bytes"], 0)

    def test_syntax_error_line_accurate(self):
        bin_dir, env = self._avr()
        r = assemble.assemble_avr(BAD_AVR, "atmega328p", bin_dir, env)
        self.assertFalse(r["success"])
        self.assertGreater(len(r["errors"]), 0)
        self.assertEqual(r["errors"][0]["line"], 1)


if __name__ == "__main__":
    unittest.main()
