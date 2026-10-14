"""test_esp8266_build — the `esp8266` target, end to end through build().

C sketches compile with the vendored xtensa-lx106-gcc/ bundle against the
blob-free bw8266 runtime (esp8266/), and come back as an ESP8266 flash image.
Checks:
  1. a blink sketch and the fork's own button/wave sketches build through
     build(); the image has the 0xE9 boot header, a segment count matching its
     segment table, an entry inside an instruction-RAM segment, and a valid
     checksum
  2. esp8266_image.py is byte-identical to the fork's tools/elf2image.js
     (vendored as scripts/fixtures/esp8266/elf2image.js), run with real node,
     on every ELF built here -- including one with flash-mapped (irom) code
  3. a compile error is the program's (stage 'compile', gcc log returned)
  4. path options and the Keil dialect are refused

Structural, not an execution test: the image boots in the esp8266js emulator,
whose own CI runs these sketches.

In CI the bundle and node are required: a missing one FAILS rather than skips.
"""
import base64
import os
import shutil
import struct
import subprocess
import tempfile
import unittest

import esp8266_image
from app import (build, build_esp8266, CompileReq, ESP8266_TARGETS,
                 SRC_ESP8266_RUNTIME, stage_xtensa_gcc)

HERE = os.path.dirname(os.path.abspath(__file__))
FIX = os.path.join(HERE, "scripts", "fixtures", "esp8266")
IN_CI = bool(os.environ.get("CI"))

BLINK = r"""
#include <Arduino.h>
void setup(void) { pinMode(LED_BUILTIN, OUTPUT); }
void loop(void) {
  digitalWrite(LED_BUILTIN, HIGH); delay(500);
  digitalWrite(LED_BUILTIN, LOW);  delay(500);
}
"""

# Code placed in flash (the irom region): exercises the flash-mapped half of
# the image, at its flash offset after the boot image.
IROM = r"""
#include <Arduino.h>
__attribute__((section(".irom.text"), noinline)) int twice(int x) { return 2 * x; }
static volatile int sink;
void setup(void) { bw_serial_begin(115200); sink = twice(21); }
void loop(void) {}
"""

_BIN = stage_xtensa_gcc()
if IN_CI and _BIN is None:
    raise RuntimeError("CI: the xtensa-lx106-gcc bundle did not stage")


def _req(code, **kw):
    return CompileReq(code=code, language="c", target="esp8266", **kw)


def _parse(img):
    """Walk the boot image; return (entry, [(addr, len)], checksum_ok)."""
    assert img[0] == 0xE9, "boot image magic"
    count = img[1]
    entry = struct.unpack_from("<I", img, 4)[0]
    at, segs, csum = 8, [], 0xEF
    for _ in range(count):
        addr, n = struct.unpack_from("<II", img, at)
        data = img[at + 8:at + 8 + n]
        assert len(data) == n and n % 4 == 0
        for b in data:
            csum ^= b
        segs.append((addr, n))
        at += 8 + n
    at += 16 - (at % 16)
    return entry, segs, img[at - 1] == csum


@unittest.skipUnless(_BIN is not None, "no xtensa-lx106-gcc bundle/toolchain")
class Esp8266Build(unittest.TestCase):
    def _ok(self, code):
        r = build(_req(code))
        self.assertTrue(r["success"], r.get("error"))
        self.assertEqual(r["toolchain"], "xtensa-lx106-elf-gcc")
        self.assertEqual(r["mcu"], "lx106")
        self.assertEqual(r["filename"], "main.bin")
        img = base64.b64decode(r["base64"])
        self.assertEqual(len(img), r["bytes"])
        entry, segs, csum_ok = _parse(img)
        self.assertEqual(entry, r["entry"])
        self.assertGreaterEqual(len(segs), 1)
        self.assertTrue(csum_ok, "boot image checksum")
        self.assertTrue(any(a <= entry < a + n and 0x40100000 <= a < 0x40108000
                            for a, n in segs), "entry is in an IRAM segment")
        self.assertIn("ESP8266 image", r["memory"])
        return r, img

    def test_blink(self):
        self._ok(BLINK)

    def test_fork_button_sketch(self):
        with open(os.path.join(FIX, "button.c")) as fh:
            self._ok(fh.read())

    def test_fork_wave_sketch(self):
        with open(os.path.join(FIX, "wave.c")) as fh:
            self._ok(fh.read())

    def test_flash_mapped_code_lands_at_its_flash_offset(self):
        _r, img = self._ok(IROM)
        self.assertGreater(len(img), 0x10000, "irom code is placed at flash offset 0x10000+")

    def test_a_compile_error_is_the_programs(self):
        r = build(_req("#include <Arduino.h>\nvoid setup(void) { this is not C; }\nvoid loop(void) {}\n"))
        self.assertFalse(r["success"])
        self.assertEqual(r["stage"], "compile")
        self.assertIn("error", r["log"])
        self.assertIn("main.c", r["log"])

    def test_warnings_are_errors(self):
        r = build(_req("#include <Arduino.h>\nvoid setup(void) { int unused; }\nvoid loop(void) {}\n"))
        self.assertFalse(r["success"])
        self.assertEqual(r["stage"], "compile")

    def test_path_options_are_refused(self):
        for opt in ("-I/etc", "-L/tmp", "-T/etc/passwd", "@/etc/passwd"):
            r = build_esp8266(_req(BLINK, options=[opt]), ESP8266_TARGETS["esp8266"], None)
            self.assertFalse(r["success"], opt)
            self.assertEqual(r["stage"], "options", opt)
            self.assertEqual(build(_req(BLINK, options=[opt]))["stage"], "options", opt)

    def test_escaping_include_is_refused(self):
        r = build(_req('#include "../../etc/passwd"\nvoid setup(void){}\nvoid loop(void){}\n'))
        self.assertFalse(r["success"])

    def test_keil_dialect_is_refused(self):
        r = build(CompileReq(code="sbit LED = P1^0;\nvoid main(void){}\n",
                             language="keil", target="esp8266"))
        self.assertFalse(r["success"])
        self.assertIn("8051-only", r["error"])


@unittest.skipUnless(_BIN is not None, "no xtensa-lx106-gcc bundle/toolchain")
class ImageIdentity(unittest.TestCase):
    """esp8266_image.py vs the fork's elf2image.js, on real ELFs."""

    def setUp(self):
        self.node = shutil.which("node")
        if self.node is None:
            if IN_CI:
                self.fail("CI: node is required for the elf2image.js identity check")
            self.skipTest("node not available")

    def _elf(self, code, work):
        src = os.path.join(work, "s.c")
        with open(src, "w") as fh:
            fh.write(code)
        elf = os.path.join(work, "s.elf")
        rt = SRC_ESP8266_RUNTIME
        subprocess.run(
            [os.path.join(_BIN, "xtensa-lx106-elf-gcc"), "-Os", "-ffreestanding", "-nostdlib",
             "-nostartfiles", "-mlongcalls", "-mtext-section-literals", "-Wall", "-Wextra",
             "-Werror", "-I", rt, "-I", os.path.join(rt, "arduino"),
             "-T", os.path.join(rt, "bw8266.ld"), "-L", rt,
             os.path.join(rt, "runtime.c"), os.path.join(rt, "arduino", "arduino.c"),
             src, "-lgcc", "-o", elf], check=True, capture_output=True)
        return elf

    def _same(self, code):
        with tempfile.TemporaryDirectory() as work:
            elf = self._elf(code, work)
            out = os.path.join(work, "js.bin")
            subprocess.run([self.node, os.path.join(FIX, "elf2image.js"), elf, out],
                           check=True, capture_output=True)
            with open(out, "rb") as fh:
                js = fh.read()
            with open(elf, "rb") as fh:
                py = esp8266_image.elf2image(fh.read())["image"]
            self.assertEqual(py, js)
            return py

    def test_blink(self):
        self._same(BLINK)

    def test_button_and_wave(self):
        for name in ("button.c", "wave.c"):
            with open(os.path.join(FIX, name)) as fh:
                self._same(fh.read())

    def test_with_flash_mapped_code(self):
        self.assertGreater(len(self._same(IROM)), 0x10000)


class ImageErrors(unittest.TestCase):
    def test_not_an_elf(self):
        with self.assertRaises(ValueError):
            esp8266_image.elf2image(b"\x00" * 64)


if __name__ == "__main__":
    unittest.main()
