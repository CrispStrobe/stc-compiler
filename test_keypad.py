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


class TestKeypadMicroPython(unittest.TestCase):
    """The MicroPython lane (A2-BOARD-SUPPORT 'shared scanner — SETTLED
    2026-08-18'): the scanner IS Pin juggling, same 0..15 index, same
    debounce contract (two agreeing scans, 5 ms apart), hats on the PART."""

    PICO = """\
DEVICE PICO:
  PART keys = KEYPAD4X4 ROWS GP2 GP3 GP4 GP5 COLS GP6 GP7 GP8 GP9

  PIN led1 = GP25 OUTPUT

  WHEN started:
    FOREVER:
      IF a key is pressed THEN:
        turn on led1
      wait 20 ms

  WHEN key 5 pressed:
    toggle led1
"""

    def test_pico_scanner_tristates_rows(self):
        code, _ = sp.transpile(self.PICO)
        self.assertIn("def bw_part_keys_read():", code)
        # drive low, read a pulled-up column, RELEASE the row before returning
        self.assertIn("_pin2.init(Pin.OUT, value=0)", code)
        self.assertIn("if not _pin6.value():", code)
        self.assertIn("_pin2.init(Pin.IN)", code)
        self.assertIn("return 15", code)
        self.assertIn("return -1", code)
        # rows idle tri-stated; columns idle with the pull-up
        self.assertIn("_pin2 = Pin(2, Pin.IN)", code)
        self.assertIn("_pin6 = Pin(6, Pin.IN, Pin.PULL_UP)", code)

    def test_pico_debounced_poll_scheduled_first(self):
        code, _ = sp.transpile(self.PICO)
        self.assertIn("def bw_kp_keys_poll():", code)
        # wrap-safe 5 ms gate + two agreeing reads
        self.assertIn("if time.ticks_diff(time.ticks_ms(), bw_kp_keys_t) >= 5:", code)
        self.assertIn("if _r == bw_kp_keys_raw:", code)
        self.assertIn("_tasks = [bw_kp_keys_poll(), bw_task0(), bw_task1()]", code)

    def test_pico_hat_edges_on_debounced_key(self):
        code, _ = sp.transpile(self.PICO)
        self.assertIn("_now = 1 if bw_kp_keys_key == 5 else 0", code)
        self.assertIn("_fired = _now and not _prev", code)
        # the sugar desugars to the index read, same as the C targets
        self.assertIn("if bw_part_keys_read() >= 0:", code)

    def test_microbit_scanner_and_pulls(self):
        src = self.PICO.replace("DEVICE PICO:", "DEVICE MICROBIT:") \
            .replace("ROWS GP2 GP3 GP4 GP5 COLS GP6 GP7 GP8 GP9",
                     "ROWS P0 P1 P2 P8 COLS P12 P13 P14 P15") \
            .replace("GP25", "P16")
        code, _ = sp.transpile(src)
        self.assertIn("pin0.write_digital(0)", code)
        self.assertIn("if not pin12.read_digital():", code)
        # read_digital() is the micro:bit's tri-state
        self.assertIn("pin0.read_digital()   # release: back to input", code)
        self.assertIn("pin12.set_pull(pin12.PULL_UP)", code)
        # running_time() does not wrap; the plain gate is honest there
        self.assertIn("if (running_time() - bw_kp_keys_t) >= 5:", code)


if __name__ == "__main__":
    unittest.main()
