"""Cowgol's real compiler, isolated from the service's C compiler routes.

The bundle is built from a pinned CrispStrobe/cowgol checkout. Requests never
select executable paths, invoke a shell, or execute the resulting program.
"""

import base64
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time


ROOT = Path(__file__).resolve().parent
MAX_SOURCE_BYTES = 1_000_000
MAX_ARTIFACT_BYTES = 4 * 1024 * 1024
TIMEOUT = 25
TARGETS = {
    "cowgol-cgen": {"description": "Cowgol to standalone C source (little-endian host)",
                    "family": "cgen", "format": "c", "extension": "c"},
    "z80-cpm": {"description": "Cowgol for Z80 CP/M (.COM at 0100h)",
                "family": "cpmz", "format": "bin", "extension": "com", "origin": 0x100},
    "i8080-cpm": {"description": "Cowgol for Intel 8080 CP/M (.COM at 0100h)",
                  "family": "cpm", "format": "bin", "extension": "com", "origin": 0x100},
    "dos": {"description": "Cowgol for 8086 DOS (small-model MZ executable)",
            "family": "msdos", "format": "bin", "extension": "exe"},
}
ALIASES = {"cgen": "cowgol-cgen", "cowgol-c": "cowgol-cgen", "cowgol-z80-cpm": "z80-cpm",
           "8080-cpm": "i8080-cpm", "cowgol-8080-cpm": "i8080-cpm", "i8086-dos": "dos"}
INCLUDES = frozenset(("cowgol.coh", "strings.coh", "malloc.coh", "file.coh", "argv.coh", "brickwright.coh"))
# This scanner observes Cowgol's # comments, strings, character literals and
# @-keywords, so prose containing 'include' or '@asm' is not mistaken for code.
TOKENS = re.compile(r"\#[^\n]*|\s+|\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|@?[A-Za-z_][A-Za-z_0-9]*|.", re.S)


def reject_source(source, family):
    if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
        return {"success": False, "stage": "source", "error": "Cowgol source too large"}
    if "\0" in source:
        return {"success": False, "stage": "source", "error": "Cowgol source cannot contain NUL"}
    tokens = [match for match in TOKENS.finditer(source)
              if not match.group().isspace() and not match.group().startswith("#")]
    allowed = INCLUDES
    for index, token in enumerate(tokens):
        word = token.group().lower()
        error = None
        if word in ("@asm", "@extern", "extern"):
            error = "inline assembly and external symbol directives are unavailable in hosted Cowgol; use the local compiler"
        elif word == "include":
            name = tokens[index + 1].group() if index + 1 < len(tokens) else ""
            terminator = tokens[index + 2].group() if index + 2 < len(tokens) else ""
            if name not in {f'"{item}"' for item in allowed} or terminator != ";":
                error = "Cowgol includes must name one trusted bundled header: " + ", ".join(sorted(allowed))
        if error:
            return {"success": False, "stage": "source", "line": source.count("\n", 0, token.start()) + 1,
                    "error": error}
    return None


def bundle_root():
    return Path(os.environ.get("COWGOL_BUNDLE", str(ROOT / "vendor/cowgol"))).resolve()


def _required(spec):
    family = spec["family"]
    tools = [f"bin/{name}-{family}" for name in ("cowfe", "cowbe", "cowlink")]
    files = tools + [f"rt/{family}/cowgol.coh", f"rt/{family}/brickwright.coh", "rt/common.coh"]
    if family == "cgen":
        files += ["rt/cgen/cowgol.h", "rt/cgen/brickwright.h"]
    else:
        files.append(f"rt/{family}/cowgol.coo")
        tools += (["bin/nasm", "bin/djlink"] if family == "msdos" else ["bin/zmac"])
        files += tools[3:]
    return files, tools


def bundle_status():
    directory = bundle_root()
    try:
        manifest = json.loads((directory / "manifest.json").read_text())
        if manifest.get("schema") != 1 or not re.fullmatch(r"[0-9a-f]{40}", manifest.get("source_commit", "")):
            raise ValueError("invalid Cowgol bundle manifest")
        available = {}
        for name, spec in TARGETS.items():
            files, _tools = _required(spec)
            if name in manifest.get("targets", []) and all((directory / path).is_file() for path in files):
                available[name] = spec["description"]
        return {"available": bool(available), "source": manifest.get("source_repository"),
                "commit": manifest["source_commit"], "targets": available,
                "missing_targets": sorted(set(TARGETS) - set(available))}
    except (OSError, ValueError, TypeError):
        return {"available": False, "targets": {}, "missing_targets": sorted(TARGETS),
                "reason": "Cowgol bundle is not installed; run scripts/build-cowgol-bundle.py"}


def _stage_bundle(directory):
    """Verify content-addressed files, then copy once to an executable temp tree.

    Vercel may discard executable mode bits. Do not mutate the deployment tree
    or trust a partial /tmp stage left by a terminated request.
    """
    manifest_bytes = (directory / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    digest = hashlib.sha256(manifest_bytes).hexdigest()
    stage_parent = Path(tempfile.gettempdir()) / "stc-cowgol"
    stage_parent.mkdir(exist_ok=True)
    destination = stage_parent / digest
    if (destination / ".complete").is_file():
        return destination
    with tempfile.TemporaryDirectory(prefix="stage-", dir=stage_parent) as temporary:
        staging = Path(temporary) / "bundle"
        staging.mkdir()
        for relative, expected in manifest["sha256"].items():
            path = Path(relative)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("invalid path in Cowgol bundle manifest")
            blob = (directory / path).read_bytes()
            if hashlib.sha256(blob).hexdigest() != expected:
                raise ValueError(f"Cowgol bundle checksum mismatch: {relative}")
            output = staging / path
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(blob)
            if path.parts[0] == "bin":
                output.chmod(0o755)
        (staging / ".complete").write_text(digest)
        try:
            staging.rename(destination)
        except OSError:
            if not (destination / ".complete").is_file():
                raise
    return destination


def build_cowgol(req):
    target = ALIASES.get(req.target.lower(), req.target.lower())
    spec = TARGETS.get(target)
    if spec is None:
        return {"success": False, "stage": "target", "error": f"Cowgol has no hosted target {req.target!r}; known: " + ", ".join(TARGETS)}
    if req.format != spec["format"]:
        return {"success": False, "stage": "target", "error": f"Cowgol target {target} requires format {spec['format']!r}"}
    if req.options or req.defines:
        return {"success": False, "stage": "options", "error": "hosted Cowgol accepts no custom options or defines"}
    if req.disassemble or req.symbols:
        return {"success": False, "stage": "options", "error": "Cowgol debug symbols and disassembly are not available yet"}
    refusal = reject_source(req.code, spec["family"])
    if refusal:
        return refusal
    status = bundle_status()
    if target not in status["targets"]:
        return {"success": False, "stage": "toolchain", "toolchain": "cowgol-" + spec["family"],
                "error": f"Cowgol toolchain for {target} is not installed; run scripts/build-cowgol-bundle.py"}
    try:
        bundle = _stage_bundle(bundle_root())
        with tempfile.TemporaryDirectory(prefix="cowgol-request-") as temporary:
            work = Path(temporary)
            source, cob, coo = [work / f"main.{ext}" for ext in ("cow", "cob", "coo")]
            source.write_text(req.code, encoding="utf-8")
            family = spec["family"]
            started, logs = time.monotonic(), []

            def run(command):
                with tempfile.TemporaryFile() as log:
                    remaining = max(0.01, TIMEOUT - (time.monotonic() - started))
                    process = subprocess.run([str(arg) for arg in command], stdin=subprocess.DEVNULL,
                                             stdout=log, stderr=subprocess.STDOUT, cwd=work,
                                             timeout=remaining)
                    log.seek(0)
                    text = log.read(65536).decode("utf-8", "replace")
                logs.append(text.replace(str(work) + os.sep, "").replace(str(bundle) + os.sep, ""))
                if process.returncode:
                    raise RuntimeError(f"{Path(command[0]).name} exited with status {process.returncode}")

            run([bundle / f"bin/cowfe-{family}", f"-I{bundle / 'rt' / family}/", f"-I{bundle / 'rt'}/", source, cob])
            run([bundle / f"bin/cowbe-{family}", cob, coo])
            output = work / f"main.{spec['extension']}"
            if family == "cgen":
                run([bundle / "bin/cowlink-cgen", "-o", output, coo])
                generated = output.read_text()
                prefix = '#include "cowgol.h"\n'
                if not generated.startswith(prefix):
                    raise ValueError("unexpected Cowgol C output: missing runtime header")
                # Always declare the bridge: unused prototypes have no linker
                # cost; detecting a particular source spelling is unnecessary.
                notice = (bundle / "licenses/Cowgol-BSD-2-Clause.txt").read_text().replace("*/", "* /")
                generated = ("/* Cowgol cgen output. Runtime: BSD-2-Clause.\n" + notice + "\n*/\n"
                             + (bundle / "rt/cgen/cowgol.h").read_text() + "\n"
                             + (bundle / "rt/cgen/brickwright.h").read_text() + "\n"
                             + generated[len(prefix):])
                output.write_text(generated)
            else:
                assembly = work / ("main.asm" if family == "msdos" else "main.z80")
                run([bundle / f"bin/cowlink-{family}", "-o", assembly, coo, bundle / f"rt/{family}/cowgol.coo"])
                if family == "msdos":
                    obj = work / "main.obj"
                    run([bundle / "bin/nasm", "-f", "obj", "-o", obj, assembly])
                    run([bundle / "bin/djlink", "-o", output, obj])
                else:
                    # zmac selects its writer by suffix; .cim is the raw
                    # memory image format which CP/M calls .COM.
                    cim = work / "main.cim"
                    run([bundle / "bin/zmac", "-j", "-m", "-z" if family == "cpmz" else "-8", "-o", cim, assembly])
                    cim.rename(output)
            maximum = 0xff00 if spec.get("origin") == 0x100 else MAX_ARTIFACT_BYTES
            if not output.is_file() or not 0 < output.stat().st_size <= maximum:
                raise ValueError(f"Cowgol output is empty or exceeds {maximum} bytes for {target}")
            artifact = output.read_bytes()
            if family == "msdos" and artifact[:2] != b"MZ":
                raise ValueError("Cowgol DOS linker did not produce an MZ executable")
            response = {"success": True, "base64": base64.b64encode(artifact).decode("ascii"),
                        "filename": output.name, "bytes": len(artifact), "log": "".join(logs),
                        "language": "cowgol", "target": target, "toolchain": "cowgol-" + family,
                        "compiler_commit": status["commit"], "artifact_kind": "source" if family == "cgen" else "executable"}
            if "origin" in spec:
                response["origin"] = spec["origin"]
            if family == "cgen":
                response.update(c=artifact.decode("utf-8"), source_language="c")
            return response
    except subprocess.TimeoutExpired:
        return {"success": False, "stage": "compile", "error": f"Cowgol compile timed out after {TIMEOUT}s"}
    except (OSError, RuntimeError, ValueError, KeyError) as error:
        return {"success": False, "stage": "compile", "error": str(error),
                "log": "".join(locals().get("logs", []))}
