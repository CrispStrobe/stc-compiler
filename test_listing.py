"""test_listing — smoke tests for the {asm, lineMap, format, v} artifact.

Each toolchain gets a hand-written blink, compiled with disassemble=True.
Assertions:
  1. listing is present and has version 1
  2. asm text contains "main"
  3. lineMap is non-empty and resolves a known source line
  4. format matches the toolchain id
"""
import unittest

from app import build_avr, build_arm, CompileReq, AVR_TARGETS, ARM_TARGETS

AVR_BLINK = r"""
#include <avr/io.h>
int main(void) {
    DDRB |= (1 << PB5);
    for (;;) {
        PORTB |= (1 << PB5);
        PORTB &= ~(1 << PB5);
    }
}
"""

ARM_BLINK = r"""
#include <stdint.h>
#define SIO_BASE        0xd0000000
#define SIO_GPIO_OUT_SET (*(volatile uint32_t *)(SIO_BASE + 0x14))
#define SIO_GPIO_OE_SET (*(volatile uint32_t *)(SIO_BASE + 0x24))

int main(void) {
    SIO_GPIO_OE_SET = (1u << 25);
    for (;;) {
        SIO_GPIO_OUT_SET = (1u << 25);
    }
}
"""

SDCC_BLINK = r"""
#include <stc12.h>
#define LED P1_0
void main(void) {
    for (;;) {
        LED = 0;
        LED = 1;
    }
}
"""


class TestAvrListing(unittest.TestCase):
    def _build(self):
        req = CompileReq(code=AVR_BLINK, target="atmega328p", disassemble=True)
        return build_avr(req, AVR_TARGETS["atmega328p"], None, None)

    def test_listing_present(self):
        r = self._build()
        self.assertTrue(r["success"], r.get("error"))
        self.assertIsNotNone(r.get("listing"))
        self.assertEqual(r["listing"]["v"], 1)

    def test_asm_contains_main(self):
        r = self._build()
        self.assertIn("main", r["listing"]["asm"])

    def test_linemap_nonempty(self):
        r = self._build()
        lm = r["listing"]["lineMap"]
        self.assertGreater(len(lm), 0)
        # Every entry has addr, file, line
        for entry in lm:
            self.assertIn("addr", entry)
            self.assertIn("file", entry)
            self.assertIn("line", entry)

    def test_linemap_resolves_main_line(self):
        r = self._build()
        lm = r["listing"]["lineMap"]
        # main.c line 3 (int main) should be in the map
        main_lines = [e for e in lm if e["file"] == "main.c"]
        self.assertGreater(len(main_lines), 0,
                           "no main.c entries in lineMap")

    def test_format(self):
        r = self._build()
        self.assertEqual(r["listing"]["format"], "avr-gcc")


class TestArmListing(unittest.TestCase):
    def _build(self):
        req = CompileReq(code=ARM_BLINK, target="rp2040", disassemble=True)
        return build_arm(req, ARM_TARGETS["rp2040"], None)

    def test_listing_present(self):
        r = self._build()
        self.assertTrue(r["success"], r.get("error"))
        self.assertIsNotNone(r.get("listing"))
        self.assertEqual(r["listing"]["v"], 1)

    def test_asm_contains_main(self):
        r = self._build()
        self.assertIn("main", r["listing"]["asm"])

    def test_linemap_nonempty(self):
        r = self._build()
        lm = r["listing"]["lineMap"]
        self.assertGreater(len(lm), 0)

    def test_linemap_resolves_main_line(self):
        r = self._build()
        lm = r["listing"]["lineMap"]
        main_lines = [e for e in lm if e["file"] == "main.c"]
        self.assertGreater(len(main_lines), 0,
                           "no main.c entries in lineMap")

    def test_format(self):
        r = self._build()
        self.assertEqual(r["listing"]["format"], "arm-gcc")


class TestSdccListing(unittest.TestCase):
    def _build(self):
        from app import build
        req = CompileReq(code=SDCC_BLINK, target="stc12c5a60s2",
                         disassemble=True)
        return build(req)

    def test_listing_present(self):
        r = self._build()
        self.assertTrue(r["success"], r.get("error"))
        self.assertIsNotNone(r.get("listing"))
        self.assertEqual(r["listing"]["v"], 1)

    def test_asm_contains_main(self):
        r = self._build()
        self.assertIn("main", r["listing"]["asm"])

    def test_linemap_nonempty(self):
        r = self._build()
        lm = r["listing"]["lineMap"]
        self.assertGreater(len(lm), 0)

    def test_linemap_resolves_source_line(self):
        r = self._build()
        lm = r["listing"]["lineMap"]
        # Should reference the source file
        files = {e["file"] for e in lm}
        self.assertTrue(any("main" in f for f in files),
                        f"no main-related file in lineMap files: {files}")

    def test_format(self):
        r = self._build()
        self.assertEqual(r["listing"]["format"], "sdcc")


if __name__ == "__main__":
    unittest.main()
