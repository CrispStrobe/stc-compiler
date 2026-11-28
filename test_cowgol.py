"""Hosted Cowgol boundary, plus real compiler/emulator contract tests.

The dedicated Cowgol CI job installs the pinned bundle and requires all real
toolchain checks. The ordinary Python suite also runs every input guard.
"""

import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import cowgol_build as cowgol

BW_BOARD_PIN = "d21d6d8b819263eb08231134b7556efa82289a90"

HELLO = '''include "cowgol.coh";
var total: uint16 := 0;
var count: uint8 := 0;
while count < 7 loop
    total := total + 6;
    count := count + 1;
end loop;
if total == 42 then
    print("Cowgol ");
    print_i16(total);
    print_nl();
end if;
'''


def request(**values):
    defaults = dict(code=HELLO, language="cowgol", target="z80-cpm", format="bin",
                    options=[], defines={}, disassemble=False, symbols=False)
    defaults.update(values)
    return SimpleNamespace(**defaults)


class CowgolBoundaryTests(unittest.TestCase):
    def test_trusted_includes_and_literals(self):
        source = '# @asm "ignored"; include "/etc/passwd";\ninclude "cowgol.coh";\nprint("include @asm #");\nvar c := \'#\';'
        self.assertIsNone(cowgol.reject_source(source, "cpmz"))
        self.assertIsNone(cowgol.reject_source('include "brickwright.coh";', "cgen"))

    def test_include_paths_and_escapes_are_refused_before_toolchain_access(self):
        for name in ("/etc/passwd", "../cowgol.coh", "unknown.coh", "rt/cowgol.coh", "cowgol\\x2ecoh"):
            with self.subTest(name=name):
                result = cowgol.build_cowgol(request(code=f'include "{name}";'))
                self.assertFalse(result["success"])
                self.assertEqual(result["stage"], "source")
        self.assertIsNone(cowgol.reject_source('include "brickwright.coh";', "cpmz"))

    def test_inline_assembler_and_external_names_are_refused(self):
        for source in ('@asm "%include /etc/passwd";', 'extern "bad";', '@extern "bad";'):
            with self.subTest(source=source):
                result = cowgol.build_cowgol(request(code="# comment\n" + source))
                self.assertFalse(result["success"])
                self.assertEqual(result["line"], 2)

    def test_target_format_and_options_are_explicit(self):
        for values in ({"target": "stc12c5a60s2"}, {"format": "hex"}, {"options": ["-I/etc"]},
                       {"defines": {"X": "1"}}, {"symbols": True}, {"disassemble": True}):
            with self.subTest(values=values):
                self.assertFalse(cowgol.build_cowgol(request(**values))["success"])

    def test_source_limits_and_nul_are_refused(self):
        self.assertIsNotNone(cowgol.reject_source("#" * (cowgol.MAX_SOURCE_BYTES + 1), "cgen"))
        self.assertIsNotNone(cowgol.reject_source("\0", "cgen"))

    def test_riscv_refuses_filesystem_header_before_compiling(self):
        result = cowgol.build_cowgol(request(target="riscv32", format="elf", code='include "file.coh";'))
        self.assertFalse(result["success"])
        self.assertEqual(result["stage"], "source")

    def test_riscv_elf_validation_counts_uninitialized_workspace(self):
        # One code segment with eight initialized bytes and eight BSS bytes.
        image = bytearray(0x108)
        image[:7] = b"\x7fELF\x01\x01\x01"
        struct.pack_into("<HHIIIIIHHHHHH", image, 16,
                         2, 243, 1, 0x1000, 52, 0, 0, 52, 32, 1, 0, 0, 0)
        struct.pack_into("<IIIIIIII", image, 52, 1, 0x100, 0x1000, 0x1000, 8, 16, 5, 0x100)
        image[0x100:] = b"12345678"
        parsed = cowgol.riscv_image(image)
        self.assertEqual(parsed["entry"], 0x1000)
        self.assertEqual(base64.b64decode(parsed["segments"][0]["bytes"]), b"12345678" + bytes(8))
        invalid = []
        huge_bss = image.copy()
        struct.pack_into("<I", huge_bss, 52 + 20, cowgol.RISCV_STATIC_END - 0x1000 + 1)
        invalid.append(huge_bss)
        bss_entry = image.copy()
        struct.pack_into("<I", bss_entry, 24, 0x1008)
        invalid.append(bss_entry)
        wrong_cpu = image.copy()
        struct.pack_into("<H", wrong_cpu, 18, 40)
        invalid.append(wrong_cpu)
        wrong_endian = image.copy()
        wrong_endian[5] = 2
        invalid.append(wrong_endian)
        wrong_physical = image.copy()
        struct.pack_into("<I", wrong_physical, 52 + 12, 0x2000)
        invalid.append(wrong_physical)
        truncated = image[:-1]
        invalid.append(truncated)
        overlap = image.copy()
        struct.pack_into("<H", overlap, 44, 2)
        overlap[84:116] = overlap[52:84]
        invalid.append(overlap)
        for candidate in invalid:
            with self.subTest(header=candidate[:84].hex()), self.assertRaises(ValueError):
                cowgol.riscv_image(candidate)

    def test_missing_bundle_is_reported_without_c_fallback(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"COWGOL_BUNDLE": temporary}):
            self.assertEqual(cowgol.bundle_status()["targets"], {})
            result = cowgol.build_cowgol(request())
            self.assertFalse(result["success"])
            self.assertEqual(result["stage"], "toolchain")

    def test_missing_riscv_gcc_does_not_hide_the_installed_c_export(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"COWGOL_BUNDLE": temporary}):
            directory = Path(temporary)
            for name in ("bin/cowfe-cgen", "bin/cowbe-cgen", "bin/cowlink-cgen", "rt/cgen/cowgol.coh",
                         "rt/cgen/brickwright.coh", "rt/common.coh", "rt/cgen/cowgol.h",
                         "rt/cgen/brickwright.h", "rt/cgen/brickwright-console.h"):
                output = directory / name
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text("test bundle marker")
            (directory / "manifest.json").write_text(json.dumps({"schema": 1,
                "source_commit": "1" * 40, "targets": ["cowgol-cgen", "riscv32"]}))
            with patch.object(cowgol, "_riscv_toolchain", return_value=None):
                self.assertEqual(set(cowgol.bundle_status()["targets"]), {"cowgol-cgen"})
                result = cowgol.build_cowgol(request(target="riscv32", format="elf"))
                self.assertEqual(result["stage"], "toolchain")
                self.assertIn("riscv-gcc/picolibc", result["error"])

    def test_corrupt_bundle_fails_content_hash(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / "rt").mkdir()
            (directory / "rt/common.coh").write_text("corrupt")
            (directory / "manifest.json").write_text(json.dumps({"sha256": {
                "rt/common.coh": hashlib.sha256(b"expected").hexdigest()}}))
            with self.assertRaisesRegex(ValueError, "checksum mismatch"):
                cowgol._stage_bundle(directory)

    def test_cpm_layout_guard_requires_static_storage_evidence(self):
        self.assertEqual(cowgol.cpm_memory("top 164\nws = 1e4\nlomem = 1ec\n", 100)["lomem"], 0x1ec)
        for listing in ("top 164\n", "top 164\nws = 1e4\nlomem = d001\n", "top 164\nws = 1e4\nlomem = 100\n"):
            with self.subTest(listing=listing), self.assertRaises(ValueError):
                cowgol.cpm_memory(listing, 100)

    def test_api_routes_cowgol_without_generic_c_validation(self):
        from app import CompileReq, build, transpile_only
        response = {"success": True, "artifact_kind": "source"}
        with patch.object(cowgol, "build_cowgol", return_value=response) as compiler:
            self.assertEqual(build(CompileReq(code=HELLO, language="cowgol", target="z80-cpm", format="bin")), response)
            self.assertEqual(compiler.call_args.args[0].target, "z80-cpm")
            self.assertEqual(asyncio.run(transpile_only(CompileReq(code=HELLO, language="cowgol"))), response)
            self.assertEqual(compiler.call_args.args[0].target, "cowgol-cgen")
            self.assertEqual(compiler.call_args.args[0].format, "c")

    def test_http_cowgol_transpile_and_download_share_the_compile_guard(self):
        import app
        from fastapi.responses import JSONResponse
        limited = JSONResponse({"success": False, "error": "rate limited"}, status_code=429)
        req = app.CompileReq(code=HELLO, language="cowgol", target="z80-cpm", format="bin")
        with patch.object(app, "_guarded_build", return_value=limited) as guarded:
            self.assertIs(asyncio.run(app.transpile_only(req, object())), limited)
            self.assertEqual(guarded.call_args.args[0].target, "cowgol-cgen")
            self.assertIs(asyncio.run(app.download(req, object())), limited)
            self.assertEqual(guarded.call_args.args[0].target, "z80-cpm")


class CowgolCompilerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if set(cowgol.bundle_status()["targets"]) != set(cowgol.TARGETS):
            if os.environ.get("REQUIRE_COWGOL_TOOLCHAIN") == "1":
                raise AssertionError("CI requires every pinned Cowgol target; build the bundle first")
            raise unittest.SkipTest("Cowgol bundle is not installed")

    def compile(self, target, **values):
        result = cowgol.build_cowgol(request(target=target, format="c" if target == "cowgol-cgen" else "bin", **values))
        self.assertTrue(result["success"], result)
        image = base64.b64decode(result["base64"], validate=True)
        self.assertEqual(len(image), result["bytes"])
        self.assertTrue(result["toolchain"].startswith("cowgol-"))
        self.assertRegex(result["compiler_commit"], r"^[a-f0-9]{40}$")
        return result, image

    def oracle(self, name):
        path = os.environ.get(name)
        if not path or not Path(path).is_file():
            if os.environ.get("REQUIRE_COWGOL_TOOLCHAIN") == "1":
                self.fail(f"CI requires {name}")
            self.skipTest(f"set {name} to exercise native program execution")
        return path

    def test_cpms_compile_and_execute_in_emulator(self):
        emulator = self.oracle("COWGOL_CPMEMU")
        for target in ("z80-cpm", "i8080-cpm"):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as temporary:
                result, image = self.compile(target)
                self.assertEqual(result["origin"], 0x100)
                self.assertEqual(result["filename"], "main.com")
                self.assertLess(len(image), 0xcf00)
                self.assertLessEqual(result["memory_end"], 0xd000)
                program = Path(temporary) / "main.com"
                program.write_bytes(image)
                output = subprocess.run([emulator, str(program)], capture_output=True, text=True, timeout=10, check=True)
                self.assertEqual(output.stdout.strip(), "Cowgol 42")

    def test_dos_compiles_and_executes_in_emu2(self):
        emulator = self.oracle("COWGOL_EMU2")
        result, image = self.compile("dos")
        self.assertEqual(image[:2], b"MZ")
        self.assertEqual(result["filename"], "main.exe")
        with tempfile.TemporaryDirectory() as temporary:
            program = Path(temporary) / "main.exe"
            program.write_bytes(image)
            output = subprocess.run([emulator, str(program)], capture_output=True, text=True, timeout=10, check=True)
            self.assertEqual(output.stdout.strip(), "Cowgol 42")

    def test_standalone_c_builds_and_runs_without_cowgol_headers(self):
        compiler = shutil.which("cc")
        if not compiler:
            self.skipTest("C compiler unavailable")
        result, image = self.compile("cowgol-cgen")
        self.assertEqual(result["artifact_kind"], "source")
        self.assertEqual(result["c"].encode(), image)
        self.assertIn(b"Redistribution", image)
        with tempfile.TemporaryDirectory() as temporary:
            source, binary = Path(temporary) / "main.c", Path(temporary) / "main"
            source.write_bytes(image)
            subprocess.run([compiler, "-std=gnu99", "-fwrapv", "-fno-strict-aliasing", str(source), "-o", str(binary)],
                           capture_output=True, text=True, timeout=15, check=True)
            output = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10, check=True)
            self.assertEqual(output.stdout.strip(), "Cowgol 42")

    def test_real_diagnostics_keep_source_line_and_fail(self):
        result = cowgol.build_cowgol(request(code='include "cowgol.coh";\nthis is invalid;'))
        self.assertFalse(result["success"])
        self.assertIn("main.cow", result["log"])
        self.assertNotIn("cowgol-request-", result["log"])

    def test_portable_console_bridge_signed_limits_on_each_machine(self):
        source = '''include "cowgol.coh";
include "brickwright.coh";
bw_print_text("console");
bw_print_number(-2147483647 as int32 - 1);
bw_print_number(2147483647 as int32);
bw_print_number(-42 as int32);
'''
        for target, oracle_name in (("z80-cpm", "COWGOL_CPMEMU"), ("i8080-cpm", "COWGOL_CPMEMU"), ("dos", "COWGOL_EMU2")):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as temporary:
                result, image = self.compile(target, code=source)
                program = Path(temporary) / result["filename"]
                program.write_bytes(image)
                output = subprocess.run([self.oracle(oracle_name), str(program)], capture_output=True, text=True, timeout=10, check=True)
                self.assertEqual(output.stdout.strip().splitlines(), ["console", "-2147483648", "2147483647", "-42"])

    def test_native_bridge_does_not_fake_canvas_or_gpio(self):
        for target in ("z80-cpm", "i8080-cpm", "dos"):
            with self.subTest(target=target):
                result = cowgol.build_cowgol(request(target=target, code='include "cowgol.coh";\ninclude "brickwright.coh";\nbw_digital_write(1 as int32, 1 as int32);'))
                self.assertFalse(result["success"])
                self.assertIn("bw_digital_write", result.get("log", ""))

    def test_cpm_rejects_uninitialized_workspace_that_would_overlap_os(self):
        source = '''include "cowgol.coh";
var large: uint8[54000];
large[53999] := 42;
print_i8(large[53999]);
'''
        for target in ("z80-cpm", "i8080-cpm"):
            with self.subTest(target=target):
                result = cowgol.build_cowgol(request(target=target, code=source))
                self.assertFalse(result["success"])
                self.assertIn("static workspace exceed", result["error"])


class CowgolRiscVCompilerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if "riscv32" not in cowgol.bundle_status()["targets"]:
            if os.environ.get("REQUIRE_COWGOL_TOOLCHAIN") == "1":
                raise AssertionError("CI requires the Cowgol RISC-V target and pinned GCC runtime")
            raise unittest.SkipTest("Cowgol RISC-V bundle is not installed")

    def compile(self, source=HELLO):
        result = cowgol.build_cowgol(request(target="riscv32", format="elf", code=source))
        self.assertTrue(result["success"], result)
        self.assertEqual(result["target"], "riscv32")
        self.assertEqual(result["format"], "elf")
        self.assertEqual(result["toolchain"], "cowgol-riscv32")
        self.assertEqual(result["artifact_kind"], "executable")
        self.assertEqual(result["filename"], "main.elf")
        self.assertEqual(result["entry"], result["image"]["entry"])
        elf = base64.b64decode(result["base64"], validate=True)
        self.assertEqual(len(elf), result["bytes"])
        self.assertEqual(cowgol.riscv_image(elf), result["image"])
        self.assertLessEqual(result["memory_end"], cowgol.RISCV_STATIC_END)
        return result

    def run_program(self, result):
        board = os.environ.get("COWGOL_BW_BOARD")
        node = shutil.which("node")
        if not node or not board or not (Path(board) / "src/riscv32-machine.js").is_file():
            if os.environ.get("REQUIRE_COWGOL_TOOLCHAIN") == "1":
                self.fail("CI requires node and COWGOL_BW_BOARD for actual RV32 execution")
            self.skipTest("set COWGOL_BW_BOARD to the pinned machine checkout to execute RV32 output")
        self.assertEqual(subprocess.check_output(["git", "-C", board, "rev-parse", "HEAD"], text=True).strip(),
                         BW_BOARD_PIN, "the execution oracle must match Brickwright's exact machine pin")
        with tempfile.TemporaryDirectory() as temporary:
            image_file = Path(temporary) / "image.json"
            image_file.write_text(json.dumps(result["image"]))
            process = subprocess.run([node, str(Path(__file__).parent / "scripts/run-cowgol-riscv.mjs"), str(image_file)],
                                     capture_output=True, text=True, timeout=20, check=True)
            return process.stdout

    def test_full_cowgol_compiles_and_executes_on_the_brickwright_machine(self):
        source = '''include "cowgol.coh";
include "brickwright.coh";
include "malloc.coh";
include "argv.coh";
var values: int32[3];
values[0] := 20;
values[1] := 22;
values[2] := values[0] + values[1];
var quotient: uint32;
var remainder: uint32;
(quotient, remainder) := divmodu32(85, 2);
var memory := Alloc(8);
[memory] := 17;
ArgvInit();
if ArgvNext() != nil then
    ExitWithError();
end if;
bw_print_text("RISC-V Cowgol");
bw_print_number(values[2]);
bw_print_number(quotient as int32);
bw_print_number(remainder as int32);
bw_print_number([memory] as int32);
Free(memory);
bw_print_number(-2147483647 as int32 - 1);
bw_print_number(2147483647 as int32);
'''
        output = self.run_program(self.compile(source))
        self.assertEqual(output.strip().splitlines(),
                         ["RISC-V Cowgol", "42", "42", "1", "17", "-2147483648", "2147483647"])

    def test_ordinary_console_cowgol_runs_without_portable_bridge(self):
        self.assertEqual(self.run_program(self.compile()).strip(), "Cowgol 42")

    def test_unsupported_riscv_host_operations_fail_at_link(self):
        for call in ("bw_digital_write(1 as int32, 1 as int32)", "bw_delay(1 as int32)",
                     "bw_canvas_clear(0 as int32)"):
            with self.subTest(call=call):
                result = cowgol.build_cowgol(request(target="riscv32", format="elf", code=
                    'include "cowgol.coh";\ninclude "brickwright.coh";\n' + call + ";"))
                self.assertFalse(result["success"])
                self.assertEqual(result["stage"], "compile")
                self.assertIn("cowgol_" + call.split("(")[0], result.get("log", ""))

    def test_riscv_refuses_source_layout_overflow_before_narrowing(self):
        # Array indices and lengths are uint16 in this bootstrap. Multiple
        # simultaneously-live arrays exercise the linked workspace bound
        # without relying on an out-of-range source array length.
        source = 'include "cowgol.coh";\n'
        source += "\n".join(f"var large{i}: uint8[65000];" for i in range(64))
        source += "\n" + "\n".join(f"MemSet(&large{i}[0], 42, @bytesof large{i});" for i in range(64))
        source += "\nvar index := get_char() as uint16;\n"
        source += "\n".join(f"print_i8(large{i}[index]);" for i in range(64))
        result = cowgol.build_cowgol(request(target="riscv32", format="elf", code=source))
        self.assertFalse(result["success"])
        self.assertEqual(result["stage"], "compile")
        self.assertRegex((result.get("error", "") + result.get("log", "")).lower(),
                         "workspace|array|overflow|too large")


if __name__ == "__main__":
    unittest.main()
