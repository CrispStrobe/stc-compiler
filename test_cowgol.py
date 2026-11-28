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
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import cowgol_build as cowgol


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

    def test_missing_bundle_is_reported_without_c_fallback(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(os.environ, {"COWGOL_BUNDLE": temporary}):
            self.assertEqual(cowgol.bundle_status()["targets"], {})
            result = cowgol.build_cowgol(request())
            self.assertFalse(result["success"])
            self.assertEqual(result["stage"], "toolchain")

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


if __name__ == "__main__":
    unittest.main()
