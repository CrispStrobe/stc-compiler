"""test_z80_build — the Z80 bench C target, proven the way the others are.

`test_arm_build.py` is the shape this follows: drive the real build function
with real source, then assert on the IMAGE rather than on the fact that a
subprocess returned 0.

What is being proven, and why each claim is here:

  1. the bench blink compiles at all — `sdcc -mz80` out of the VENDORED
     bundle, which is a different claim from "sdcc on this developer's PATH
     can do it". The Debian bullseye sdcc 4.0.0 in `bin/` has the z80 port
     compiled in; what it did not have until 2026-09-05 was
     `share/sdcc/lib/z80`, so the link silently borrowed `/usr/share/sdcc`
     — a directory that does not exist on Vercel. `test_bundle_carries_the_
     z80_runtime` and `test_link_uses_the_vendored_runtime` are that gap,
     turned into two assertions that go red if the runtime is dropped again.

  2. the RESET PATH. The Z80 fetches its first instruction from $0000, and
     the bench maps ROM there (`MAP ROM $0000-$7FFF`). So the image must
     have a byte at $0000, it must be a jump, and following that jump must
     land on a `call _main`. "There is code at 0x0000" on its own would pass
     for an image that jumps into the weeds.

  3. the OUT. The whole point of this target is that brickwright-lite's
     `generateC` z80 core writes pins through an OUT latch on I/O port 0,
     so `BW_PORT_OUT = _z80_sh` has to become `out (0),a` — asserted in the
     listing (the mnemonic, through the existing z80 listing parser) AND in
     the image (`D3 00`), because a listing is a rendering and the bytes are
     the artefact.

  4. DATA in RAM. `--data-loc` must be the first byte of the bench's RAM
     ($8000), not somewhere in ROM where a store goes nowhere. Proven by
     finding the store itself in the image.

THE SOURCE IS NOT INVENTED HERE. `BENCH_BLINK_C` is the shape
brickwright-lite's `generateC()` emits for `DEVICE Z80` — `__sfr __at 0x00`
declarations, a shadow byte for the write-only latch, a busy-loop `delay_ms`
because the bench has no timer, and `void main(void)`. It was taken from the
generator's output for `examples/z80-pd-bench/program.bw` and trimmed to one
LED so the assertions are readable; the constructs are unchanged, which is
what makes this an oracle for the real consumer rather than a compiler smoke
test.

The memory map is likewise measured, not chosen: it is what bw-board's
`extractZ80Machine` reads off the bench wiring, asserted by lite's
`examples/z80-pd-bench/check-extract.mjs` and written out in that example's
`EXPECTED.md` — `MAP ROM $0000-$7FFF`, `MAP RAM $8000-$FFFF`.
"""
from __future__ import annotations

import base64
import os
import re

import shutil
import subprocess
import tempfile
import unittest

from app import (BASE_DIR, CompileReq, Z80_TARGETS, build, build_z80,
                 sdcc_bin_dir, stage_toolchain)

# One LED on OUT0, the generator's own idioms. See the module docstring for
# where every line of this comes from.
BENCH_BLINK_C = r"""
#include <stdint.h>

#define F_CPU 7372800UL

/* Z80 breadboard machine: OUT latch (74HC374) + IN buffer (74HC244)
 * on I/O port 0. sdcc -mz80 compatible C. */
__sfr __at 0x00 BW_PORT_OUT;
__sfr __at 0x00 BW_PORT_IN;
static uint8_t _z80_sh;  /* shadow byte for the OUT latch */

/* Busy-loop delay (no timer on this machine). */
static void delay_ms(unsigned int ms)
{
    unsigned int i;
    while (ms--) {
        for (i = 0; i < 737u; i++) ;
    }
}

static void bw_setup(void)
{
}

void main(void)
{
    bw_setup();
    for (;;) {
        _z80_sh |= (uint8_t)(1 << 0); BW_PORT_OUT = _z80_sh;
        delay_ms(500);
        _z80_sh &= (uint8_t)~(1 << 0); BW_PORT_OUT = _z80_sh;
        delay_ms(500);
    }
}
"""

# A program whose only job is to read the IN buffer, so the `in a,(0)`
# half of the port pair is proven too and not assumed from the OUT half.
BENCH_INPUT_C = r"""
#include <stdint.h>
__sfr __at 0x00 BW_PORT_OUT;
__sfr __at 0x00 BW_PORT_IN;
static uint8_t _z80_sh;

void main(void)
{
    for (;;) {
        uint8_t sw = BW_PORT_IN;
        _z80_sh = sw;
        BW_PORT_OUT = _z80_sh;
    }
}
"""

# Long multiply and divide: the code generator cannot inline these, so the
# link has to reach into z80.lib for `__mullong` and `__divulong`.
LIBCALL_C = r"""
#include <stdint.h>
__sfr __at 0x00 BW_PORT_OUT;
static unsigned long a = 7, b = 11;
void main(void){ a = a * b / 3; BW_PORT_OUT = (uint8_t)a; }
"""

# The bench, as bw-board's extractZ80Machine reads it off the wiring.
ROM_START, ROM_END = 0x0000, 0x7FFF
RAM_START = 0x8000


def load_ihex(text: str) -> dict[int, int]:
    """Intel HEX -> {address: byte}, checksums enforced.

    A record whose checksum does not agree is a corrupt image, and an image
    that is 'successful' with bytes is exactly what would hide it.
    """
    memory: dict[int, int] = {}
    for raw in text.splitlines():
        record = raw.strip()
        if not record:
            continue
        assert record.startswith(":"), f"not an Intel HEX record: {record!r}"
        data = bytes.fromhex(record[1:])
        assert (sum(data) & 0xFF) == 0, f"bad checksum on {record!r}"
        count, addr, kind = data[0], (data[1] << 8) | data[2], data[3]
        if kind == 0x01:
            break
        if kind != 0x00:
            continue
        for offset in range(count):
            memory[addr + offset] = data[4 + offset]
    return memory


def found(pattern: str, text: str) -> bool:
    """A regex search that fails SMALL. `assertRegex` prints the haystack,
    and the haystack here is a whole assembly listing."""
    return re.search(pattern, text) is not None


def image_of(result: dict) -> bytes:
    """The image as one byte string, for searching for an instruction.

    Address order, gaps closed up: the areas are contiguous in a linked
    image, and a pattern that straddles a gap would be a false positive
    nobody could act on anyway.
    """
    memory = load_ihex(result["hex"])
    return bytes(memory[addr] for addr in sorted(memory))


def read16(memory: dict[int, int], addr: int) -> int:
    """Little-endian word, the Z80's only word order."""
    return memory[addr] | (memory[addr + 1] << 8)


def link_z80(code: str) -> str:
    """Run the SERVICE's sdcc over `code` and keep the work directory, so a
    test can read the link script and the map. The caller removes it."""
    stage_toolchain()
    bin_dir = sdcc_bin_dir()
    work = tempfile.mkdtemp(prefix="z80lk-")
    src = os.path.join(work, "main.c")
    with open(src, "w", encoding="utf-8") as handle:
        handle.write(code)
    spec = Z80_TARGETS["z80"]
    subprocess.run(
        [os.path.join(bin_dir, "sdcc"), "-mz80", "--std-c99",
         "--code-loc", f"0x{spec['code_loc']:04x}",
         "--data-loc", f"0x{spec['data_loc']:04x}",
         "-o", os.path.join(work, "main.ihx"), src],
        capture_output=True, text=True, timeout=60, cwd=work)
    return work


def compile_bench(code: str = BENCH_BLINK_C, target: str = "z80", **kwargs):
    req = CompileReq(code=code, target=target, fosc=None, **kwargs)
    return build_z80(req, Z80_TARGETS[target], None)


class TestZ80Bundle(unittest.TestCase):
    """The deployment half: the tools the SERVICE runs, not the host's."""

    def test_vendored_sdcc_has_the_z80_port(self):
        stage_toolchain()
        out = subprocess.run([os.path.join(sdcc_bin_dir(), "sdcc"), "--version"],
                             capture_output=True, text=True, timeout=20).stdout
        self.assertIn("z80", out.split("\n")[0],
                      f"sdcc --version does not list the z80 port: {out!r}")

    def test_bundle_carries_the_z80_runtime(self):
        """crt0.rel and z80.lib ship in the repo, so the link does not
        depend on a /usr/share/sdcc that Vercel does not have."""
        lib = os.path.join(BASE_DIR, "share", "sdcc", "lib", "z80")
        for name in ("crt0.rel", "z80.lib"):
            self.assertTrue(os.path.exists(os.path.join(lib, name)),
                            f"share/sdcc/lib/z80/{name} is missing from the "
                            f"bundle; sdcc -mz80 would link against the host's")

    def test_link_uses_the_vendored_crt0(self):
        """sdcc writes the link script it used. The crt0 named in it must be
        the bundle's, not the host's — that is the difference between a
        target that works on Vercel and one that works only here."""
        work = link_z80(BENCH_BLINK_C)
        try:
            with open(os.path.join(work, "main.lk"), encoding="utf-8") as handle:
                script = handle.read()
            crt0 = [row for row in script.splitlines() if row.endswith("crt0.rel")]
            self.assertEqual(len(crt0), 1, f"expected one crt0 in:\n{script}")
            used = os.path.realpath(crt0[0].strip())
            # It is a COPY (stage_toolchain puts the bundle in /tmp), so
            # samefile would be wrong. The claim is that the bytes are the
            # bundle's: the host's sdcc 4.2.0 crt0 and the vendored 4.0.0 one
            # are different files, so this tells them apart.
            with open(used, "rb") as handle:
                linked = handle.read()
            with open(os.path.join(BASE_DIR, "share", "sdcc", "lib", "z80",
                                   "crt0.rel"), "rb") as handle:
                vendored = handle.read()
            self.assertEqual(linked, vendored,
                             f"the link used {used!r}, whose bytes are not the "
                             f"vendored crt0's")
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_library_modules_come_from_the_vendored_z80_lib(self):
        """crt0 is not the runtime. A program that divides pulls modules out
        of z80.lib, and the .map names the library each module came from —
        so this catches a bundle that ships crt0 and nothing else, which
        would link here (the host has a z80.lib) and fail on Vercel."""
        work = link_z80(LIBCALL_C)
        try:
            with open(os.path.join(work, "main.map"), encoding="utf-8") as handle:
                mapped = handle.read()
            libs = {row.strip() for row in mapped.splitlines()
                    if row.strip().endswith("z80.lib")}
            self.assertTrue(libs, "no z80.lib module was linked at all; this "
                                  "program is supposed to need one")
            prefix = os.path.realpath(os.path.dirname(sdcc_bin_dir()))
            for lib in libs:
                self.assertTrue(os.path.realpath(lib).startswith(prefix),
                                f"a runtime module came from {lib!r}, outside "
                                f"the toolchain the service runs ({prefix!r})")
        finally:
            shutil.rmtree(work, ignore_errors=True)


class TestZ80Build(unittest.TestCase):
    def test_bench_blink_compiles(self):
        result = compile_bench()
        self.assertTrue(result["success"], result.get("error"))
        self.assertGreater(result["bytes"], 0)
        self.assertEqual(result["toolchain"], "sdcc-z80")
        self.assertEqual(result["mcu"], "z80")
        self.assertEqual(result["origin"], 0x0000)

    def test_hex_is_valid_intel_hex(self):
        result = compile_bench()
        memory = load_ihex(result["hex"])      # asserts every checksum
        self.assertGreater(len(memory), 64, "image is implausibly small")

    def test_reset_vector_at_0000_is_a_jump_into_rom(self):
        """The Z80 fetches its first opcode from $0000 and the bench maps
        ROM there. $0000 must hold JP nn (0xC3) and nn must be in ROM."""
        memory = load_ihex(compile_bench()["hex"])
        self.assertIn(0x0000, memory, "nothing at the reset vector")
        self.assertEqual(memory[0x0000], 0xC3,
                         f"reset vector is 0x{memory[0x0000]:02x}, not JP (0xC3)")
        entry = read16(memory, 0x0001)
        self.assertTrue(ROM_START <= entry <= ROM_END,
                        f"reset jumps to 0x{entry:04x}, outside ROM "
                        f"0x{ROM_START:04x}-0x{ROM_END:04x}")

    def test_reset_path_reaches_main(self):
        """Follow the reset jump and find the CALL to _main. A jump into
        the weeds passes 'code at 0x0000' and fails this."""
        result = compile_bench(symbols=True)
        memory = load_ihex(result["hex"])
        main = result["symbols"].get("_main")
        self.assertIsNotNone(main, f"no _main in {sorted(result['symbols'])}")

        entry = read16(memory, 0x0001)
        # crt0's init block: a handful of instructions ending in JP _exit.
        # Walk it looking for CALL nn (0xCD) targets; _main must be one.
        called = set()
        for offset in range(0, 32):
            if memory.get(entry + offset) == 0xCD:
                called.add(read16(memory, entry + offset + 1))
        self.assertIn(main, called,
                      f"the reset path at 0x{entry:04x} calls {sorted(called)}, "
                      f"never _main at 0x{main:04x}")

    def test_out_port_write_reaches_the_image_and_the_listing(self):
        """`BW_PORT_OUT = _z80_sh` must become an OUT to port 0."""
        result = compile_bench(disassemble=True)
        self.assertTrue(result["success"], result.get("error"))
        asm = result["listing"]["asm"]
        # assertRegex would dump the entire listing on failure, which buries
        # the finding. `found` names the pattern and nothing else.
        self.assertTrue(found(r"\bout\s+\(_BW_PORT_OUT\),\s*a", asm),
                        "the listing has no OUT to the latch port")
        # The .lst carries address columns before the source, so this is a
        # substring match on the equate, not a line anchor.
        self.assertTrue(found(r"_BW_PORT_OUT\s*=\s*0x0000\b", asm),
                        "the latch is not at I/O port 0")
        self.assertTrue(image_of(result).find(b"\xd3\x00") >= 0,
                        "no `out (0),a` (D3 00) byte pair in the image")

    def test_in_port_read_reaches_the_image_and_the_listing(self):
        result = compile_bench(code=BENCH_INPUT_C, disassemble=True)
        self.assertTrue(result["success"], result.get("error"))
        self.assertTrue(found(r"\bin\s+a,\s*\(_BW_PORT_IN\)",
                              result["listing"]["asm"]),
                        "the listing has no IN from the buffer port")
        self.assertTrue(image_of(result).find(b"\xdb\x00") >= 0,
                        "no `in a,(0)` (DB 00) byte pair in the image")

    def test_variables_live_in_bench_ram(self):
        """--data-loc is the first byte of the bench's RAM. The shadow byte
        is the program's only variable, so the image must contain a store
        to $8000 — a store into ROM would go nowhere on the real board."""
        store = bytes((0x32, RAM_START & 0xFF, RAM_START >> 8))
        self.assertTrue(image_of(compile_bench()).find(store) >= 0,
                        f"no `ld (0x{RAM_START:04x}),a` in the image; the "
                        f"shadow byte is not in bench RAM")

    def test_code_clears_the_absolute_areas(self):
        """crt0 owns $0000-$003B (RST vectors), $0100-$010B (init). _CODE
        must start above them or the linker overlays the reset path."""
        result = compile_bench(symbols=True)
        memory = load_ihex(result["hex"])
        entry = read16(memory, 0x0001)
        for name, addr in result["symbols"].items():
            if name in ("_main",):
                self.assertGreater(addr, entry,
                                   f"{name} at 0x{addr:04x} is below crt0's "
                                   f"init block at 0x{entry:04x}")

    def test_stages_payload_uses_the_existing_z80_parser(self):
        result = compile_bench(disassemble=True)
        stages = result["stages"]
        self.assertIn("tokens", stages)
        self.assertIn("listing", stages)
        symbols = stages["passes"][0]["symbols"] if stages["passes"] else {}
        self.assertIn("_main", symbols,
                      f"the .sym gave no _main: {sorted(symbols)[:10]}")

    def test_bin_format_is_a_full_rom(self):
        result = compile_bench(format="bin")
        self.assertTrue(result["success"], result.get("error"))
        self.assertEqual(result["bytes"], Z80_TARGETS["z80"]["rom"])
        blob = base64.b64decode(result["base64"])
        self.assertEqual(blob[0], 0xC3, "the ROM does not start with JP")

    def test_a_program_that_needs_z80_lib_links(self):
        """crt0.rel alone is not the runtime. The moment a program divides,
        or does long arithmetic, the code generator emits a CALL into
        z80.lib -- so a bundle carrying only crt0 would compile this and
        fail at link with `?ASlink-Error-Undefined Global`."""
        result = compile_bench(code=LIBCALL_C)
        self.assertTrue(result["success"], result.get("error"))

    def test_errors_are_mapped_not_dumped(self):
        """A syntax error comes back as {line, message}, the shape every
        other chain here uses — not as a wall of stderr."""
        result = compile_bench(code="void main(void) { this is not C; }\n")
        self.assertFalse(result["success"])
        self.assertTrue(result["errors"], f"no parsed errors in {result!r}")
        self.assertEqual(result["errors"][0]["line"], 1)

    def test_undefined_symbol_is_a_link_error_with_a_message(self):
        result = compile_bench(
            code="extern void nowhere(void);\nvoid main(void){ nowhere(); }\n")
        self.assertFalse(result["success"])
        self.assertTrue(any("nowhere" in e["message"] for e in result["errors"]),
                        f"the link error does not name the symbol: "
                        f"{result['errors']!r}")


class TestZ80Routing(unittest.TestCase):
    """The `target` field reaches build_z80, which is a separate claim from
    build_z80 working."""

    def test_target_z80_routes_through_build(self):
        req = CompileReq(code=BENCH_BLINK_C, target="z80", fosc=None)
        result = build(req)
        self.assertTrue(result["success"], result.get("error"))
        self.assertEqual(result["toolchain"], "sdcc-z80")

    def test_alias_z80_bench_is_the_same_machine(self):
        req = CompileReq(code=BENCH_BLINK_C, target="z80-bench", fosc=None)
        result = build(req)
        self.assertTrue(result["success"], result.get("error"))
        self.assertEqual(result["mcu"], "z80")

    def test_unknown_target_names_z80_among_the_known(self):
        req = CompileReq(code=BENCH_BLINK_C, target="z80-nonesuch", fosc=None)
        result = build(req)
        self.assertFalse(result["success"])
        self.assertIn("z80", result["error"])

    def test_keil_dialect_is_refused_for_the_z80(self):
        """Keil C51 is 8051 by definition. The refusal fires on source the
        translator actually CHANGED -- `sbit LED = P1^0` is 8051 through and
        through -- because that is the only kind that could be miscompiled
        into a Z80 image without anybody noticing."""
        req = CompileReq(code="#include <reg51.h>\nsbit LED = P1^0;\n"
                              "void main(void){ LED = 0; }\n",
                         target="z80", language="keil", fosc=None)
        result = build(req)
        self.assertFalse(result["success"], result.get("filename"))
        self.assertIn("8051-only", result["error"])


if __name__ == "__main__":
    unittest.main()
