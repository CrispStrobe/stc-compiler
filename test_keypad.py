"""KEYPAD4X4 PART — sixteen keys for eight pins, read-only.

The emitted scanner is the one verified on Prechin A2 silicon
(stc12c5a60s2-lab: 06-matrix89 mapped the pins, 09-keyshow89 consumed
them, 2026-08-17). These tests pin the contract, not the silicon.
"""
import unittest

import stc_pseudocode as sp

SRC = """\
DEVICE STC89C52RC:
  CLOCK 11059200

  TABLE font = 0b00111111, 0b00000110, 0b01011011, 0b01001111

  PART keys = KEYPAD4X4 ROWS P1.7 P1.6 P1.5 P1.4 COLS P1.3 P1.2 P1.1 P1.0

  WHEN started:
    FOREVER:
      set k to keys
      IF k >= 0 THEN:
        print k
      wait 30 ms
"""


class TestKeypad(unittest.TestCase):
    def test_transpiles_with_scanner_helper(self):
        c, program = sp.transpile(SRC)
        self.assertIn("static signed char bw_part_keys_read(void)", c)
        # row-major: first row low, first column hit returns 0
        self.assertIn("if (!P1_3) { P1_7 = 1; return 0; }", c)
        self.assertIn("if (!P1_0) { P1_4 = 1; return 15; }", c)
        self.assertIn("return -1;", c)
        # the read site is the helper call
        self.assertIn("bw_part_keys_read()", c)

    def test_round_trip_is_stable(self):
        rt = sp.decompile(SRC)
        self.assertIn(
            "PART keys = KEYPAD4X4 ROWS P1.7 P1.6 P1.5 P1.4 "
            "COLS P1.3 P1.2 P1.1 P1.0", rt)
        c1, _ = sp.transpile(SRC)
        c2, _ = sp.transpile(rt)
        self.assertEqual(c1, c2)

    def test_keypad_cannot_be_written(self):
        bad = SRC.replace("set k to keys", "set keys to 5")
        with self.assertRaises(sp.PseudocodeError) as ctx:
            sp.transpile(bad)
        self.assertIn("cannot be written", str(ctx.exception))

    def test_duplicate_pin_refused(self):
        bad = SRC.replace("COLS P1.3", "COLS P1.7")
        with self.assertRaises(sp.PseudocodeError) as ctx:
            sp.transpile(bad)
        self.assertIn("same pin twice", str(ctx.exception))

    def test_pin_clash_with_part_refused(self):
        bad = SRC.replace(
            "WHEN started:",
            "PIN stray = P1.5 OUTPUT\n\n  WHEN started:")
        with self.assertRaises(sp.PseudocodeError):
            sp.transpile(bad)

    def test_capability_gated_off_non_8051(self):
        bad = SRC.replace("DEVICE STC89C52RC", "DEVICE ATMEGA328P")
        with self.assertRaises(sp.PseudocodeError) as ctx:
            sp.transpile(bad)
        self.assertIn("KEYPAD4X4", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
