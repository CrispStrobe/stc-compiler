"""test_a2_parts — SEVENSEG8 and LEDBANK8 compile + round-trip tests.

Each test parses pseudocode, emits C, and verifies the output contains
the expected ISR hooks, frame buffers, font tables, and helper functions.
Round-trip: pseudocode -> AST -> canonical pseudocode is a fixed point.
"""
import unittest

import stc_pseudocode as sp


class TestSevenSeg8(unittest.TestCase):
    """PART display = SEVENSEG8 SEGMENTS P0 SELECT P2.0 P2.1 P2.2"""

    DECL = ("DEVICE STC12C5A60S2:\n  CLOCK 11059200\n"
            "  PART display = SEVENSEG8 SEGMENTS P0 SELECT P2.0 P2.1 P2.2\n\n")

    def _parse(self, body):
        return sp.parse(self.DECL + "  WHEN started:\n" + body)

    def _c(self, body):
        return sp.emit_c(self._parse(body))

    def test_parse_sevenseg(self):
        prog = self._parse("    show number 42 on display\n")
        self.assertIn("display", prog.parts)
        self.assertIsInstance(prog.parts["display"], sp.SevenSegPart)
        self.assertEqual(prog.parts["display"].seg_port, 0)
        self.assertEqual(len(prog.parts["display"].sel_pins), 3)

    def test_c_has_font(self):
        c = self._c("    show number 42 on display\n")
        self.assertIn("bw_7seg_font", c)
        self.assertIn("0x3F", c)  # digit 0

    def test_c_has_framebuffer(self):
        c = self._c("    show number 42 on display\n")
        self.assertIn("bw_display_fb[8]", c)
        self.assertIn("bw_display_cur", c)

    def test_c_has_isr(self):
        c = self._c("    show number 42 on display\n")
        self.assertIn("bw_tick", c)
        self.assertIn("bw_ms++", c)
        # ISR scan sets the select pins
        self.assertIn("P2_0 = bw_display_cur", c)

    def test_c_bw_ms_first(self):
        """bw_ms++ must be the FIRST thing after the reload in the ISR."""
        c = self._c("    show number 42 on display\n")
        lines = c.split("\n")
        ms_line = next(i for i, l in enumerate(lines) if "bw_ms++" in l)
        reload_line = next(i for i, l in enumerate(lines)
                          if "TH0 = (unsigned char)" in l)
        # bw_ms++ is immediately after the reload
        self.assertEqual(ms_line, reload_line + 1)

    def test_show_number(self):
        c = self._c("    show number 1234 on display\n")
        self.assertIn("bw_display_show_number(1234)", c)

    def test_show_number_runs(self):
        """The emitted bw_<part>_show_number, executed (Lite tasks B14, B18).

        The helper is cut out of the emitted C with its font and frame
        buffer, built by the host compiler with SDCC's widths (int 16 bits,
        long 32), and run: the frame buffer must read right-aligned with the
        minus DIRECTLY before the most significant digit, and the number
        must not wrap at 16 bits. MEASURED before B14: -42 gave "    - 42"
        (the sign one digit too far left). MEASURED before B18 (on the A2
        bench in Lite, SDCC + emu8051): the helper took an `int`, so 32768
        showed "  -32768", 65536 "       0" and 12345678 "   24910". A
        number wider than the display shows its low eight digits and no
        minus, as the stc12 block (CrispStrobe/extensions) does.
        """
        import re
        import shutil
        import subprocess
        import tempfile
        cc = shutil.which("cc") or shutil.which("gcc")
        if not cc:
            self.skipTest("no host C compiler to run the emitted helper")
        c = self._c("    show number -42 on display\n")
        font = re.search(r"static const __code unsigned char bw_7seg_font\[16\] = \{.*?\};",
                         c, re.S).group(0).replace("__code ", "")
        helper = re.search(r"static void bw_display_show_number\((?:int|long) n\)\n\{.*?\n\}\n",
                           c, re.S).group(0)
        # SDCC's widths on a host whose int is 32 bits: long -> int32_t
        # first (so the int macro below cannot reach it), then int -> short.
        helper = re.sub(r"\bunsigned long\b", "uint32_t", helper)
        helper = re.sub(r"\blong\b", "int32_t", helper)
        helper = re.sub(r"\b0UL\b", "((uint32_t)0)", helper)
        helper = re.sub(r"\b0xFFFFUL\b", "((uint32_t)0xFFFF)", helper)
        cases = [
            (-42, "     -42"), (42, "      42"), (-7, "      -7"), (0, "       0"),
            (-1, "      -1"), (-32768, "  -32768"), (32767, "   32767"), (-9999, "   -9999"),
            (32768, "   32768"), (-32769, "  -32769"), (65535, "   65535"),
            (65536, "   65536"), (99999, "   99999"), (12345678, "12345678"),
            (99999999, "99999999"), (-1234567, "-1234567"), (-9999999, "-9999999"),
            (-12345678, "12345678"), (100000000, "00000000"), (123456789, "23456789"),
            (2147483647, "47483647"), (-2147483648, "47483648"),
        ]
        prog = "\n".join([
            "#include <stdio.h>",
            "#include <stdint.h>",
            "#define int short",
            font,
            "static unsigned char bw_display_fb[8];",
            helper,
            "#undef int",
            "static const int64_t cases[] = {%s};" % ", ".join(f"{n}LL" for n, _ in cases),
            "int main(void) {",
            "    unsigned k, d;",
            "    for (k = 0; k < sizeof cases / sizeof cases[0]; k++) {",
            "        bw_display_show_number((int32_t)cases[k]);",
            "        for (d = 0; d < 8; d++) {",
            "            unsigned char b = bw_display_fb[d], g = '?', j;",
            "            if (b == 0) g = ' ';",
            "            else if (b == 0x40) g = '-';",
            "            else for (j = 0; j < 10; j++) if (bw_7seg_font[j] == b) g = '0' + j;",
            "            putchar(g);",
            "        }",
            "        putchar('\\n');",
            "    }",
            "    return 0;",
            "}",
        ])
        with tempfile.TemporaryDirectory() as tmp:
            src, exe = f"{tmp}/show.c", f"{tmp}/show"
            with open(src, "w") as f:
                f.write(prog)
            subprocess.run([cc, "-o", exe, src], check=True, capture_output=True)
            out = subprocess.run([exe], check=True, capture_output=True, text=True).stdout
        got = out.split("\n")[:len(cases)]
        self.assertEqual(list(zip([n for n, _ in cases], got)), cases)

    def test_show_digit(self):
        c = self._c("    show digit 0 = value 5 on display\n")
        self.assertIn("bw_display_show_digit", c)

    def test_set_segments(self):
        c = self._c("    set digit 3 to segments 127 on display\n")
        self.assertIn("bw_display_set_segments", c)

    def test_clear_display(self):
        c = self._c("    clear display\n")
        self.assertIn("bw_display_clear()", c)

    def test_roundtrip(self):
        prog = self._parse("    show number 42 on display\n"
                           "    clear display\n")
        pseudo = sp.emit_pseudocode(prog)
        pseudo2 = sp.emit_pseudocode(sp.parse(pseudo))
        self.assertEqual(pseudo, pseudo2)

    def test_common_anode(self):
        src = ("DEVICE STC12C5A60S2:\n  CLOCK 11059200\n"
               "  PART d = SEVENSEG8 SEGMENTS P0 SELECT P2.0 P2.1 P2.2 "
               "COMMON ANODE\n\n  WHEN started:\n    show number 1 on d\n")
        prog = sp.parse(src)
        self.assertTrue(prog.parts["d"].common_anode)
        c = sp.emit_c(prog)
        # Common anode inverts the segment byte
        self.assertIn("~bw_d_fb[bw_d_cur]", c)

    def test_timer0_started_no_tasks(self):
        """With a single WHEN and SEVENSEG8, Timer 0 starts for the ISR."""
        c = self._c("    show number 42 on display\n")
        self.assertIn("ET0 = 1", c)
        self.assertIn("EA  = 1", c)
        self.assertIn("TR0 = 1", c)

    def test_with_cooperative_tasks(self):
        """With multiple WHENs, the cooperative scheduler runs."""
        src = (self.DECL +
               "  PIN led = P1.0 OUTPUT ACTIVE LOW\n\n"
               "  WHEN started:\n    show number 42 on display\n"
               "    FOREVER:\n      wait 1 seconds\n\n"
               "  WHEN started:\n    turn on led\n")
        prog = sp.parse(src)
        c = sp.emit_c(prog)
        self.assertIn("bw_now", c)
        self.assertIn("bw_display_cur", c)


class TestLedBank8(unittest.TestCase):
    """PART leds = LEDBANK8 ON P1 ACTIVE LOW"""

    DECL = ("DEVICE STC12C5A60S2:\n  CLOCK 11059200\n"
            "  PART leds = LEDBANK8 ON P1 ACTIVE LOW\n\n")

    def _parse(self, body):
        return sp.parse(self.DECL + "  WHEN started:\n" + body)

    def _c(self, body):
        return sp.emit_c(self._parse(body))

    def test_parse_ledbank(self):
        prog = self._parse("    turn on led 0 on leds\n")
        self.assertIn("leds", prog.parts)
        self.assertIsInstance(prog.parts["leds"], sp.LedBankPart)
        self.assertTrue(prog.parts["leds"].active_low)

    def test_c_has_shadow(self):
        c = self._c("    turn on led 0 on leds\n")
        self.assertIn("bw_leds_shadow", c)

    def test_c_active_low_isr(self):
        c = self._c("    turn on led 0 on leds\n")
        self.assertIn("~bw_leds_shadow", c)

    def test_turn_on_led(self):
        c = self._c("    turn on led 3 on leds\n")
        self.assertIn("bw_leds_on", c)

    def test_turn_off_led(self):
        c = self._c("    turn off led 5 on leds\n")
        self.assertIn("bw_leds_off", c)

    def test_set_leds(self):
        c = self._c("    set leds to 255 on leds\n")
        self.assertIn("bw_leds_set", c)

    def test_light_only(self):
        c = self._c("    light only led 0 on leds\n")
        self.assertIn("bw_leds_only", c)

    def test_roundtrip(self):
        prog = self._parse("    turn on led 3 on leds\n"
                           "    set leds to 255 on leds\n")
        pseudo = sp.emit_pseudocode(prog)
        pseudo2 = sp.emit_pseudocode(sp.parse(pseudo))
        self.assertEqual(pseudo, pseudo2)

    def test_active_high(self):
        src = ("DEVICE STC12C5A60S2:\n  CLOCK 11059200\n"
               "  PART bank = LEDBANK8 ON P1\n\n"
               "  WHEN started:\n    turn on led 0 on bank\n")
        prog = sp.parse(src)
        self.assertFalse(prog.parts["bank"].active_low)
        c = sp.emit_c(prog)
        # Active high: no inversion
        self.assertIn("bw_bank_shadow;", c)
        self.assertNotIn("~bw_bank_shadow", c)


class TestSharedPort(unittest.TestCase):
    """SEVENSEG8 + LEDBANK8 on the same port."""

    def test_shared_port_compiles(self):
        src = ("DEVICE STC12C5A60S2:\n  CLOCK 11059200\n"
               "  PART display = SEVENSEG8 SEGMENTS P0 SELECT P2.0 P2.1 P2.2\n"
               "  PART leds = LEDBANK8 ON P2\n\n"
               "  WHEN started:\n    show number 42 on display\n"
               "    turn on led 7 on leds\n")
        prog = sp.parse(src)
        c = sp.emit_c(prog)
        self.assertIn("bw_display_cur", c)
        self.assertIn("bw_leds_shadow", c)

    def test_shared_port_roundtrip(self):
        src = ("DEVICE STC12C5A60S2:\n  CLOCK 11059200\n"
               "  PART display = SEVENSEG8 SEGMENTS P0 SELECT P2.0 P2.1 P2.2\n"
               "  PART leds = LEDBANK8 ON P2\n\n"
               "  WHEN started:\n    show number 42 on display\n"
               "    turn on led 7 on leds\n")
        pseudo = sp.emit_pseudocode(sp.parse(src))
        pseudo2 = sp.emit_pseudocode(sp.parse(pseudo))
        self.assertEqual(pseudo, pseudo2)

    def test_both_in_isr(self):
        src = ("DEVICE STC12C5A60S2:\n  CLOCK 11059200\n"
               "  PART display = SEVENSEG8 SEGMENTS P0 SELECT P2.0 P2.1 P2.2\n"
               "  PART leds = LEDBANK8 ON P2\n\n"
               "  WHEN started:\n    show number 1 on display\n"
               "    turn on led 0 on leds\n")
        c = sp.emit_c(sp.parse(src))
        # Both scan hooks in the ISR
        lines = c.split("\n")
        in_isr = False
        isr_body = []
        for line in lines:
            if "void bw_tick" in line:
                in_isr = True
            if in_isr:
                isr_body.append(line)
            if in_isr and line.strip() == "}":
                break
        isr_text = "\n".join(isr_body)
        self.assertIn("bw_display_cur", isr_text)
        self.assertIn("bw_leds_shadow", isr_text)


class TestDuplicateNameRefused(unittest.TestCase):
    def test_same_name_twice(self):
        src = ("DEVICE STC12C5A60S2:\n  CLOCK 11059200\n"
               "  PART x = SEVENSEG8 SEGMENTS P0 SELECT P2.0 P2.1 P2.2\n"
               "  PART x = LEDBANK8 ON P1\n\n"
               "  WHEN started:\n    show number 1 on x\n")
        with self.assertRaises(sp.PseudocodeError):
            sp.parse(src)


if __name__ == "__main__":
    unittest.main()
