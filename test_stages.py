"""test_stages — verify the debug stages payload on /assemble.

When debug=True, the response gains stages: {tokens, passes, listing}.
Tests per chain with hand-verified token/symbol expectations.
"""
import unittest

import assemble
import stages


# ---- tokenizer tests ----

class TestTokenizer(unittest.TestCase):
    def test_label_instruction_comment(self):
        tokens = stages.tokenize_asm("loop:\n    lda #$05  ; load A")
        types = [t["type"] for t in tokens]
        self.assertIn("label", types)
        self.assertIn("identifier", types)  # lda
        self.assertIn("comment", types)

    def test_directive(self):
        tokens = stages.tokenize_asm('.segment "CODE"')
        dirs = [t for t in tokens if t["type"] == "directive"]
        self.assertEqual(dirs[0]["text"], ".segment")

    def test_number_formats(self):
        tokens = stages.tokenize_asm("    lda #$FF\n    ldx #0x10\n    ldy #42")
        nums = [t for t in tokens if t["type"] == "number"]
        self.assertGreater(len(nums), 0)

    def test_line_col(self):
        tokens = stages.tokenize_asm("start:\n    nop")
        label = next(t for t in tokens if t["type"] == "label")
        self.assertEqual(label["line"], 1)
        self.assertEqual(label["col"], 1)
        nop = next(t for t in tokens if t["text"] == "nop")
        self.assertEqual(nop["line"], 2)


# ---- 6502 / ca65 stages ----

BLINK_6502 = """\
.segment "CODE"
.proc main
    COUNT = 5
    lda #COUNT
    sta $6000
loop:
    dex
    bne loop
    rts
.endproc
.segment "VECTORS"
.word $0000
.word main
.word $0000
"""


class Test6502Stages(unittest.TestCase):
    def _assemble(self, **kw):
        from app import stage_cc65
        bin_dir = stage_cc65()
        import os
        cfg = os.path.join(os.path.dirname(__file__), "eater.cfg")
        return assemble.assemble_6502(BLINK_6502, cfg, bin_dir=bin_dir, **kw)

    def test_no_stages_without_debug(self):
        r = self._assemble(debug=False)
        self.assertTrue(r["success"])
        self.assertNotIn("stages", r)

    def test_stages_present_with_debug(self):
        r = self._assemble(debug=True)
        self.assertTrue(r["success"])
        self.assertIn("stages", r)

    def test_stages_has_tokens(self):
        r = self._assemble(debug=True)
        tokens = r["stages"]["tokens"]
        self.assertIsInstance(tokens, list)
        self.assertGreater(len(tokens), 5)
        # Should find 'loop' as a label and 'main' as an identifier (after .proc)
        labels = [t for t in tokens if t["type"] == "label"]
        self.assertTrue(any(t["text"] == "loop" for t in labels),
                        f"no 'loop' label in {[t['text'] for t in labels]}")
        idents = [t for t in tokens if t["type"] == "identifier"]
        self.assertTrue(any(t["text"] == "main" for t in idents),
                        f"no 'main' in identifiers")

    def test_stages_has_symbols(self):
        r = self._assemble(debug=True)
        passes = r["stages"]["passes"]
        self.assertGreater(len(passes), 0)
        symbols = passes[0]["symbols"]
        self.assertIn("main", symbols)
        self.assertEqual(symbols["main"]["value"], 0x8000)
        self.assertTrue(symbols["main"]["resolved"])
        self.assertIn("loop", symbols)
        self.assertIn("COUNT", symbols)
        self.assertEqual(symbols["COUNT"]["value"], 5)

    def test_stages_has_listing(self):
        r = self._assemble(debug=True)
        listing = r["stages"]["listing"]
        self.assertIsInstance(listing, str)
        self.assertIn("lda", listing.lower())
        self.assertIn("loop", listing)


# ---- 8051 / sdas stages ----

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


class Test8051Stages(unittest.TestCase):
    def _assemble(self, **kw):
        from app import sdcc_bin_dir, stage_toolchain
        stage_toolchain()
        return assemble.assemble_8051(BLINK_8051, sdcc_bin_dir(), **kw)

    def test_no_stages_without_debug(self):
        r = self._assemble(debug=False)
        self.assertTrue(r["success"])
        self.assertNotIn("stages", r)

    def test_stages_present_with_debug(self):
        r = self._assemble(debug=True)
        self.assertTrue(r["success"])
        self.assertIn("stages", r)

    def test_stages_has_tokens(self):
        r = self._assemble(debug=True)
        tokens = r["stages"]["tokens"]
        self.assertIsInstance(tokens, list)
        labels = [t for t in tokens if t["type"] == "label"]
        self.assertTrue(any(t["text"] == "start" for t in labels))

    def test_stages_has_listing(self):
        r = self._assemble(debug=True)
        listing = r["stages"]["listing"]
        self.assertIsInstance(listing, str)
        self.assertGreater(len(listing), 0)


# ---- Z80 / sdasz80 stages ----

HALT_Z80 = """\
.module z80halt
.area CODE (ABS)
.org 0x0000
start:
    ld a, #0x55
    halt
"""


class TestZ80Stages(unittest.TestCase):
    def _assemble(self, **kw):
        from app import sdcc_bin_dir, stage_toolchain
        stage_toolchain()
        return assemble.assemble_z80(HALT_Z80, sdcc_bin_dir(), **kw)

    def test_stages_present_with_debug(self):
        r = self._assemble(debug=True)
        self.assertTrue(r["success"])
        self.assertIn("stages", r)

    def test_stages_has_tokens(self):
        r = self._assemble(debug=True)
        tokens = r["stages"]["tokens"]
        labels = [t for t in tokens if t["type"] == "label"]
        self.assertTrue(any(t["text"] == "start" for t in labels))


# ---- AVR / avr-gcc stages ----

BLINK_AVR = """\
.global main
main:
    sbi 0x04, 5
    sbi 0x05, 5
loop:
    rjmp loop
"""


class TestAvrStages(unittest.TestCase):
    def _assemble(self, **kw):
        from app import stage_avr
        import os
        bin_dir = stage_avr()
        if bin_dir is None:
            self.skipTest("no AVR toolchain")
        env = dict(os.environ)
        deps = os.path.join(os.path.dirname(bin_dir), "lib-deps")
        if os.path.isdir(deps):
            env["LD_LIBRARY_PATH"] = deps + os.pathsep + env.get("LD_LIBRARY_PATH", "")
        return assemble.assemble_avr(BLINK_AVR, "atmega328p", bin_dir, env, **kw)

    def test_stages_present_with_debug(self):
        r = self._assemble(debug=True)
        self.assertTrue(r["success"])
        self.assertIn("stages", r)

    def test_stages_has_tokens(self):
        r = self._assemble(debug=True)
        tokens = r["stages"]["tokens"]
        labels = [t for t in tokens if t["type"] == "label"]
        self.assertTrue(any(t["text"] == "loop" for t in labels))

    def test_stages_has_symbols(self):
        r = self._assemble(debug=True)
        passes = r["stages"]["passes"]
        self.assertGreater(len(passes), 0)
        symbols = passes[0]["symbols"]
        self.assertIn("main", symbols)
        self.assertTrue(symbols["main"]["resolved"])


# ---- ARM / arm-none-eabi-gcc stages ----

VECTOR_LOOP_ARM = """\
.syntax unified
.cpu cortex-m4
.thumb
.section .vectors, "a"
.word 0x20020000
.word Reset_Handler
.text
.global Reset_Handler
.type Reset_Handler, %function
Reset_Handler:
    b .
"""


class TestArmStages(unittest.TestCase):
    def _assemble(self, **kw):
        from app import stage_arm
        import os
        bin_dir = stage_arm()
        if bin_dir is None:
            self.skipTest("no ARM toolchain")
        env = dict(os.environ)
        deps = os.path.join(os.path.dirname(bin_dir), "lib-deps")
        if os.path.isdir(deps):
            env["LD_LIBRARY_PATH"] = deps + os.pathsep + env.get("LD_LIBRARY_PATH", "")
        ld = os.path.join(os.path.dirname(__file__), "nrf52833.ld")
        return assemble.assemble_arm(VECTOR_LOOP_ARM, "cortex-m4",
                                     bin_dir, env, ld, **kw)

    def test_stages_present_with_debug(self):
        r = self._assemble(debug=True)
        self.assertTrue(r["success"])
        self.assertIn("stages", r)

    def test_stages_has_tokens(self):
        r = self._assemble(debug=True)
        tokens = r["stages"]["tokens"]
        labels = [t for t in tokens if t["type"] == "label"]
        self.assertTrue(any(t["text"] == "Reset_Handler" for t in labels))

    def test_stages_has_symbols(self):
        r = self._assemble(debug=True)
        passes = r["stages"]["passes"]
        self.assertGreater(len(passes), 0)
        symbols = passes[0]["symbols"]
        self.assertIn("Reset_Handler", symbols)


if __name__ == "__main__":
    unittest.main()
