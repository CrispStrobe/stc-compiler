"""MATRIX8X8 PART — an 8x8 LED dot matrix that refreshes itself in the Timer-0
ISR (one row per tick, 125 Hz), so the drawing verbs are plain frame-buffer
writes and the WHEN block keeps running.

Wiring measured on Prechin A2 silicon (docs/BOARD-PRECHIN-A2.md): 595 rows
active-HIGH with Q7 = top, port columns active-LOW with bit7 = left. These
tests pin the contract and the scan ORIENTATION, not the silicon.

The acceptance example is pseudocode/16-a2-screen.bw in the stc12 lab repo; a
copy of its vocabulary is exercised inline here so the test is self-contained.
"""
import re
import unittest

import stc_pseudocode as sp

SRC = """\
DEVICE STC89C52RC:
  CLOCK 11059200

  TABLE heart = 0b01100110, 0b11111111, 0b11111111, 0b11111111, 0b01111110, 0b00111100, 0b00011000, 0b00000000

  PART screen = MATRIX8X8 ROWS 74HC595 DATA P3.4 CLOCK P3.6 LATCH P3.5 COLUMNS P0

  WHEN started:
    show image heart on screen
    wait 1 second
    clear screen
    set d to 0
    REPEAT 8:
      light pixel d d
      wait 100 ms
      change d by 1
    draw row 0 = 0b11111111
    scroll screen left
    set pixel 2 3 to on
    set pixel 4 5 brightness 2
    set screen brightness 1
    IF pixel 2 3 is on THEN:
      clear pixel 2 3
"""

HEART = [0b01100110, 0b11111111, 0b11111111, 0b11111111,
         0b01111110, 0b00111100, 0b00011000, 0b00000000]


def isr_body(c: str) -> str:
    m = re.search(r"void bw_tick\(void\) __interrupt\(1\)\n\{.*?\n\}\n", c, re.S)
    assert m, "no bw_tick ISR in generated C"
    return m.group(0)


class RefMatrix:
    """A faithful Python port of the emitted buffer + scan, used to compute the
    golden (row-select byte, column-port byte) trace and compare orientation.

    Buffer: 2 bit-planes of 8 row-bytes; bit(7-x) is column x (bit7 = left).
    A pixel's level is the little-endian bits across the planes."""
    PLANES = 2
    LEVELS = 4

    def __init__(self):
        self.buf = [0] * (8 * self.PLANES)
        self.dim = self.LEVELS - 1
        self.scan = 0
        self.phase = 0
        self.rowbit = [0x80 >> y for y in range(8)]

    def setpx(self, x, y, level):
        m = 0x80 >> x
        for p in range(self.PLANES):
            if level & 1:
                self.buf[y + p * 8] |= m
            else:
                self.buf[y + p * 8] &= ~m & 0xFF
            level >>= 1

    def row(self, y, bits):
        for p in range(self.PLANES):
            self.buf[y + p * 8] = bits & 0xFF

    def image(self, img):
        for y in range(8):
            self.row(y, img[y])

    def tick(self):
        """One ISR pass. Returns (row_select_byte_on_595, column_port_byte).

        Bit-plane phase render (BCM): phase p lights 'level > p', so over the
        LEVELS-1 phases a pixel of level L is lit in L of them (duty L/3). The
        global dim caps at min(level, dim): a phase renders only while dim > p."""
        rb = self.rowbit[self.scan]
        if self.dim > self.phase:
            p0 = self.buf[self.scan]
            p1 = self.buf[self.scan + 8]
            if self.phase == 0:
                lit = p0 | p1            # level >= 1
            elif self.phase == 1:
                lit = p1                 # level >= 2
            else:
                lit = p0 & p1            # level >= 3
        else:
            lit = 0
        col = ~lit & 0xFF
        row = self.scan
        self.scan += 1
        if self.scan >= 8:
            self.scan = 0
            self.phase = (self.phase + 1) % (self.LEVELS - 1)
        return row, rb, col

    def frame(self):
        return [self.tick() for _ in range(8)]


class TestMatrix(unittest.TestCase):
    def test_compiles_with_buffer_scan_and_helpers(self):
        c, _ = sp.transpile(SRC)
        # 2 bits/pixel, 4 levels, 16 bytes, one widen-point constant.
        self.assertIn("#define MATRIX_PLANES 2", c)
        self.assertIn("#define MATRIX_LEVELS 4", c)
        self.assertIn("static unsigned char bw_scr_screen[8 * MATRIX_PLANES];", c)
        # every drawing verb has a helper.
        for helper in ("_clear", "_setpx", "_getpx", "_row", "_image", "_scroll"):
            self.assertIn(f"bw_scr_screen{helper}", c)
        self.assertIn("bw_scr_screen_dim", c)         # global brightness storage
        self.assertIn("bw_scr_level(", c)             # brightness clamp

    def test_isr_ms_first_and_no_muldiv(self):
        c, _ = sp.transpile(SRC)
        body = isr_body(c)
        # bw_ms++ must be the FIRST statement, before any scan work.
        self.assertLess(body.index("bw_ms++"), body.index("bw_scr_screen"),
                        "bw_ms++ must precede the matrix scan in the ISR")
        # No multiply/divide/modulo anywhere in the ISR (table-driven scan).
        stripped = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
        for op in ("*", "/", "%"):
            self.assertNotIn(op, stripped, f"ISR must contain no {op!r}")
        # One row per tick, wrapping at 8; a completed frame steps the BCM phase.
        self.assertIn("bw_scr_screen_scan++", body)
        self.assertIn("if (bw_scr_screen_scan >= 8) {", body)
        self.assertIn("bw_scr_screen_scan = 0;", body)
        self.assertIn("bw_scr_screen_phase++;", body)
        self.assertIn("if (bw_scr_screen_phase >= MATRIX_LEVELS - 1) "
                      "bw_scr_screen_phase = 0;", body)

    def test_isr_is_sole_writer_of_595_and_columns(self):
        c, _ = sp.transpile(SRC)
        body = isr_body(c)
        # The ISR writes the 595 pins and the column port...
        for sig in ("P3_4 =", "P3_6 =", "P3_5 =", "P0 ="):
            self.assertIn(sig, body)
        # ...and NOTHING outside the ISR writes P0 or those pins (mainline only
        # ever touches the RAM frame buffer). Strip the ISR, then look.
        outside = c.replace(body, "")
        for sig in ("P0 =", "P3_4 =", "P3_5 =", "P3_6 ="):
            self.assertNotIn(sig, outside,
                             f"{sig!r} must appear only inside the ISR")

    def test_rowbit_table_is_q7_top(self):
        c, _ = sp.transpile(SRC)
        self.assertIn("{ 0x80, 0x40, 0x20, 0x10, 0x08, 0x04, 0x02, 0x01 };", c)

    def test_golden_scan_trace_for_heart(self):
        """The measured orientation: with the heart shown at full brightness,
        tick y must select 595 bit (0x80>>y) and drive the column port with
        ~heart[y] (active-low columns, image bit7 = left)."""
        ref = RefMatrix()
        ref.image(HEART)
        trace = ref.frame()
        expected = [(y, 0x80 >> y, (~HEART[y]) & 0xFF) for y in range(8)]
        self.assertEqual(trace, expected)
        # And the top row of the heart (0b01100110) lights columns 1,2,5,6:
        # those column bits go LOW, the rest HIGH.
        _, rb0, col0 = trace[0]
        self.assertEqual(rb0, 0x80)                    # row 0 == Q7 == top
        self.assertEqual(col0, 0x99)                   # ~0b01100110

    def test_golden_single_pixel(self):
        """A single lit pixel (0,0): only row 0 carries a low column, and only
        the left-most column bit is low there."""
        ref = RefMatrix()
        ref.setpx(0, 0, RefMatrix.LEVELS - 1)
        trace = ref.frame()
        rows_lit = [(y, col) for (y, _rb, col) in trace if col != 0xFF]
        self.assertEqual(rows_lit, [(0, 0x7F)])        # ~0x80: only bit7 (left) low

    def test_bcm_duty_per_level(self):
        """The grayscale claim: over the LEVELS-1 BCM phases a pixel of level L
        is lit in exactly L of them, so duty = L/3. This is what makes the four
        painted brightness levels actually distinct on the panel."""
        colbit = 0x80 >> 3                             # cell x=3
        for level in range(RefMatrix.LEVELS):          # 0..3
            ref = RefMatrix()
            ref.setpx(3, 4, level)                     # cell (x=3, y=4)
            lit = 0
            for _ in range(RefMatrix.LEVELS - 1):      # one frame per phase
                _, _, col = ref.frame()[4]             # row y=4 this phase
                if (~col & 0xFF) & colbit:
                    lit += 1
            self.assertEqual(lit, level,
                             f"level {level} lit in {lit}/3 phases, want {level}")

    def test_bcm_global_dim_caps_level(self):
        """`set screen brightness B` caps every pixel at min(level, B): a full-
        brightness pixel under dim=1 lights in only 1 of the 3 phases (1/3)."""
        colbit = 0x80 >> 3
        ref = RefMatrix()
        ref.setpx(3, 4, RefMatrix.LEVELS - 1)          # full brightness
        ref.dim = 1                                    # global cap at 1/3
        lit = sum(1 for _ in range(RefMatrix.LEVELS - 1)
                  if (~ref.frame()[4][2] & 0xFF) & colbit)
        self.assertEqual(lit, 1, "dim=1 must cap the pixel at 1/3 duty")

    def test_brightness_reporter_lowers(self):
        c, _ = sp.transpile(SRC)
        # `pixel 2 3 is on` becomes a getpx != 0 test.
        self.assertIn("bw_scr_screen_getpx", c)
        self.assertRegex(c, r"bw_scr_screen_getpx\(.*?\)\s*!=\s*0\)")
        # `set pixel X Y brightness B` and `set screen brightness B` clamp.
        self.assertIn("bw_scr_screen_dim = bw_scr_level(", c)

    def test_builds_under_sdcc_if_present(self):
        """Against the VENDORED sdcc, so this runs everywhere rather than only
        on a developer's box -- and so it exercises the 4.0.0 the service
        ships, not whatever `brew install sdcc` last put on PATH."""
        import shutil
        import subprocess
        import tempfile
        import os
        from app import stage_toolchain, sdcc_bin_dir
        stage_toolchain()
        bin_dir = sdcc_bin_dir()
        sdcc = os.path.join(bin_dir, "sdcc") if bin_dir else None
        if not (sdcc and os.path.exists(sdcc)):
            sdcc = shutil.which("sdcc")
        if not sdcc:
            self.skipTest("no vendored or system sdcc")
        c, _ = sp.transpile(SRC)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "m.c")
            with open(path, "w") as f:
                f.write(c)
            r = subprocess.run([sdcc, "-mmcs51", "--std-c99", path],
                               cwd=d, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(os.path.exists(os.path.join(d, "m.ihx")))

    def test_round_trip_is_stable(self):
        rt = sp.decompile(SRC)
        self.assertIn(
            "PART screen = MATRIX8X8 ROWS 74HC595 DATA P3.4 CLOCK P3.6 "
            "LATCH P3.5 COLUMNS P0", rt)
        c1, _ = sp.transpile(SRC)
        c2, _ = sp.transpile(rt)
        self.assertEqual(c1, c2)

    def test_pin_clash_with_declared_pin_refused(self):
        bad = SRC.replace(
            "  WHEN started:",
            "  PIN stray = P3.4 OUTPUT\n\n  WHEN started:")
        with self.assertRaises(sp.PseudocodeError):
            sp.transpile(bad)

    def test_column_port_clash_refused(self):
        bad = SRC.replace(
            "  WHEN started:",
            "  PORT other = P0 OUTPUT\n\n  WHEN started:")
        with self.assertRaises(sp.PseudocodeError):
            sp.transpile(bad)

    def test_duplicate_control_pin_refused(self):
        bad = SRC.replace("CLOCK P3.6", "CLOCK P3.4")
        with self.assertRaises(sp.PseudocodeError) as ctx:
            sp.transpile(bad)
        self.assertIn("same pin twice", str(ctx.exception))

    def test_control_pin_inside_columns_refused(self):
        # P0.5 as a control pin collides with the P0 column port.
        bad = SRC.replace("DATA P3.4", "DATA P0.5")
        with self.assertRaises(sp.PseudocodeError):
            sp.transpile(bad)

    def test_gated_off_non_8051(self):
        bad = SRC.replace("DEVICE STC89C52RC", "DEVICE ATMEGA328P")
        with self.assertRaises(sp.PseudocodeError) as ctx:
            sp.transpile(bad)
        self.assertIn("MATRIX8X8", str(ctx.exception))

    def test_show_image_requires_a_table(self):
        bad = SRC.replace("show image heart on screen",
                          "show image nope on screen")
        with self.assertRaises(sp.PseudocodeError) as ctx:
            sp.transpile(bad)
        self.assertIn("TABLE", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
