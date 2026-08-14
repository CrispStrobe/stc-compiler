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


# ---- ARM / nRF52833 (arm-none-eabi-gcc) ------------------------------------

# Minimal vector table: initial SP + Reset_Handler → infinite loop.
# nRF52833 memory map: flash at 0x0, RAM at 0x20000000, 128K RAM.
VECTOR_LOOP_NRF = """\
.syntax unified
.cpu cortex-m4
.thumb

.section .vectors, "a"
.word 0x20020000        @ initial SP (top of 128K RAM)
.word Reset_Handler     @ reset vector

.text
.global Reset_Handler
.type Reset_Handler, %function
Reset_Handler:
    b .                 @ infinite loop
"""

# GPIO row/col LED program for micro:bit V2.
# nRF52833 PS GPIO: P0 base = 0x50000000, PIN_CNF[n] at offset 0x700+4*n,
# OUT at 0x504.
# micro:bit V2 LED matrix: ROW1=P0.21, ROW2=P0.22, ..., ROW5=P0.25;
# COL1=P0.28, COL2=P0.11, COL3=P0.31, COL4=P1.05, COL5=P0.30.
# This lights the top-left LED: ROW1 (P0.21) high, COL1 (P0.28) low.
GPIO_LED_NRF = """\
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
    @ P0 GPIO base
    ldr r0, =0x50000000

    @ Configure P0.21 (ROW1) as output: PIN_CNF[21] = 1
    @ PIN_CNF base = 0x700, PIN_CNF[21] = 0x700 + 21*4 = 0x754
    movs r1, #1
    str r1, [r0, #0x54]     @ offset from 0x700 base — see below
    ldr r2, =0x50000754
    str r1, [r2]

    @ Configure P0.28 (COL1) as output: PIN_CNF[28] = 1
    @ PIN_CNF[28] = 0x700 + 28*4 = 0x770
    ldr r2, =0x50000770
    str r1, [r2]

    @ Set P0.21 high (ROW1 on), P0.28 low (COL1 sink)
    @ OUT register at offset 0x504
    ldr r2, =(1 << 21)      @ ROW1 bit
    ldr r3, =0x50000504
    str r2, [r3]

loop:
    b loop
"""

BAD_ARM = "bad instruction here"

CODAL_SOURCE = """\
.syntax unified
.thumb
    bl MicroBitDisplay_enable
"""


class TestArmAssemble(unittest.TestCase):
    def _arm(self):
        import os
        from app import stage_arm
        bin_dir = stage_arm()
        if bin_dir is None:
            self.skipTest("no ARM toolchain")
        deps = os.path.join(os.path.dirname(bin_dir), "lib-deps")
        env = dict(os.environ)
        if os.path.isdir(deps):
            env["LD_LIBRARY_PATH"] = deps + os.pathsep + env.get(
                "LD_LIBRARY_PATH", "")
        ld = os.path.join(os.path.dirname(__file__), "nrf52833.ld")
        return bin_dir, env, ld

    def test_vector_loop_assembles(self):
        bin_dir, env, ld = self._arm()
        r = assemble.assemble_arm(VECTOR_LOOP_NRF, "cortex-m4",
                                  bin_dir, env, ld)
        self.assertTrue(r["success"], r.get("log") or r.get("errors"))

    def test_vector_loop_is_ihex(self):
        bin_dir, env, ld = self._arm()
        r = assemble.assemble_arm(VECTOR_LOOP_NRF, "cortex-m4",
                                  bin_dir, env, ld)
        import base64
        content = base64.b64decode(r["base64"]).decode("ascii", errors="replace")
        self.assertTrue(content.startswith(":"),
                        "output should be Intel HEX (starts with ':')")

    def test_vector_loop_hex_nonempty(self):
        bin_dir, env, ld = self._arm()
        r = assemble.assemble_arm(VECTOR_LOOP_NRF, "cortex-m4",
                                  bin_dir, env, ld)
        self.assertGreater(r["bytes"], 0)

    def test_gpio_led_assembles(self):
        bin_dir, env, ld = self._arm()
        r = assemble.assemble_arm(GPIO_LED_NRF, "cortex-m4",
                                  bin_dir, env, ld)
        self.assertTrue(r["success"], r.get("log") or r.get("errors"))

    def test_gpio_led_contains_reset(self):
        bin_dir, env, ld = self._arm()
        r = assemble.assemble_arm(GPIO_LED_NRF, "cortex-m4",
                                  bin_dir, env, ld)
        self.assertIsNotNone(r.get("listing"))
        self.assertIn("Reset_Handler", r["listing"]["asm"])

    def test_listing_present(self):
        bin_dir, env, ld = self._arm()
        r = assemble.assemble_arm(VECTOR_LOOP_NRF, "cortex-m4",
                                  bin_dir, env, ld)
        self.assertIsNotNone(r.get("listing"))
        self.assertEqual(r["listing"]["v"], 1)

    def test_syntax_error_line_accurate(self):
        bin_dir, env, ld = self._arm()
        r = assemble.assemble_arm(BAD_ARM, "cortex-m4", bin_dir, env, ld)
        self.assertFalse(r["success"])
        self.assertGreater(len(r["errors"]), 0)
        self.assertEqual(r["errors"][0]["line"], 1)

    def test_codal_softdevice_rejected(self):
        bin_dir, env, ld = self._arm()
        r = assemble.assemble_arm(CODAL_SOURCE, "cortex-m4",
                                  bin_dir, env, ld)
        self.assertFalse(r["success"])
        self.assertIn("CODAL", r["errors"][0]["message"])

    def test_toolchain_field(self):
        bin_dir, env, ld = self._arm()
        r = assemble.assemble_arm(VECTOR_LOOP_NRF, "cortex-m4",
                                  bin_dir, env, ld)
        self.assertEqual(r["toolchain"], "arm-none-eabi-gcc")

    def test_health_lists_nrf52833(self):
        from app import ASSEMBLE_TARGETS
        self.assertIn("nrf52833", ASSEMBLE_TARGETS)
        self.assertEqual(ASSEMBLE_TARGETS["nrf52833"], "arm")


if __name__ == "__main__":
    unittest.main()
