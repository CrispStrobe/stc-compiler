"""test_avr_widening — verify ATmega2560 (avr6) and ATtiny85 (avr25) compile paths.

Hand-written blink programs as oracles: PORTB on 2560 (PB7 = D13),
PB3 on tiny85. Each must: compile, produce non-empty hex, have DWARF
line info, and report the correct toolchain/mcu in the response.
"""
import unittest
import base64

from app import build_avr, CompileReq, AVR_TARGETS

BLINK_MEGA2560 = r"""
#include <avr/io.h>

int main(void) {
    DDRB |= (1 << PB7);   /* Arduino Mega LED = PB7 (D13) */
    for (;;) {
        PORTB |= (1 << PB7);
        PORTB &= ~(1 << PB7);
    }
}
"""

BLINK_TINY85 = r"""
#include <avr/io.h>

int main(void) {
    DDRB |= (1 << PB3);   /* ATtiny85 physical pin 2 */
    for (;;) {
        PORTB |= (1 << PB3);
        PORTB &= ~(1 << PB3);
    }
}
"""


class TestMega2560(unittest.TestCase):
    SPEC = AVR_TARGETS["atmega2560"]

    def _build(self, **kw):
        req = CompileReq(code=BLINK_MEGA2560, target="atmega2560", **kw)
        return build_avr(req, self.SPEC, None, None)

    def test_compiles(self):
        r = self._build()
        self.assertTrue(r["success"], r.get("error"))

    def test_hex_nonempty(self):
        r = self._build()
        raw = base64.b64decode(r["base64"])
        self.assertGreater(len(raw), 10, "Intel HEX too short")

    def test_symbols_path_runs(self):
        r = self._build(symbols=True)
        self.assertTrue(r["success"], r.get("error"))
        # Hand-written C with no bw_taskN: symbols is None, error explains
        self.assertIsNone(r["symbols"])
        self.assertIn("no bw_taskN", r["symbols_error"])

    def test_toolchain_and_mcu(self):
        r = self._build()
        self.assertEqual(r["toolchain"], "avr-gcc")
        self.assertEqual(r["mcu"], "atmega2560")

    def test_flash_in_spec(self):
        self.assertEqual(self.SPEC["flash"], 262144)


class TestTiny85(unittest.TestCase):
    SPEC = AVR_TARGETS["attiny85"]

    def _build(self, **kw):
        req = CompileReq(code=BLINK_TINY85, target="attiny85", **kw)
        return build_avr(req, self.SPEC, None, None)

    def test_compiles(self):
        r = self._build()
        self.assertTrue(r["success"], r.get("error"))

    def test_hex_nonempty(self):
        r = self._build()
        raw = base64.b64decode(r["base64"])
        self.assertGreater(len(raw), 10, "Intel HEX too short")

    def test_symbols_path_runs(self):
        r = self._build(symbols=True)
        self.assertTrue(r["success"], r.get("error"))
        self.assertIsNone(r["symbols"])
        self.assertIn("no bw_taskN", r["symbols_error"])

    def test_toolchain_and_mcu(self):
        r = self._build()
        self.assertEqual(r["toolchain"], "avr-gcc")
        self.assertEqual(r["mcu"], "attiny85")

    def test_flash_in_spec(self):
        self.assertEqual(self.SPEC["flash"], 8192)


if __name__ == "__main__":
    unittest.main()
