"""test_device_parity — every device sb3-creator knows must parse+compile here.

Milestone test per fleet doctrine: if a device that was working stops,
this test fails and the gap is visible immediately.
"""
import unittest

import stc_pseudocode as sp


def _blink(device, clock, pin, direction="OUTPUT", active_low=""):
    """Minimal blink program for any device."""
    al = " ACTIVE LOW" if active_low else ""
    return (f"DEVICE {device}\nCLOCK {clock}\n"
            f"PIN led = {pin} {direction}{al}\n\n"
            f"WHEN started:\n  FOREVER:\n    turn on led\n"
            f"    wait 0.5 seconds\n    turn off led\n"
            f"    wait 0.5 seconds\n")


class TestDeviceParity(unittest.TestCase):
    """Each test parses + emits C for one device. A failure means the
    hosted pseudocode front-end rejects a device sb3-creator accepts."""

    def _check(self, src, device_name):
        prog = sp.parse(src)
        self.assertEqual(prog.part, device_name.lower())
        c = sp.emit_c(prog)
        self.assertGreater(len(c), 100, f"{device_name} C too short")
        return c

    # ---- 8051 family ----
    def test_stc12c5a60s2(self):
        self._check(_blink("STC12C5A60S2", 11059200, "P1.0", active_low=True),
                     "stc12c5a60s2")

    def test_stc12c5a16s2(self):
        self._check(_blink("STC12C5A16S2", 11059200, "P1.0", active_low=True),
                     "stc12c5a16s2")

    def test_stc89c52rc(self):
        self._check(_blink("STC89C52RC", 11059200, "P1.0", active_low=True),
                     "stc89c52rc")

    def test_stc89c52(self):
        self._check(_blink("STC89C52", 11059200, "P1.0", active_low=True),
                     "stc89c52")

    def test_stc15f2k60s2(self):
        self._check(_blink("STC15F2K60S2", 11059200, "P1.0", active_low=True),
                     "stc15f2k60s2")

    def test_stc15w408as(self):
        self._check(_blink("STC15W408AS", 11059200, "P1.0", active_low=True),
                     "stc15w408as")

    # ---- Arduino family ----
    def test_arduino_uno(self):
        self._check(_blink("ARDUINO-UNO", 16000000, "D13"), "arduino-uno")

    def test_arduino_nano(self):
        self._check(_blink("ARDUINO-NANO", 16000000, "D13"), "arduino-nano")

    def test_arduino_mega(self):
        self._check(_blink("ARDUINO-MEGA", 16000000, "D13"), "arduino-mega")

    # ---- Bare AVR ----
    def test_atmega328p(self):
        self._check(_blink("ATMEGA328P", 16000000, "D13"), "atmega328p")

    def test_atmega168p(self):
        self._check(_blink("ATMEGA168P", 16000000, "D13"), "atmega168p")

    def test_attiny88(self):
        self._check(_blink("ATTINY88", 8000000, "PB0"), "attiny88")

    def test_attiny85(self):
        self._check(_blink("ATTINY85", 8000000, "PB3"), "attiny85")

    # ---- 6502 ----
    def test_eater6502_refuses_rather_than_emitting_the_wrong_architecture(self):
        """This device used to pass the check above -- because the check is
        len(c) > 100, and it was registered on the AVR generator, so it
        cheerfully produced <avr/io.h> and ISR(TIMER0_COMPA_vect) for a 65C02.

        There is no 6502 generator yet, so the right behaviour is to say so at
        the DEVICE line. See test_device_matrix.py, which holds every device
        to an outcome it has to declare."""
        with self.assertRaises(sp.PseudocodeError) as caught:
            sp.parse(_blink("EATER6502", 1000000, "PA0"))
        self.assertIn("no pseudocode generator", str(caught.exception))

    # ---- micro:bit + Pico ----
    def test_microbit(self):
        prog = sp.parse("DEVICE MICROBIT\nPIN led = P0 OUTPUT\n\n"
                         "WHEN started:\n  turn on led\n")
        self.assertEqual(prog.part, "microbit")

    def test_pico(self):
        prog = sp.parse("DEVICE PICO\nPIN led = GP25 OUTPUT\n\n"
                         "WHEN started:\n  turn on led\n")
        self.assertEqual(prog.part, "pico")

    # ---- DEVICE without colon (sb3-creator dialect) ----
    def test_device_without_colon(self):
        """sb3-creator writes DEVICE XXX without a trailing colon."""
        prog = sp.parse("DEVICE ATTINY88\nCLOCK 8000000\nPIN led = PB0 OUTPUT\n\n"
                         "WHEN started:\n  turn on led\n")
        self.assertEqual(prog.part, "attiny88")


if __name__ == "__main__":
    unittest.main()
