"""Tests for display-peripheral verb families (LCD, TFT, OLED, RGB).

Covers: parsing, pseudocode round-trip, C emission, and the verb-name
coordination with sb3-creator's C function signatures.
"""

import pytest
import stc_pseudocode as sp


# ---- fixtures ---------------------------------------------------------------

LCD_PROGRAM = """\
DEVICE STC12C5A60S2:
  CLOCK 11059200

  WHEN started:
    lcd clear screen1
    lcd set cursor 0 0 on screen1
    lcd print "Hello" on screen1
    lcd print counter on screen1
"""

TFT_PROGRAM = """\
DEVICE STC12C5A60S2:
  CLOCK 11059200

  WHEN started:
    tft pixel 10 20 R 255 G 0 B 128 on disp
    tft fill 0 0 240 320 R 0 G 0 B 0 on disp
    tft clear disp
    tft set cursor 1 5 on disp
    tft print "test" on disp
    tft print score on disp
"""

OLED_PROGRAM = """\
DEVICE STC12C5A60S2:
  CLOCK 11059200

  WHEN started:
    oled pixel 5 10 1 on oled1
    oled clear oled1
    oled set cursor 2 3 on oled1
    oled print "hi" on oled1
    oled print value on oled1
"""

RGB_PROGRAM = """\
DEVICE STC12C5A60S2:
  CLOCK 11059200

  WHEN started:
    set led1 colour to R 255 G 128 B 0
"""

MULTI_DISPLAY = """\
DEVICE STC12C5A60S2:
  CLOCK 11059200

  WHEN started:
    lcd clear screen1
    tft clear disp
    oled clear oled1
    set rgb1 colour to R 0 G 255 B 0
"""


# ---- LCD parse ---------------------------------------------------------------

class TestLcdParse:
    def test_lcd_clear(self):
        program = sp.parse(LCD_PROGRAM)
        stmt = program.whens[0][0]
        assert isinstance(stmt, sp.LcdClear)
        assert stmt.display == "screen1"

    def test_lcd_cursor(self):
        program = sp.parse(LCD_PROGRAM)
        stmt = program.whens[0][1]
        assert isinstance(stmt, sp.LcdCursor)
        assert stmt.display == "screen1"
        assert isinstance(stmt.row, sp.Num) and stmt.row.value == 0
        assert isinstance(stmt.col, sp.Num) and stmt.col.value == 0

    def test_lcd_print_string(self):
        program = sp.parse(LCD_PROGRAM)
        stmt = program.whens[0][2]
        assert isinstance(stmt, sp.LcdPrint)
        assert stmt.text == "Hello"
        assert stmt.value is None

    def test_lcd_print_expression(self):
        program = sp.parse(LCD_PROGRAM)
        stmt = program.whens[0][3]
        assert isinstance(stmt, sp.LcdPrint)
        assert stmt.text is None
        assert isinstance(stmt.value, sp.Var)


# ---- TFT parse ---------------------------------------------------------------

class TestTftParse:
    def test_tft_pixel(self):
        program = sp.parse(TFT_PROGRAM)
        stmt = program.whens[0][0]
        assert isinstance(stmt, sp.TftPixel)
        assert stmt.display == "disp"
        assert stmt.x.value == 10
        assert stmt.r.value == 255

    def test_tft_fill(self):
        program = sp.parse(TFT_PROGRAM)
        stmt = program.whens[0][1]
        assert isinstance(stmt, sp.TftFill)
        assert stmt.w.value == 240 and stmt.h.value == 320

    def test_tft_clear(self):
        program = sp.parse(TFT_PROGRAM)
        stmt = program.whens[0][2]
        assert isinstance(stmt, sp.TftClear)

    def test_tft_cursor(self):
        program = sp.parse(TFT_PROGRAM)
        stmt = program.whens[0][3]
        assert isinstance(stmt, sp.TftCursor)
        assert stmt.row.value == 1 and stmt.col.value == 5

    def test_tft_print_string(self):
        program = sp.parse(TFT_PROGRAM)
        stmt = program.whens[0][4]
        assert isinstance(stmt, sp.TftPrint)
        assert stmt.text == "test"

    def test_tft_print_expression(self):
        program = sp.parse(TFT_PROGRAM)
        stmt = program.whens[0][5]
        assert isinstance(stmt, sp.TftPrint)
        assert isinstance(stmt.value, sp.Var)


# ---- OLED parse --------------------------------------------------------------

class TestOledParse:
    def test_oled_pixel(self):
        program = sp.parse(OLED_PROGRAM)
        stmt = program.whens[0][0]
        assert isinstance(stmt, sp.OledPixel)
        assert stmt.x.value == 5 and stmt.y.value == 10
        assert stmt.value.value == 1

    def test_oled_clear(self):
        program = sp.parse(OLED_PROGRAM)
        stmt = program.whens[0][1]
        assert isinstance(stmt, sp.OledClear)

    def test_oled_cursor(self):
        program = sp.parse(OLED_PROGRAM)
        stmt = program.whens[0][2]
        assert isinstance(stmt, sp.OledCursor)

    def test_oled_print_string(self):
        program = sp.parse(OLED_PROGRAM)
        stmt = program.whens[0][3]
        assert isinstance(stmt, sp.OledPrint)
        assert stmt.text == "hi"

    def test_oled_print_expression(self):
        program = sp.parse(OLED_PROGRAM)
        stmt = program.whens[0][4]
        assert isinstance(stmt, sp.OledPrint)
        assert isinstance(stmt.value, sp.Var)


# ---- RGB parse ----------------------------------------------------------------

class TestRgbParse:
    def test_rgb_set(self):
        program = sp.parse(RGB_PROGRAM)
        stmt = program.whens[0][0]
        assert isinstance(stmt, sp.RgbSet)
        assert stmt.led == "led1"
        assert stmt.r.value == 255
        assert stmt.g.value == 128
        assert stmt.b.value == 0


# ---- pseudocode round-trip ----------------------------------------------------

class TestDisplayRoundTrip:
    def test_lcd_round_trip(self):
        p1 = sp.emit_pseudocode(sp.parse(LCD_PROGRAM))
        p2 = sp.emit_pseudocode(sp.parse(p1))
        assert p1 == p2

    def test_tft_round_trip(self):
        p1 = sp.emit_pseudocode(sp.parse(TFT_PROGRAM))
        p2 = sp.emit_pseudocode(sp.parse(p1))
        assert p1 == p2

    def test_oled_round_trip(self):
        p1 = sp.emit_pseudocode(sp.parse(OLED_PROGRAM))
        p2 = sp.emit_pseudocode(sp.parse(p1))
        assert p1 == p2

    def test_rgb_round_trip(self):
        p1 = sp.emit_pseudocode(sp.parse(RGB_PROGRAM))
        p2 = sp.emit_pseudocode(sp.parse(p1))
        assert p1 == p2

    def test_multi_display_round_trip(self):
        p1 = sp.emit_pseudocode(sp.parse(MULTI_DISPLAY))
        p2 = sp.emit_pseudocode(sp.parse(p1))
        assert p1 == p2


# ---- C emission ---------------------------------------------------------------

class TestDisplayCEmit:
    def test_lcd_clear_emits(self):
        c = sp.emit(sp.parse(LCD_PROGRAM))
        assert "bw_lcd_clear(screen1);" in c

    def test_lcd_cursor_emits(self):
        c = sp.emit(sp.parse(LCD_PROGRAM))
        assert "bw_lcd_cursor(screen1, 0, 0);" in c

    def test_lcd_print_string_emits(self):
        c = sp.emit(sp.parse(LCD_PROGRAM))
        assert 'bw_lcd_print_s(screen1, "Hello");' in c

    def test_lcd_print_expression_emits(self):
        c = sp.emit(sp.parse(LCD_PROGRAM))
        assert "bw_lcd_print_n(screen1, counter);" in c

    def test_tft_pixel_emits(self):
        c = sp.emit(sp.parse(TFT_PROGRAM))
        assert "bw_tft_pixel(disp, 10, 20, 255, 0, 128);" in c

    def test_tft_fill_emits(self):
        c = sp.emit(sp.parse(TFT_PROGRAM))
        assert "bw_tft_fill(disp, 0, 0, 240, 320, 0, 0, 0);" in c

    def test_tft_clear_emits(self):
        c = sp.emit(sp.parse(TFT_PROGRAM))
        assert "bw_tft_clear(disp);" in c

    def test_tft_cursor_emits(self):
        c = sp.emit(sp.parse(TFT_PROGRAM))
        assert "bw_tft_cursor(disp, 1, 5);" in c

    def test_tft_print_string_emits(self):
        c = sp.emit(sp.parse(TFT_PROGRAM))
        assert 'bw_tft_print_s(disp, "test");' in c

    def test_tft_print_expression_emits(self):
        c = sp.emit(sp.parse(TFT_PROGRAM))
        assert "bw_tft_print_n(disp, score);" in c

    def test_oled_pixel_emits(self):
        c = sp.emit(sp.parse(OLED_PROGRAM))
        assert "bw_oled_pixel(oled1, 5, 10, 1);" in c

    def test_oled_clear_emits(self):
        c = sp.emit(sp.parse(OLED_PROGRAM))
        assert "bw_oled_clear(oled1);" in c

    def test_oled_cursor_emits(self):
        c = sp.emit(sp.parse(OLED_PROGRAM))
        assert "bw_oled_cursor(oled1, 2, 3);" in c

    def test_oled_print_string_emits(self):
        c = sp.emit(sp.parse(OLED_PROGRAM))
        assert 'bw_oled_print_s(oled1, "hi");' in c

    def test_oled_print_expression_emits(self):
        c = sp.emit(sp.parse(OLED_PROGRAM))
        assert "bw_oled_print_n(oled1, value);" in c

    def test_rgb_set_emits(self):
        c = sp.emit(sp.parse(RGB_PROGRAM))
        assert "bw_rgb_set(led1, 255, 128, 0);" in c

    def test_multi_display_all_present(self):
        c = sp.emit(sp.parse(MULTI_DISPLAY))
        assert "bw_lcd_clear(screen1);" in c
        assert "bw_tft_clear(disp);" in c
        assert "bw_oled_clear(oled1);" in c
        assert "bw_rgb_set(rgb1, 0, 255, 0);" in c


# ---- C emission in cooperative scheduler (stmts_task) -------------------------

class TestDisplayCEmitTask:
    """Display verbs in multi-WHEN programs use stmts_task, not stmts_c."""

    MULTI_WHEN = """\
DEVICE STC12C5A60S2:
  CLOCK 11059200
  PIN led = P1.0 OUTPUT

  WHEN started:
    lcd clear screen1
    tft pixel 0 0 R 255 G 0 B 0 on disp
    oled clear oled1
    set rgb1 colour to R 0 G 0 B 255
    FOREVER:
      wait 1 seconds

  WHEN started:
    tft clear disp
    FOREVER:
      wait 1 seconds
"""

    def test_task_lcd_clear(self):
        c = sp.emit(sp.parse(self.MULTI_WHEN))
        assert "bw_lcd_clear(screen1);" in c

    def test_task_tft_pixel(self):
        c = sp.emit(sp.parse(self.MULTI_WHEN))
        assert "bw_tft_pixel(disp, 0, 0, 255, 0, 0);" in c

    def test_task_oled_clear(self):
        c = sp.emit(sp.parse(self.MULTI_WHEN))
        assert "bw_oled_clear(oled1);" in c

    def test_task_rgb_set(self):
        c = sp.emit(sp.parse(self.MULTI_WHEN))
        assert "bw_rgb_set(rgb1, 0, 0, 255);" in c

    def test_task_tft_clear(self):
        c = sp.emit(sp.parse(self.MULTI_WHEN))
        assert "bw_tft_clear(disp);" in c
