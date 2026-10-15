"""test_stm32_startup — the STM32 flash images initialise RAM like firmware.

On silicon SRAM is garbage at power-on, so an image whose reset vector
points straight at main runs with initialised globals holding noise and
"zero" globals not zero. stm32-startup.S supplies a Reset_Handler that
copies .data from flash and zeroes .bss before main; the linker scripts
lay .data out AT> FLASH and keep a stack reserve free above .bss.

No Cortex-M system emulator is available in CI, so the startup is
verified statically from the built image and its disassembly:

  * word 0 is the end of SRAM, word 1 is Reset_Handler (not main);
  * Reset_Handler's literal pool names _sidata/_sdata/_edata/_sbss/_ebss,
    and those resolve to a 4-byte .data VMA range at the start of SRAM, a
    .bss range after it, and a flash LMA inside the image that holds the
    initialiser's bytes;
  * Reset_Handler calls main;
  * the program's own .vectors table follows the startup table and the
    other slots dispatch through bw_dispatch;
  * a program whose globals eat into the stack reserve fails to link with
    the linker-script message;
  * the response's mcu names the chip.
"""
import base64
import re
import unittest

from app import ARM_TARGETS, CompileReq, build_arm

FLASH = 0x08000000
SRAM = 0x20000000

CHIPS = {
    # chip: (SRAM bytes, stack reserve, vector table slots, table name)
    "stm32f030": (4 * 1024, 512, 48),
    "stm32g030": (8 * 1024, 1024, 48),
    "stm32f103": (20 * 1024, 1024, 76),
}

INIT_VALUE = 0xC0FFEE42

PROGRAM = """
#include <stdint.h>
volatile uint32_t bw_initialised = 0xC0FFEE42u;
volatile uint32_t bw_zeroed;
static void bw_fault(void) { for (;;) { } }
static void bw_tick(void) { bw_zeroed++; }
int main(void)
{
    for (;;) { bw_zeroed += bw_initialised; }
}
__attribute__((section(".vectors"), used))
const void *bw_vectors[48] = {
    (void *)0x20001000, (void *)main, [3] = (void *)bw_fault,
    [16 + 16] = (void *)bw_tick,
};
"""


def build(chip, code=PROGRAM, disassemble=True):
    req = CompileReq(code=code, target=chip, disassemble=disassemble)
    return build_arm(req, ARM_TARGETS[chip], None)


def symbol(listing, name):
    m = re.search(r"^([0-9a-f]{8}) <" + re.escape(name) + r">:", listing, re.M)
    return int(m.group(1), 16) if m else None


def words(blob, addr, count):
    off = addr - FLASH
    return [int.from_bytes(blob[off + 4 * i: off + 4 * i + 4], "little")
            for i in range(count)]


class TestStm32Startup(unittest.TestCase):
    def check_chip(self, chip):
        sram, reserve, slots = CHIPS[chip]
        r = build(chip)
        self.assertTrue(r["success"], r.get("error"))
        self.assertEqual(r["mcu"], chip)
        blob = base64.b64decode(r["base64"])
        listing = r["disassembly"]

        reset = symbol(listing, "Reset_Handler")
        main = symbol(listing, "main")
        dispatch = symbol(listing, "bw_dispatch")
        prog_table = symbol(listing, "bw_vectors")
        self.assertIsNotNone(reset, "startup Reset_Handler is linked")
        self.assertIsNotNone(dispatch)
        self.assertEqual(r["entry"] & ~1, reset, "ELF entry is Reset_Handler")

        table = words(blob, FLASH, slots)
        self.assertEqual(table[0], SRAM + sram, "initial SP = end of SRAM")
        self.assertEqual(table[1], reset | 1, "reset vector is Reset_Handler")
        self.assertNotEqual(table[1] & ~1, main, "reset no longer jumps to main")
        self.assertTrue(all(w == dispatch | 1 for w in table[2:]),
                        "every other slot dispatches to the program's table")
        self.assertEqual(prog_table, FLASH + 4 * slots,
                         "program's .vectors follows the startup table")
        self.assertEqual(words(blob, prog_table, 33)[32] & ~1,
                         symbol(listing, "bw_tick"),
                         "program's IRQ handler is still in its table")

        # Reset_Handler body: copy loop, zero loop, bl main.
        body = listing[listing.index("<Reset_Handler>:"):
                       listing.index("<bw_dispatch>:")]
        self.assertRegex(body, r"bl\s+[0-9a-f]+ <main>")
        self.assertRegex(body, r"str\s+r3, \[r1\]")
        self.assertRegex(body, r"movs\s+r3, #0")
        # Its five literals, in load order.
        lits = [int(x, 16) for x in
                re.findall(r"ldr\s+r\d, \[pc, #\d+\]\s+; \(([0-9a-f]+)", body)]
        self.assertEqual(len(lits), 5, body)
        sidata, sdata, edata, sbss, ebss = (words(blob, a, 1)[0] for a in lits)

        self.assertEqual(sdata, SRAM, ".data starts SRAM")
        self.assertEqual(edata - sdata, 4, "one initialised word in .data")
        self.assertEqual(sbss, edata)
        self.assertGreaterEqual(ebss - sbss, 4, "the zero global is in .bss")
        self.assertLessEqual(ebss + reserve, SRAM + sram)
        self.assertTrue(FLASH <= sidata and sidata + 4 <= FLASH + len(blob),
                        ".data's load image is inside the flash image")
        self.assertEqual(words(blob, sidata, 1)[0], INIT_VALUE,
                         "the initialiser's bytes are in flash at _sidata")
        self.assertEqual(sidata + 4, FLASH + len(blob),
                         ".data image ends the binary (no SRAM-sized gap)")

    def test_stm32f030(self):
        self.check_chip("stm32f030")

    def test_stm32g030(self):
        self.check_chip("stm32g030")

    def test_stm32f103(self):
        self.check_chip("stm32f103")

    def test_stack_reserve_overflow_fails_at_link(self):
        for chip, (sram, reserve, _) in CHIPS.items():
            with self.subTest(chip=chip):
                # Fits SRAM but leaves less than the reserve for the stack.
                n = (sram - reserve) // 4 + 8
                code = (f"#include <stdint.h>\nvolatile uint32_t big[{n}];\n"
                        "int main(void) { for (;;) { big[0]++; } }\n")
                r = build(chip, code, disassemble=False)
                self.assertFalse(r["success"])
                self.assertIn(f"{reserve}-byte stack reserve", r["error"])

                ok = (f"#include <stdint.h>\nvolatile uint32_t big[{n - 16}];\n"
                      "int main(void) { for (;;) { big[0]++; } }\n")
                r = build(chip, ok, disassemble=False)
                self.assertTrue(r["success"], r.get("error"))

    def test_rp2040_unchanged(self):
        code = "int main(void) { for (;;) { } }\n"
        r = build_arm(CompileReq(code=code, target="rp2040", disassemble=True),
                      ARM_TARGETS["rp2040"], None)
        self.assertTrue(r["success"], r.get("error"))
        self.assertEqual(r["mcu"], "rp2040")
        self.assertEqual(r["entry"] & ~1, SRAM)
        self.assertNotIn("Reset_Handler", r["disassembly"])


if __name__ == "__main__":
    unittest.main()
