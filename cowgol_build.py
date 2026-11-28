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
import struct
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
    "riscv32": {"description": "Cowgol via C for Brickwright RV32IMAC (console ELF)",
                "family": "cgen", "format": "elf", "extension": "elf", "cross": "riscv32"},
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
    # The RV32 console machine has no filesystem. Do not expose a header
    # whose POSIX calls have no implementation in its embedded runtime.
    allowed = INCLUDES - {"file.coh"} if family == "riscv32" else INCLUDES
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
        if spec.get("cross") == "riscv32":
            files.append("rt/cgen/brickwright-console.h")
    else:
        files.append(f"rt/{family}/cowgol.coo")
        tools += (["bin/nasm", "bin/djlink"] if family == "msdos" else ["bin/zmac"])
        files += tools[3:]
    return files, tools


def _riscv_toolchain():
    """Reuse the service's pinned GCC staging, including compressed cc1.

    Import lazily: app imports this module while registering its routes.
    Merely having a GCC executable does not supply the machine runtime or
    picolibc, so require those parts before advertising this target.
    """
    from app import stage_riscv_gcc
    try:
        binary_directory = stage_riscv_gcc()
    except OSError:
        return None
    if not binary_directory:
        return None
    root = Path(binary_directory).parent
    required = ("bin/riscv64-unknown-elf-gcc", "runtime/crt0.S", "runtime/console.c",
                "runtime/riscv-cc.ld", "picolibc/include/stdio.h", "picolibc/lib/libc.a")
    return root if all((root / name).is_file() for name in required) else None


def bundle_status():
    directory = bundle_root()
    try:
        manifest = json.loads((directory / "manifest.json").read_text())
        if manifest.get("schema") != 1 or not re.fullmatch(r"[0-9a-f]{40}", manifest.get("source_commit", "")):
            raise ValueError("invalid Cowgol bundle manifest")
        available = {}
        for name, spec in TARGETS.items():
            files, _tools = _required(spec)
            if (name in manifest.get("targets", [])
                    and all((directory / path).is_file() for path in files)
                    and (not spec.get("cross") or _riscv_toolchain() is not None)):
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


def cpm_memory(listing, image_bytes):
    """Check static storage absent from a .COM file against Brickwright's TPA.

    The pinned linker emits TOP (end of bytes), ws (after its 128-byte stack),
    and LOMEM (after static workspace). Checking only image length misses a
    large uninitialized array that would overwrite the guest OS.
    """
    matches = re.findall(r"^\s*(top|ws|lomem)\s+(?:=\s*)?([0-9a-f]+)\s*$", listing, re.I | re.M)
    values = {name.lower(): int(value, 16) for name, value in matches}
    if len(matches) != 3 or set(values) != {"top", "ws", "lomem"}:
        raise ValueError("Cowgol CP/M assembly is missing its memory layout symbols")
    if values["top"] != 0x100 + image_bytes or values["ws"] != values["top"] + 128 or values["lomem"] < values["ws"]:
        raise ValueError("Cowgol CP/M assembly has an inconsistent code/stack/workspace layout")
    if values["lomem"] > 0xd000:
        raise ValueError("Cowgol CP/M code, stack and static workspace exceed the $0100–$CFFF area")
    return values


RISCV_RAM_END = 0x400000
RISCV_STATIC_END = RISCV_RAM_END - 0x8000  # runtime reserves 32 KiB for the stack


def riscv_image(artifact):
    """Validate the complete RV32 ELF before allocating any load segment.

    This matches Brickwright's four-MiB RISC-V machine. Static workspace is
    included through p_memsz, even when it occupies no bytes in the ELF file.
    The linker reserves the top 32 KiB for the runtime stack; segment growth
    must not silently consume that reservation.
    """
    if (len(artifact) < 52 or artifact[:7] != b"\x7fELF\x01\x01\x01"
            or struct.unpack_from("<HHI", artifact, 16) != (2, 243, 1)):
        raise ValueError("Cowgol RISC-V compiler did not produce a little-endian RV32 executable ELF")
    entry, phoff = struct.unpack_from("<II", artifact, 24)
    ehsize, phentsize, phnum = struct.unpack_from("<HHH", artifact, 40)
    if ehsize != 52 or phentsize != 32 or not 0 < phnum <= 32 or phoff < 52 or phoff + phnum * phentsize > len(artifact):
        raise ValueError("Cowgol RISC-V ELF has an invalid program-header table")
    loads = []
    for index in range(phnum):
        kind, offset, address, physical, file_size, memory_size, flags, alignment = struct.unpack_from(
            "<IIIIIIII", artifact, phoff + index * phentsize)
        if kind != 1:
            continue
        if (physical != address or not memory_size or file_size > memory_size or offset + file_size > len(artifact)
                or address < 0x1000 or address + memory_size > RISCV_STATIC_END):
            raise ValueError("Cowgol RISC-V code and static workspace exceed the $001000–$3F7FFF area or contain invalid segment sizes")
        if alignment > 1 and (alignment & (alignment - 1) or (address - offset) % alignment):
            raise ValueError("Cowgol RISC-V ELF has an invalid segment alignment")
        loads.append((address, memory_size, offset, file_size, flags))
    loads.sort()
    if (not loads or entry & 1
            or not any(address <= entry < address + size and flags & 1
                       for address, _memory, _offset, size, flags in loads)):
        raise ValueError("Cowgol RISC-V ELF entry is outside executable code")
    if any(left[0] + left[1] > right[0] for left, right in zip(loads, loads[1:])):
        raise ValueError("Cowgol RISC-V ELF has overlapping load segments")
    return {"entry": entry, "segments": [
        {"addr": address, "bytes": base64.b64encode(
            artifact[offset:offset + size] + bytes(memory - size)).decode("ascii")}
        for address, memory, offset, size, _flags in loads]}


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
    refusal = reject_source(req.code, spec.get("cross", spec["family"]))
    if refusal:
        return refusal
    status = bundle_status()
    if target not in status["targets"]:
        installation = ("install the Cowgol bundle and the service's riscv-gcc/picolibc runtime"
                        if spec.get("cross") == "riscv32" else "run scripts/build-cowgol-bundle.py")
        return {"success": False, "stage": "toolchain", "toolchain": "cowgol-" + spec.get("cross", spec["family"]),
                "error": f"Cowgol toolchain for {target} is not installed; {installation}"}
    try:
        bundle = _stage_bundle(bundle_root())
        with tempfile.TemporaryDirectory(prefix="cowgol-request-") as temporary:
            work = Path(temporary)
            source, cob, coo = [work / f"main.{ext}" for ext in ("cow", "cob", "coo")]
            source.write_text(req.code, encoding="utf-8")
            family = spec["family"]
            started, logs = time.monotonic(), []
            private_roots = [work, bundle]

            def run(command, env=None):
                with tempfile.TemporaryFile() as log:
                    remaining = max(0.01, TIMEOUT - (time.monotonic() - started))
                    process = subprocess.run([str(arg) for arg in command], stdin=subprocess.DEVNULL,
                                             stdout=log, stderr=subprocess.STDOUT, cwd=work,
                                             timeout=remaining, env=env)
                    log.seek(0)
                    text = log.read(65536).decode("utf-8", "replace")
                for private_root in private_roots:
                    text = text.replace(str(private_root) + os.sep, "")
                logs.append(text)
                if process.returncode:
                    raise RuntimeError(f"{Path(command[0]).name} exited with status {process.returncode}")

            # cowfe prepends include paths. Target overrides (notably cgen's
            # allocator) must be searched before the portable fallback.
            run([bundle / f"bin/cowfe-{family}", f"-I{bundle / 'rt'}/", f"-I{bundle / 'rt' / family}/", source, cob])
            run([bundle / f"bin/cowbe-{family}", cob, coo])
            output = work / f"main.{spec['extension']}"
            memory_layout = None
            image = None
            if family == "cgen":
                c_source = work / "main.c"
                run([bundle / "bin/cowlink-cgen", "-o", c_source, coo])
                generated = c_source.read_text()
                prefix = '#include "cowgol.h"\n'
                if not generated.startswith(prefix):
                    raise ValueError("unexpected Cowgol C output: missing runtime header")
                # Always declare the bridge: unused prototypes have no linker
                # cost; detecting a particular source spelling is unnecessary.
                notice = (bundle / "licenses/Cowgol-BSD-2-Clause.txt").read_text().replace("*/", "* /")
                definitions = ("#define COWGOL_NO_POSIX 1\n#define COWGOL_NO_ARGS 1\n"
                               "#define COWGOL_RAM_SIZE 65536\n") if spec.get("cross") else ""
                generated = ("/* Cowgol cgen output. Runtime: BSD-2-Clause.\n" + notice + "\n*/\n" + definitions
                             + (bundle / "rt/cgen/cowgol.h").read_text() + "\n"
                             + (bundle / "rt/cgen/brickwright.h").read_text() + "\n"
                             + generated[len(prefix):])
                if spec.get("cross") == "riscv32":
                    generated += "\n" + (bundle / "rt/cgen/brickwright-console.h").read_text()
                c_source.write_text(generated)
                if spec.get("cross") == "riscv32":
                    cross_root = _riscv_toolchain()
                    if cross_root is None:
                        raise ValueError("Cowgol RISC-V GCC and picolibc runtime are not installed")
                    private_roots.append(cross_root)
                    runtime = cross_root / "runtime"
                    env = dict(os.environ)
                    deps = cross_root / "lib-deps"
                    if deps.is_dir():
                        env["LD_LIBRARY_PATH"] = str(deps) + os.pathsep + env.get("LD_LIBRARY_PATH", "")
                    run([cross_root / "bin/riscv64-unknown-elf-gcc", "-std=gnu99",
                         "-march=rv32imac", "-mabi=ilp32", "-Os", "-fwrapv", "-fno-strict-aliasing",
                         "-ffunction-sections", "-fdata-sections", "-Wl,--gc-sections", "-nostdlib",
                         "-isystem", cross_root / "picolibc/include", "-T", runtime / "riscv-cc.ld",
                         runtime / "crt0.S", runtime / "console.c", c_source,
                         "-L" + str(cross_root / "picolibc/lib"), "-lc", "-lm", "-lgcc", "-o", output], env=env)
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
                    listing = work / "main.lst"
                    run([bundle / "bin/zmac", "-j", "-m", "-z" if family == "cpmz" else "-8", "-o", cim, "-o", listing, assembly])
                    memory_layout = cpm_memory(listing.read_text(), cim.stat().st_size)
                    cim.rename(output)
            maximum = 0xcf00 if spec.get("origin") == 0x100 else MAX_ARTIFACT_BYTES
            if not output.is_file() or not 0 < output.stat().st_size <= maximum:
                raise ValueError(f"Cowgol output is empty or exceeds {maximum} bytes for {target}")
            artifact = output.read_bytes()
            if family == "msdos" and artifact[:2] != b"MZ":
                raise ValueError("Cowgol DOS linker did not produce an MZ executable")
            if spec.get("cross") == "riscv32":
                image = riscv_image(artifact)
            response = {"success": True, "base64": base64.b64encode(artifact).decode("ascii"),
                        "filename": output.name, "bytes": len(artifact), "log": "".join(logs),
                        "language": "cowgol", "target": target, "format": spec["format"],
                        "toolchain": "cowgol-" + spec.get("cross", family),
                        "compiler_commit": status["commit"],
                        "artifact_kind": "source" if family == "cgen" and not spec.get("cross") else "executable"}
            if "origin" in spec:
                response["origin"] = spec["origin"]
                response["memory_end"] = memory_layout["lomem"]
                response["memory"] = (f"CP/M image {len(artifact)} bytes, stack 128 bytes, "
                                      f"static workspace {memory_layout['lomem'] - memory_layout['ws']} bytes; "
                                      f"end ${memory_layout['lomem']:04X} (limit $D000)")
            if image is not None:
                response.update(entry=image["entry"], image=image, mcu="rv32imac",
                                memory_end=max(segment["addr"] + len(base64.b64decode(segment["bytes"]))
                                               for segment in image["segments"]),
                                memory="RV32 console: code and static workspace below $3F8000; 64 KiB Cowgol arena; 32 KiB stack reservation")
            if family == "cgen" and not spec.get("cross"):
                response.update(c=artifact.decode("utf-8"), source_language="c")
            return response
    except subprocess.TimeoutExpired:
        return {"success": False, "stage": "compile", "error": f"Cowgol compile timed out after {TIMEOUT}s"}
    except (OSError, RuntimeError, ValueError, KeyError) as error:
        return {"success": False, "stage": "compile", "error": str(error),
                "log": "".join(locals().get("logs", []))}
