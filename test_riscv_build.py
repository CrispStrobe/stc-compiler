"""test_riscv_build — verify the RISC-V (RV32IM) compile path end-to-end.

The compiler is shecc run as WebAssembly under wasmtime (riscv_cc.py). Unlike
the other targets there is no native toolchain to stage and no hardware to
flash: the output is an ELF32 image an emulated RV32 machine boots. So the
oracle here is plain, portable C — functions, recursion, arrays, printf — and
the checks are:
  1. compile succeeds and returns a non-empty rv32 ELF (e_machine == 243)
  2. the response carries an {entry, segments} image with base64 segment bytes,
     and entry lands inside a loaded segment
  3. a compile error is the program's, reported with shecc's own diagnostics
  4. if qemu-riscv32 is on PATH, the compiled program actually runs and prints

The whole file skips cleanly when the compiler cannot run (wasmtime absent or
the wasm not vendored), the same way the native-toolchain tests skip.
"""
import base64
import shutil
import struct
import subprocess
import tempfile
import unittest

import riscv_cc
from app import build_riscv, build, CompileReq, RISCV_TARGETS

HELLO_C = r"""
int fib(int n) { return n < 2 ? n : fib(n - 1) + fib(n - 2); }
int main() {
    printf("%s\n", "hello from shecc on wasm");
    for (int i = 0; i < 8; i++) printf("fib(%d)=%d\n", i, fib(i));
    int a[4]; int s = 0;
    for (int i = 0; i < 4; i++) { a[i] = i * i; s += a[i]; }
    printf("sumsq=%d\n", s);
    return 0;
}
"""


@unittest.skipUnless(riscv_cc.available(), "no RISC-V compiler (wasmtime/wasm absent)")
class RiscvBuild(unittest.TestCase):
    def _build(self, code):
        return build_riscv(CompileReq(code=code, language="c", target="riscv32"),
                           RISCV_TARGETS["riscv32"], None, "prog")

    def test_compiles_to_an_rv32_elf_image(self):
        r = self._build(HELLO_C)
        self.assertTrue(r["success"], r.get("error"))
        self.assertEqual(r["toolchain"], "shecc")
        self.assertEqual(r["mcu"], "rv32im")
        self.assertEqual(r["filename"], "prog.elf")

        elf = base64.b64decode(r["base64"])
        self.assertGreater(len(elf), 0)
        self.assertEqual(elf[:4], b"\x7fELF")
        self.assertEqual(elf[4], 1, "ELF32")
        self.assertEqual(struct.unpack_from("<H", elf, 18)[0], 243, "e_machine == RISC-V")

        img = r["image"]
        self.assertEqual(img["entry"], r["entry"])
        self.assertGreaterEqual(len(img["segments"]), 1)
        # entry lands inside some loaded segment
        placed = []
        for seg in img["segments"]:
            data = base64.b64decode(seg["bytes"])
            self.assertGreater(len(data), 0)
            placed.append((seg["addr"], seg["addr"] + len(data)))
        self.assertTrue(any(lo <= img["entry"] < hi for lo, hi in placed),
                        "entry point is inside a loaded segment")

    def test_dispatch_routes_riscv32_through_build(self):
        r = build(CompileReq(code="int main(){ return 0; }", language="c", target="riscv32"))
        self.assertTrue(r["success"], r.get("error"))
        self.assertEqual(r["toolchain"], "shecc")

    def test_a_compile_error_is_the_programs(self):
        r = self._build("int main() { this is not valid C ; }")
        self.assertFalse(r["success"])
        self.assertEqual(r["stage"], "compile")
        self.assertRegex(r.get("log", "") + r.get("error", ""), r"in\.c:\d+")

    def test_it_actually_runs_under_qemu(self):
        qemu = shutil.which("qemu-riscv32")
        if not qemu:
            self.skipTest("qemu-riscv32 not on PATH")
        r = self._build(HELLO_C)
        self.assertTrue(r["success"], r.get("error"))
        elf = base64.b64decode(r["base64"])
        with tempfile.NamedTemporaryFile(suffix=".elf", delete=True) as fh:
            fh.write(elf); fh.flush()
            import os
            os.chmod(fh.name, 0o755)
            out = subprocess.run([qemu, fh.name], capture_output=True, text=True, timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("hello from shecc on wasm", out.stdout)
        self.assertIn("fib(7)=13", out.stdout)
        self.assertIn("sumsq=14", out.stdout)


if __name__ == "__main__":
    unittest.main()
