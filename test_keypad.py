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


HAT_SRC = """\
DEVICE STC89C52RC:
  CLOCK 11059200

  PART keys = KEYPAD4X4 ROWS P1.7 P1.6 P1.5 P1.4 COLS P1.3 P1.2 P1.1 P1.0

  PORT segments = P0 OUTPUT

  WHEN started:
    set segments to 0
    FOREVER:
      IF a key is pressed THEN:
        set segments to 255
      IF key 3 is pressed THEN:
        set segments to 3
      wait 20 ms

  WHEN key 5 pressed:
    set segments to 5

  WHEN key 14 released:
    set segments to 0
"""


class TestKeypadHats(unittest.TestCase):
    """`WHEN key N pressed` + the reporter sugar (A2-BOARD-SUPPORT fan-out).

    The hats poll a shared DEBOUNCED scan -- one poll task per keypad, at
    most every 5 ms, and a key only becomes current after two agreeing
    reads -- then edge-detect exactly the way pin hats do."""

    def test_reporters_desugar(self):
        # `a key is pressed` is `keys >= 0`; `key 3 is pressed` is `keys = 3`.
        # No new AST shape, so every back end lowers them for free.
        c, _ = sp.transpile(HAT_SRC)
        self.assertIn("if (bw_part_keys_read() >= 0) {", c)
        self.assertIn("if (bw_part_keys_read() == 3) {", c)

    def test_debounced_poll_task(self):
        c, _ = sp.transpile(HAT_SRC)
        self.assertIn("static void bw_kp_keys_poll(void)", c)
        self.assertIn("if ((unsigned int)(bw_now() - bw_kp_keys_t) < 5)", c)
        self.assertIn("if (r == bw_kp_keys_raw)", c)      # two agreeing reads
        # the poll is dispatched in the scheduler loop, before the hats
        self.assertIn("        bw_kp_keys_poll();", c)
        self.assertLess(c.index("bw_kp_keys_poll();\n"),
                        c.index("bw_task1();"))

    def test_hats_edge_detect_on_debounced_key(self):
        c, _ = sp.transpile(HAT_SRC)
        self.assertIn("unsigned char now = (bw_kp_keys_key == 5) ? 1 : 0;", c)
        self.assertIn("unsigned char now = (bw_kp_keys_key == 14) ? 1 : 0;", c)
        # pressed = rising edge, released = falling edge
        self.assertIn("(now && !bw_task1_prev)", c)
        self.assertIn("(!now && bw_task2_prev)", c)

    def test_round_trip_is_a_fixed_point(self):
        c, prog = sp.transpile(HAT_SRC)
        back = sp.emit_pseudocode(prog)
        self.assertIn("WHEN key 5 pressed:", back)
        self.assertIn("WHEN key 14 released:", back)
        c2, prog2 = sp.transpile(back)
        self.assertEqual(c2, c)
        self.assertEqual(sp.emit_pseudocode(prog2), back)

    def test_key_variable_still_a_variable(self):
        # The keyshow example names a VARIABLE `key`; the four-token guard
        # (`key <digit> is pressed|released`) must not swallow it.
        src = HAT_SRC.replace("IF key 3 is pressed THEN:\n        set segments to 3\n      ",
                              "set key to 7\n      IF key = 7 THEN:\n        set segments to 7\n      ")
        c, _ = sp.transpile(src)
        self.assertIn("if (key == 7) {", c)

    def test_out_of_range_key_is_refused(self):
        for bad in ("WHEN key 16 pressed:", "WHEN key 99 released:"):
            src = HAT_SRC.replace("WHEN key 14 released:", bad)
            with self.assertRaises(sp.PseudocodeError):
                sp.transpile(src)
        src = HAT_SRC.replace("IF key 3 is pressed THEN:", "IF key 16 is pressed THEN:")
        with self.assertRaises(sp.PseudocodeError):
            sp.transpile(src)

    def test_no_keypad_is_refused(self):
        src = HAT_SRC.replace(
            "  PART keys = KEYPAD4X4 ROWS P1.7 P1.6 P1.5 P1.4 COLS P1.3 P1.2 P1.1 P1.0\n\n", "")
        with self.assertRaises(sp.PseudocodeError):
            sp.transpile(src)
