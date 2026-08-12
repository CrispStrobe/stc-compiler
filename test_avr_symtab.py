"""avr_symtab unit tests — fixtures hand-written from the documented formats
(`avr-nm -S`, `objdump --dwarf=decodedline`), plus the real generated C."""
import unittest

import avr_symtab
from stc_symtab import SymbolTableError

NM = """00800104 00000002 b bw_task0_state
00800106 00000002 b bw_task0_until
00800108 00000002 b bw_task1_state
0080010a 00000002 b bw_task1_until
00800100 00000004 b bw_ms
0080010c 00000002 b score
00000000 00000068 T __vectors
"""

DECODED = """Decoded dump of debug contents of section .debug_line:

CU: main.c:
File name                            Line number    Starting address    View    Stmt
main.c                                        90           0x100                x
main.c                                        95           0x10a                x
main.c                                        99           0x11e                x
main.c                                       104           0x132                x
main.c                                       112           0x150                x
main.c                                       116           0x162                x
"""

# A miniature of the real generated shape: two tasks, case labels on the
# lines the DWARF fixture names, and a matching @bw map.
C = """/* @bw-begin — machine-readable; do not hand-edit.
 * @bw yield bw_task0 0 blockA hat
 * @bw yield bw_task0 1 blockB forever
 * @bw yield bw_task0 2 blockC wait
 * @bw yield bw_task1 0 blockD hat
 * @bw yield bw_task1 1 blockE forever
 * @bw-end */
static void bw_task0(void)
{
    switch (bw_task0_state) {
    case 0:
    bw_task0_state = 1;
    case 1:
    PORTB |= (1 << 5);
    bw_task0_state = 2;
    case 2:
    if (0) return;
    }
}
static void bw_task1(void)
{
    switch (bw_task1_state) {
    case 0:
    bw_task1_state = 1;
    case 1:
    PINB = (1 << 5);
    }
}
"""


class ParseNm(unittest.TestCase):
    def test_strips_data_vma_and_keeps_sizes(self):
        syms = avr_symtab.parse_nm(NM)
        self.assertEqual(syms["bw_task0_state"], {"addr": 0x104, "size": 2, "kind": "b"})
        self.assertEqual(syms["bw_ms"]["addr"], 0x100)
        self.assertEqual(syms["bw_ms"]["size"], 4)
        self.assertEqual(syms["__vectors"]["addr"], 0, "code symbols keep their address")


class ParseDecoded(unittest.TestCase):
    def test_lowest_address_per_line_wins(self):
        text = DECODED + "main.c                                        95           0x1f0\n"
        lines = avr_symtab.parse_decodedline(text)
        self.assertEqual(lines[("main.c", 95)], 0x10a)


class Build(unittest.TestCase):
    def _case_lines(self):
        # The fixture C's case labels sit on real line numbers; map them to
        # the DWARF fixture by regenerating decodedline rows for those lines.
        import re
        rows = []
        addr = 0x100
        for i, line in enumerate(C.splitlines(), start=1):
            if re.match(r"\s*case \d+:", line):
                rows.append(f"main.c    {i}    0x{addr:x}    x")
                addr += 0x14
        return "CU: main.c:\n" + "\n".join(rows) + "\n"

    def test_full_table(self):
        table = avr_symtab.build_symbol_table(
            NM, self._case_lines(), C, f_cpu=16000000, mcu="atmega328p")
        sched = table["scheduler"]
        self.assertEqual(sched["bw_ms"], {"space": "sram", "addr": 0x100, "size": 4})
        self.assertEqual(len(sched["tasks"]), 2)
        t0 = sched["tasks"][0]
        self.assertEqual(t0["state"]["addr"], 0x104)
        self.assertEqual([y["state"] for y in t0["yields"]], [0, 1, 2])
        for y in t0["yields"]:
            self.assertIn("addr", y, f"yield {y} carries a code address")
            self.assertEqual(y["block"], {0: "blockA", 1: "blockB", 2: "blockC"}[y["state"]])
        names = [v["name"] for v in table["variables"]]
        self.assertIn("score", names)
        self.assertNotIn("bw_ms", names)

    def test_drift_refusal(self):
        drifted = C.replace("@bw yield bw_task1 1 blockE forever\n * ", "")
        with self.assertRaises(SymbolTableError):
            avr_symtab.build_symbol_table(
                NM, self._case_lines(), drifted, f_cpu=16000000, mcu="atmega328p")

    def test_missing_until_is_tolerated_missing_state_is_recorded(self):
        # until: optional (gcc prunes unreferenced statics — see OptionalUntil).
        table = avr_symtab.build_symbol_table(
            NM.replace("bw_task1_until", "unrelated"), self._case_lines(), C,
            f_cpu=16000000, mcu="atmega328p")
        self.assertNotIn("until", table["scheduler"]["tasks"][1])
        # state: a task without one is SKIPPED and named in `pruned` —
        # dead-store elimination removes an empty task's state wholesale.
        table2 = avr_symtab.build_symbol_table(
            NM.replace("bw_task1_state", "unrelated"), self._case_lines(), C,
            f_cpu=16000000, mcu="atmega328p")
        self.assertEqual([t["name"] for t in table2["scheduler"]["tasks"]], ["bw_task0"])
        self.assertEqual(table2["pruned"], ["bw_task1"])

    def test_bw_ms_is_optional_the_rp2040_has_none(self):
        # The RP2040 flavor keeps NO millisecond counter in RAM — bw_now()
        # reads the hardware TIMER. The symbol's absence is the truth, not
        # an error; the table simply omits the key.
        table = avr_symtab.build_symbol_table(
            NM.replace("bw_ms", "unrelated_ms"), self._case_lines(), C,
            f_cpu=125000000, mcu="rp2040")
        self.assertNotIn("bw_ms", table["scheduler"])
        self.assertEqual(len(table["scheduler"]["tasks"]), 2)


if __name__ == "__main__":
    unittest.main()


class ParseObjdump(unittest.TestCase):
    OBJDUMP = """main.elf:     file format elf32-avr

SYMBOL TABLE:
00800104 l     O .bss\t00000002 bw_task0_state
00800106 l     O .bss\t00000002 bw_task0_until
00800108 l     O .bss\t00000002 bw_task1_state
0080010a l     O .bss\t00000002 bw_task1_until
00800100 l     O .bss\t00000004 bw_ms
0080010c l     O .bss\t00000002 score
00000000 g     F .text\t00000068 main
"""

    def test_objdump_t_parses_identically(self):
        syms = avr_symtab.parse_nm(self.OBJDUMP)
        self.assertEqual(syms["bw_task0_state"], {"addr": 0x104, "size": 2, "kind": "b"})
        self.assertEqual(syms["bw_ms"]["size"], 4)
        self.assertEqual(syms["main"]["kind"], "t")

    def test_build_from_objdump_flavor(self):
        table = avr_symtab.build_symbol_table(
            self.OBJDUMP, Build._case_lines(Build()), C,
            f_cpu=16000000, mcu="atmega328p")
        self.assertEqual(table["scheduler"]["bw_ms"]["addr"], 0x100)


class OptionalUntil(unittest.TestCase):
    def test_task_without_until_is_not_a_refusal(self):
        nm = NM.replace("00800106 00000002 b bw_task0_until\n", "")
        table = avr_symtab.build_symbol_table(
            nm, Build._case_lines(Build()), C, f_cpu=16000000, mcu="atmega328p")
        t0 = table["scheduler"]["tasks"][0]
        self.assertNotIn("until", t0, "gcc pruned it; the table says so by absence")
        self.assertIn("until", table["scheduler"]["tasks"][1])
