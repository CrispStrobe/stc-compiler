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
    "eater6502": {"description": "Cowgol for Brickwright EATER6502 (65C02 ROM, ACIA console and VIA GPIO)",
                  "family": "eater6502", "format": "bin", "extension": "bin", "origin": 0x8000},
    "atmega328p": {"description": "Cowgol via C for 16 MHz ATmega328P / Uno (UART, digital GPIO and blocking millisecond delays)",
                   "family": "cgen", "format": "hex", "extension": "hex", "cross": "atmega328p",
                   "board": "arduino-uno", "flash_limit": 0x7e00},
    "arduino-nano": {"description": "Cowgol for classic ATmega328P Nano at 16 MHz (2 KiB bootloader reservation)",
                     "family": "cgen", "format": "hex", "extension": "hex", "cross": "atmega328p",
                     "toolchain": "arduino-nano", "board": "arduino-nano", "flash_limit": 0x7800},
}
ALIASES = {"cgen": "cowgol-cgen", "cowgol-c": "cowgol-cgen", "cowgol-z80-cpm": "z80-cpm",
           "8080-cpm": "i8080-cpm", "cowgol-8080-cpm": "i8080-cpm", "i8086-dos": "dos",
           "arduino-uno": "atmega328p"}
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
    # These embedded machines have no filesystem. Do not expose a header
    # whose calls have no implementation in their runtime.
    allowed = INCLUDES - {"file.coh"} if family in ("riscv32", "eater6502", "atmega328p") else INCLUDES
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
        if spec.get("cross") in ("riscv32", "atmega328p"):
            files.append("rt/cgen/brickwright-console.h")
        if spec.get("cross") == "atmega328p":
            files += ["rt/cgen/brickwright-avr.h", "rt/cgen-avr/malloc.coh"]
    else:
        files.append(f"rt/{family}/cowgol.coo")
        tools += (["bin/nasm", "bin/djlink"] if family == "msdos"
                  else ["bin/64tass"] if family == "eater6502" else ["bin/zmac"])
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


def _avr_toolchain():
    """Use the existing independently staged AVR GCC and avr-libc bundle."""
    from app import stage_avr
    try:
        binary_directory = stage_avr()
    except OSError:
        return None
    if not binary_directory:
        return None
    root = Path(binary_directory).parent
    required = ("avr-gcc", "avr-objcopy", "avr-objdump")
    return root if all((Path(binary_directory) / name).is_file() for name in required) else None


def _cross_toolchain(spec):
    if spec.get("cross") == "riscv32":
        return _riscv_toolchain()
    if spec.get("cross") == "atmega328p":
        return _avr_toolchain()
    return None


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
                    and (not spec.get("cross") or _cross_toolchain(spec) is not None)):
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


def eater_memory(labels, artifact):
    """Prove the bare-metal ROM, RAM relocation and vector contract.

    64tass emits both decimal and $hex label values. The linker names the
    RAM end, workspace/data split and zero-page end so uninitialized storage
    cannot disappear from the artifact-size check.
    """
    names = ("LOMEM", "eater_workspace_end", "eater_zp_end", "eater_code_end",
             "eater_data_load", "eater_data_size", "eater_reset", "eater_irq", "eater_halt")
    pattern = r"^\s*(" + "|".join(names) + r")\s*=\s*(\$[0-9a-f]+|[0-9]+)\s*$"
    matches = re.findall(pattern, labels, re.M | re.I)
    values = {name.lower(): int(value[1:], 16) if value.startswith("$") else int(value)
              for name, value in matches}
    if len(matches) != len(names) or set(values) != {name.lower() for name in names}:
        raise ValueError("Cowgol EATER6502 assembly is missing its memory layout symbols")
    if len(artifact) != 32768:
        raise ValueError("Cowgol EATER6502 must produce an exact 32 KiB ROM")
    if not (0x200 <= values["eater_workspace_end"] <= values["lomem"] <= 0x3ff8
            and 0 <= values["eater_zp_end"] <= 0x100):
        raise ValueError("Cowgol EATER6502 workspace/data exceed RAM $0200–$3FF7 or zero page")
    if not (values["eater_reset"] == 0x8000
            and 0x8000 < values["eater_halt"] < values["eater_data_load"]
            and 0x8000 < values["eater_irq"] < values["eater_data_load"]
            and values["eater_data_load"] <= values["eater_code_end"] <= 0xfffa
            and values["eater_data_size"] == values["lomem"] - values["eater_workspace_end"]
            and values["eater_data_load"] + values["eater_data_size"] == values["eater_code_end"]):
        raise ValueError("Cowgol EATER6502 has an inconsistent ROM/data relocation layout")
    if struct.unpack_from("<HHH", artifact, 0x7ffa) != (values["eater_irq"], 0x8000, values["eater_irq"]):
        raise ValueError("Cowgol EATER6502 ROM hardware vectors do not match its executable code")
    if artifact[values["eater_halt"] - 0x8000] != 0xdb:
        raise ValueError("Cowgol EATER6502 ROM is missing its STP exit instruction")
    if artifact[values["eater_irq"] - 0x8000] != 0x40:
        raise ValueError("Cowgol EATER6502 ROM is missing its RTI interrupt handler")
    return values


RISCV_RAM_END = 0x400000
RISCV_STATIC_END = RISCV_RAM_END - 0x8000  # runtime reserves 32 KiB for the stack

AVR_FLASH_LIMIT = 0x7e00  # retain the Uno's 512-byte Optiboot area
AVR_RAM_START = 0x100
AVR_RAM_END = 0x900
AVR_STACK_RESERVE = 512
AVR_STATIC_END = AVR_RAM_END - AVR_STACK_RESERVE
AVR_HEAP_SIZE = 512


def avr_hex(artifact, flash_limit=AVR_FLASH_LIMIT):
    """Decode the single contiguous ATmega328P flash image, including checksums.

    avr-objcopy emits data and EOF records only for this <32 KiB image. Other
    record types, duplicate bytes and sparse images are outside this contract.
    """
    if flash_limit not in (0x7800, AVR_FLASH_LIMIT):
        raise ValueError("Cowgol AVR requires an explicitly qualified flash boundary")
    try:
        lines = artifact.decode("ascii").splitlines()
    except UnicodeDecodeError as error:
        raise ValueError("Cowgol ATmega328P artifact is not ASCII Intel HEX") from error
    image = bytearray()
    eof = False
    for line in lines:
        if not line or eof or not re.fullmatch(r":[0-9A-Fa-f]+", line) or len(line) % 2 != 1:
            raise ValueError("Cowgol ATmega328P HEX has malformed data or data after EOF")
        record = bytes.fromhex(line[1:])
        if len(record) < 5 or len(record) != record[0] + 5 or sum(record) & 0xff:
            raise ValueError("Cowgol ATmega328P HEX has an invalid length or checksum")
        length, address, kind = record[0], int.from_bytes(record[1:3], "big"), record[3]
        if kind == 1 and length == address == 0:
            eof = True
        elif kind == 0 and length and address == len(image) and address + length <= flash_limit:
            image.extend(record[4:-1])
        else:
            raise ValueError("Cowgol ATmega328P HEX exceeds flash, overlaps, is sparse, or uses unsupported records")
    if not eof or len(image) < 104 or len(image) % 2:
        raise ValueError("Cowgol ATmega328P HEX is missing its complete reset/interrupt image or EOF")
    # ATmega328P uses absolute JMP reset vectors. Flash is below 64 KiB, so
    # the instruction's high address bits must be zero.
    if image[:2] != b"\x0c\x94" or not 104 <= int.from_bytes(image[2:4], "little") * 2 < len(image):
        raise ValueError("Cowgol ATmega328P reset vector does not point into executable flash")
    return bytes(image)


def avr_memory(symbols, artifact, elf, flash_limit=AVR_FLASH_LIMIT):
    """Check actual linked storage, arena, reset and exit markers before export."""
    if (len(elf) < 52 or elf[:7] != b"\x7fELF\x01\x01\x01"
            or struct.unpack_from("<HHI", elf, 16) != (2, 83, 1)
            or struct.unpack_from("<I", elf, 24)[0] != 0):
        raise ValueError("Cowgol ATmega328P compiler did not produce an AVR executable ELF with reset entry zero")
    required = ("__data_start", "__data_end", "__bss_end", "__data_load_start", "__data_load_end",
                "__stack", "ram", "cowgol_avr_halted", "cowgol_avr_status")
    values, sizes = {}, {}
    for line in symbols.splitlines():
        fields = line.split()
        if len(fields) >= 5 and fields[-1] in required and re.fullmatch(r"[0-9a-fA-F]{8}", fields[0]):
            name = fields[-1]
            if name in values:
                raise ValueError("Cowgol ATmega328P assembly has duplicate memory symbols")
            values[name] = int(fields[0], 16)
            sizes[name] = int(fields[-2], 16)
    if set(values) != set(required):
        raise ValueError("Cowgol ATmega328P assembly is missing its memory layout symbols")
    image = avr_hex(artifact, flash_limit)
    ram_address = lambda name: values[name] - 0x800000
    data_start, data_end, bss_end = (ram_address(name) for name in ("__data_start", "__data_end", "__bss_end"))
    heap_start = ram_address("ram")
    heap_end = heap_start + AVR_HEAP_SIZE
    halted, status = (ram_address(name) for name in ("cowgol_avr_halted", "cowgol_avr_status"))
    if not (data_start == AVR_RAM_START <= data_end <= heap_start < heap_end <= bss_end <= AVR_STATIC_END
            and data_end <= halted < bss_end and data_end <= status < bss_end and halted != status
            and not heap_start <= halted < heap_end and not heap_start <= status < heap_end
            and sizes["ram"] == AVR_HEAP_SIZE and sizes["cowgol_avr_halted"] == sizes["cowgol_avr_status"] == 1
            and values["__stack"] == AVR_RAM_END - 1):
        raise ValueError("Cowgol ATmega328P static workspace/heap exceed SRAM or its 512-byte stack reservation")
    reset = int.from_bytes(image[2:4], "little") * 2
    if not (104 <= reset < values["__data_load_start"] <= values["__data_load_end"] == len(image) <= flash_limit
            and values["__data_load_end"] - values["__data_load_start"] == data_end - data_start):
        raise ValueError("Cowgol ATmega328P flash/data layout does not match its HEX image")
    return {"flash_end": len(image), "flash_limit": flash_limit,
            "data_start": data_start, "data_end": data_end, "bss_end": bss_end,
            "heap_start": heap_start, "heap_end": heap_end, "stack_start": AVR_STATIC_END,
            "stack_reserve": AVR_STACK_RESERVE, "ram_end": AVR_RAM_END,
            "halted_addr": halted, "status_addr": status}


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
                        if spec.get("cross") == "riscv32"
                        else "install the Cowgol bundle and the service's AVR GCC/avr-libc runtime"
                        if spec.get("cross") == "atmega328p" else "run scripts/build-cowgol-bundle.py")
        return {"success": False, "stage": "toolchain", "toolchain": "cowgol-" + spec.get("toolchain", spec.get("cross", spec["family"])),
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

            def run(command, env=None, capture_log=True):
                with tempfile.TemporaryFile() as log:
                    remaining = max(0.01, TIMEOUT - (time.monotonic() - started))
                    process = subprocess.run([str(arg) for arg in command], stdin=subprocess.DEVNULL,
                                             stdout=log, stderr=subprocess.STDOUT, cwd=work,
                                             timeout=remaining, env=env)
                    log.seek(0)
                    text = log.read(65536).decode("utf-8", "replace")
                for private_root in private_roots:
                    text = text.replace(str(private_root) + os.sep, "")
                if capture_log or process.returncode:
                    logs.append(text)
                if process.returncode:
                    raise RuntimeError(f"{Path(command[0]).name} exited with status {process.returncode}")
                return text

            # cowfe prepends include paths. Target overrides (notably cgen's
            # allocator) must be searched before the portable fallback.
            include_paths = [f"-I{bundle / 'rt'}/", f"-I{bundle / 'rt' / family}/"]
            if spec.get("cross") == "atmega328p":
                include_paths.append(f"-I{bundle / 'rt/cgen-avr'}/")
            run([bundle / f"bin/cowfe-{family}", *include_paths, source, cob])
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
                if spec.get("cross") == "atmega328p":
                    definitions = ("#define COWGOL_NO_POSIX 1\n#define COWGOL_NO_ARGS 1\n"
                                   "#define COWGOL_NO_MAIN 1\n#define COWGOL_RAM_SIZE 512\n")
                bindings = ""
                if spec.get("cross") in ("riscv32", "atmega328p"):
                    bindings = "\n" + (bundle / "rt/cgen/brickwright-console.h").read_text()
                if spec.get("cross") == "atmega328p":
                    bindings += "\n" + (bundle / "rt/cgen/brickwright-avr.h").read_text()
                generated = ("/* Cowgol cgen output. Runtime: BSD-2-Clause.\n" + notice + "\n*/\n" + definitions
                             + (bundle / "rt/cgen/cowgol.h").read_text() + "\n"
                             + (bundle / "rt/cgen/brickwright.h").read_text() + "\n"
                             + bindings + "\n" + generated[len(prefix):])
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
                elif spec.get("cross") == "atmega328p":
                    cross_root = _avr_toolchain()
                    if cross_root is None:
                        raise ValueError("Cowgol AVR GCC and avr-libc runtime are not installed")
                    private_roots.append(cross_root)
                    env = dict(os.environ)
                    deps = cross_root / "lib-deps"
                    if deps.is_dir():
                        env["LD_LIBRARY_PATH"] = str(deps) + os.pathsep + env.get("LD_LIBRARY_PATH", "")
                    elf = work / "main.elf"
                    run([cross_root / "bin/avr-gcc", "-mmcu=atmega328p", "-DF_CPU=16000000UL",
                         "-std=gnu99", "-Os", "-fwrapv", "-fno-strict-aliasing",
                         "-ffunction-sections", "-fdata-sections", "-Wl,--gc-sections",
                         f"-Wl,--defsym=__TEXT_REGION_LENGTH__={spec['flash_limit']}",
                         f"-Wl,--defsym=__DATA_REGION_LENGTH__={AVR_STATIC_END - AVR_RAM_START}",
                         c_source, "-o", elf], env=env)
                    run([cross_root / "bin/avr-objcopy", "-j", ".text", "-j", ".data", "-O", "ihex", elf, output], env=env)
                    symbols = run([cross_root / "bin/avr-objdump", "-t", elf], env=env, capture_log=False)
                    memory_layout = avr_memory(symbols, output.read_bytes(), elf.read_bytes(), spec["flash_limit"])
            else:
                assembly = work / ("main.asm" if family in ("msdos", "eater6502") else "main.z80")
                run([bundle / f"bin/cowlink-{family}", "-o", assembly, coo, bundle / f"rt/{family}/cowgol.coo"])
                if family == "msdos":
                    obj = work / "main.obj"
                    run([bundle / "bin/nasm", "-f", "obj", "-o", obj, assembly])
                    run([bundle / "bin/djlink", "-o", output, obj])
                elif family == "eater6502":
                    labels = work / "main.labels"
                    run([bundle / "bin/64tass", "--quiet", "--long-branch", "--ascii", "--case-sensitive",
                         "--nostart", "--labels", labels, "-o", output, assembly])
                    if not output.is_file() or output.stat().st_size != 32768:
                        raise ValueError("Cowgol EATER6502 must produce an exact 32 KiB ROM")
                    memory_layout = eater_memory(labels.read_text(), output.read_bytes())
                else:
                    # zmac selects its writer by suffix; .cim is the raw
                    # memory image format which CP/M calls .COM.
                    cim = work / "main.cim"
                    listing = work / "main.lst"
                    run([bundle / "bin/zmac", "-j", "-m", "-z" if family == "cpmz" else "-8", "-o", cim, "-o", listing, assembly])
                    memory_layout = cpm_memory(listing.read_text(), cim.stat().st_size)
                    cim.rename(output)
            maximum = (0xcf00 if spec.get("origin") == 0x100
                       else 32768 if family == "eater6502" else MAX_ARTIFACT_BYTES)
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
                        "toolchain": "cowgol-" + spec.get("toolchain", spec.get("cross", family)),
                        "compiler_commit": status["commit"],
                        "artifact_kind": "source" if family == "cgen" and not spec.get("cross") else "executable"}
            if spec.get("origin") == 0x100:
                response["origin"] = spec["origin"]
                response["memory_end"] = memory_layout["lomem"]
                response["memory"] = (f"CP/M image {len(artifact)} bytes, stack 128 bytes, "
                                      f"static workspace {memory_layout['lomem'] - memory_layout['ws']} bytes; "
                                      f"end ${memory_layout['lomem']:04X} (limit $D000)")
            if family == "eater6502":
                response.update(origin=0x8000, entry=0x8000, mcu="w65c02", memory_end=memory_layout["lomem"],
                                memory_layout=memory_layout,
                                memory=(f"EATER6502 ROM 32768 bytes; code/data end ${memory_layout['eater_code_end']:04X}; "
                                        f"RAM end ${memory_layout['lomem']:04X} (limit $3FF8); "
                                        f"zero-page end ${memory_layout['eater_zp_end']:02X}"))
            if image is not None:
                response.update(entry=image["entry"], image=image, mcu="rv32imac",
                                memory_end=max(segment["addr"] + len(base64.b64decode(segment["bytes"]))
                                               for segment in image["segments"]),
                                memory="RV32 console: code and static workspace below $3F8000; 64 KiB Cowgol arena; 32 KiB stack reservation")
            if spec.get("cross") == "atmega328p":
                response.update(origin=0, entry=0, mcu="atmega328p", f_cpu=16000000, uart_baud=9600,
                                board=spec["board"], bootloader_reserve=0x8000 - spec["flash_limit"],
                                memory_end=memory_layout["bss_end"], memory_layout=memory_layout,
                                memory=(f"{spec['board']} ATmega328P flash {memory_layout['flash_end']}/{spec['flash_limit']} bytes; "
                                        f"static SRAM {memory_layout['bss_end'] - AVR_RAM_START}/1536 bytes "
                                        "including 512-byte heap; 512-byte C stack reserved"))
            if family == "cgen" and not spec.get("cross"):
                response.update(c=artifact.decode("utf-8"), source_language="c")
            return response
    except subprocess.TimeoutExpired:
        return {"success": False, "stage": "compile", "error": f"Cowgol compile timed out after {TIMEOUT}s"}
    except (OSError, RuntimeError, ValueError, KeyError) as error:
        return {"success": False, "stage": "compile", "error": str(error),
                "log": "".join(locals().get("logs", []))}
