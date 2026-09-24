"""test_riscv_gcc_build — verify the full-C RISC-V path end to end.

`riscv32-gcc` compiles ARBITRARY C with the native gcc + picolibc bundle
(riscv-gcc/), the heavyweight companion to the shecc-wasm `riscv32` subset. The
oracle here exercises exactly what shecc CANNOT: float printf (%f via
picolibc + math.h), malloc, qsort with a function pointer, 64-bit long long,
string.h. Checks:
  1. compile succeeds and returns a non-empty rv32 ELF (e_machine == 243)
  2. the response carries an {entry, segments} image with base64 segment bytes,
     entry inside a loaded segment
  3. a compile error is the program's (stage 'compile')

Structural, like test_arm_build.py — not an execution test. The image is a
bare-metal freestanding ELF whose crt0 sets sp to the top of the emulated RV32
machine's RAM, so qemu-linux-user (which maps only the LOAD segments, not that
stack) is the wrong runtime; the real target is bw-board's RiscV32Machine,
whose own suite boots these images.
"""
import base64
import struct
import unittest

from app import build_riscv_gcc, build, CompileReq, RISCV_TARGETS, stage_riscv_gcc

FULL_C = r"""
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>
static int cmp(const void *a, const void *b) { return *(const int*)a - *(const int*)b; }
static long long fact(int n) { return n <= 1 ? 1 : n * fact(n - 1); }
int main(void) {
    int v[5] = {5, 3, 9, 1, 7};
    qsort(v, 5, sizeof(int), cmp);
    for (int i = 0; i < 5; i++) printf("%d ", v[i]);
    printf("\n");
    char *p = malloc(32); strcpy(p, "picolibc");
    printf("%s len=%d\n", p, (int)strlen(p)); free(p);
    printf("13! = %lld\n", fact(13));
    printf("sqrt2=%.5f pi=%.5f\n", sqrt(2.0), 4.0 * atan(1.0));
    return 0;
}
"""


@unittest.skipUnless(stage_riscv_gcc() is not None, "no riscv-gcc bundle/toolchain")
class RiscvGccBuild(unittest.TestCase):
    def _build(self, code):
        return build_riscv_gcc(CompileReq(code=code, language="c", target="riscv32-gcc"),
                               RISCV_TARGETS["riscv32-gcc"], None, "prog")

    def test_compiles_full_c_to_an_rv32_image(self):
        r = self._build(FULL_C)
        self.assertTrue(r["success"], r.get("error"))
        self.assertEqual(r["toolchain"], "riscv64-unknown-elf-gcc")
        self.assertEqual(r["mcu"], "rv32imac")
        elf = base64.b64decode(r["base64"])
        self.assertEqual(elf[:4], b"\x7fELF")
        self.assertEqual(elf[4], 1, "ELF32")
        self.assertEqual(struct.unpack_from("<H", elf, 18)[0], 243, "e_machine == RISC-V")
        img = r["image"]
        self.assertEqual(img["entry"], r["entry"])
        self.assertGreaterEqual(len(img["segments"]), 1)
        placed = []
        for seg in img["segments"]:
            data = base64.b64decode(seg["bytes"])
            self.assertGreater(len(data), 0)
            placed.append((seg["addr"], seg["addr"] + len(data)))
        self.assertTrue(any(lo <= img["entry"] < hi for lo, hi in placed),
                        "entry point is inside a loaded segment")

    def test_dispatch_routes_riscv32_gcc_through_build(self):
        r = build(CompileReq(code="int main(){ return 0; }", language="c", target="riscv32-gcc"))
        self.assertTrue(r["success"], r.get("error"))
        self.assertEqual(r["toolchain"], "riscv64-unknown-elf-gcc")

    def test_a_compile_error_is_the_programs(self):
        r = self._build("int main() { this is not valid C ; }")
        self.assertFalse(r["success"])
        self.assertEqual(r["stage"], "compile")


if __name__ == "__main__":
    unittest.main()
