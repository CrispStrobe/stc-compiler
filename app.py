"""
stc-compiler — a REST compiler for the STC12C5A60S2 (and other 8051 parts).

POST C source, get back an Intel HEX image ready for stcgal. The heavy lifting
is SDCC's; this is a thin, stateless wrapper so a browser can reach it.

Shaped deliberately like CrispStrobe/legacy-lego-compiler so the BrickWright
extensions can talk to both with the same client code.
"""

import base64
import html
import json
import os
import re
import shutil
import stat
import sys
import subprocess
import tempfile
import uuid

import keil2sdcc
import stc_disasm
import stc_pseudocode
import stc_symtab

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from pydantic import BaseModel

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SRC_BIN = os.path.join(BASE_DIR, "bin")
SRC_SHARE = os.path.join(BASE_DIR, "share")

# Vercel's deployment directory is read-only and does not reliably preserve the
# executable bit, so the toolchain gets staged into /tmp once per container.
# We copy `share/` alongside `bin/` rather than pointing at the originals,
# because SDCC locates its headers and libraries as `<argv[0]>/../share/sdcc`.
# Keeping that relative layout intact means no path flags and no surprises.
STAGE = "/tmp/sdcc"
STAGE_BIN = os.path.join(STAGE, "bin")
STAGE_SHARE = os.path.join(STAGE, "share")

# A bare option value: numbers, identifiers, "0x1F", "a,b". Deliberately
# excludes '/' and '\\' so a path can never be smuggled in as an input file.
VALUE_RE = re.compile(r"[A-Za-z0-9_.,=+:-]+")

# `#include` naming a file outside the workspace. Blocking a path in `options`
# was never enough on its own: the SOURCE can name one, and a C compiler will
# read it and then quote the offending line back in its diagnostics. Since the
# compiler's output is returned to the caller, `#include "/proc/self/environ"`
# turns a compile service into a file-read primitive. Confirmed against the
# deployment with /etc/os-release before this was written.
#
# Angle-bracket includes are untouched: those resolve inside the vendored
# toolchain, which is the point of having one.
INCLUDE_RE = re.compile(r'^[ \t]*#[ \t]*include[ \t]*"([^"]*)"',
                        re.MULTILINE)


def too_large(*texts) -> dict | None:
    """Refuse input past MAX_SOURCE_BYTES.

    /compile and /translate-project already did this; the endpoints that only
    parse did not, on the reasoning that parsing is cheap. It is cheap per
    byte, which is not the same thing: this runs in a metered function with a
    memory ceiling, and an unbounded body is a way to spend someone else's
    quota. The limit is generous -- a megabyte of pseudocode is not a program.
    """
    total = sum(len((t or "").encode("utf-8", "replace")) for t in texts)
    if total > MAX_SOURCE_BYTES:
        return {"success": False,
                "error": f"input too large: {total} bytes, limit "
                         f"{MAX_SOURCE_BYTES}"}
    return None


def reject_path_options(options) -> dict | None:
    """No caller-supplied option may contain a path separator.

    Blocking a bare path was not enough on its own. `-I/etc` starts with a
    dash, so it went straight through -- and it moves where a RELATIVE
    `#include "os-release"` resolves to, which means the source-side check
    never sees a suspicious path at all. Same for -B, -L and --include-dir.

    Nothing this service legitimately accepts needs a slash: the memory and
    size flags come from TARGETS, and the shim's include paths from
    keil2sdcc.shim_args(). Neither passes through here.
    """
    for opt in options or ():
        opt = str(opt)
        if "/" in opt or "\\" in opt:
            return {"success": False, "stage": "options",
                    "error": f"options cannot contain a path; rejected {opt!r}"}
    return None


def reject_escaping_includes(source: str):
    """None if the source only includes files beside it, else an error dict."""
    for path in INCLUDE_RE.findall(source):
        cleaned = path.strip().replace("\\", "/")
        if (cleaned.startswith("/") or cleaned.startswith("~")
                or ".." in cleaned.split("/")
                or re.match(r"^[A-Za-z]:", cleaned)):
            return {"success": False, "stage": "source",
                    "error": f'#include "{path}" names a file outside the '
                             f"build directory. Only headers beside the source "
                             f"and the toolchain's own <angle-bracket> headers "
                             f"can be included."}
    return None

# Anything beyond this is a runaway, not a program.
COMPILE_TIMEOUT = 25
MAX_SOURCE_BYTES = 1_000_000

# Known parts. `defines` are handed to SDCC as -D flags; `flags` are the memory
# and size limits that make an image actually fit the chip.
TARGETS = {
    "stc12c5a60s2": {
        "flags": ["--iram-size", "256", "--xram-size", "1024",
                  "--code-size", "61440"],
        "description": "STC12C5A60S2 — 60 KB flash, 256 B IRAM, 1024 B XRAM",
    },
    "stc12c5a16s2": {
        "flags": ["--iram-size", "256", "--xram-size", "1024",
                  "--code-size", "16384"],
        "description": "STC12C5A16S2 — 16 KB flash",
    },
    "stc89c52rc": {
        "flags": ["--iram-size", "256", "--xram-size", "256",
                  "--code-size", "8192"],
        "description": "STC89C52RC — 8 KB flash, 256 B IRAM, 256 B XRAM, 12T",
    },
    "stc15f2k60s2": {
        "flags": ["--iram-size", "256", "--xram-size", "1792",
                  "--code-size", "61440"],
        "description": "STC15F2K60S2 — 60 KB flash, 256 B IRAM, 1792 B XRAM, 1T",
    },
    # Escape hatch: no size limits, caller supplies everything via `options`.
    "mcs51": {
        "flags": [],
        "description": "Generic 8051, no size limits applied",
    },
}

# ------------------------------------------------------------------ AVR side
#
# The second toolchain. avr-gcc is a separate bundle from SDCC's, staged
# separately and only when an AVR part is actually asked for, so an 8051
# request never pays for it.
#
# What is deliberately NOT here: the Arduino core. Its digitalWrite resolves
# the port at runtime, it is LGPL-2.1 (static linking into a returned .hex
# engages the relink obligation), and carrying arduino-cli plus the AVR core
# would be ~250 MB against Vercel's 250 MB function limit. stc_pseudocode's
# AvrTarget writes the ports directly instead, which is both smaller and the
# same discipline the 8051 target already uses.
SRC_AVR = os.path.join(BASE_DIR, "avr")
AVR_STAGE = "/tmp/avr"
AVR_STAGE_BIN = os.path.join(AVR_STAGE, "bin")

AVR_TARGETS = {
    "atmega328p": {
        "mcu": "atmega328p", "flash": 32768,
        "description": "ATmega328P — Arduino Uno/Nano/Pro Mini, 32 KB flash",
    },
    "atmega168p": {
        "mcu": "atmega168p", "flash": 16384,
        "description": "ATmega168P — 16 KB flash",
    },
    "atmega2560": {
        "mcu": "atmega2560", "flash": 262144,
        "description": "ATmega2560 — Arduino Mega, 256 KB flash",
    },
    "attiny85": {
        "mcu": "attiny85", "flash": 8192,
        "description": "ATtiny85 — 8 KB flash, no hardware UART",
    },
}

# ---- ARM (Cortex-M) toolchain, same pattern as AVR above ---------------------
# The bundle is built by scripts/fetch-arm-gcc.sh from Debian bullseye
# packages, trimmed to the v6-m multilib only (Cortex-M0/M0+). No newlib,
# no C++, only libgcc's integer helpers (division, shifts).
SRC_ARM = os.path.join(BASE_DIR, "arm")
ARM_STAGE = "/tmp/arm"
ARM_STAGE_BIN = os.path.join(ARM_STAGE, "bin")
ARM_LINKER_SCRIPT = os.path.join(BASE_DIR, "pico-sram.ld")

ARM_TARGETS = {
    "rp2040": {
        "mcu": "cortex-m0plus", "sram": 256 * 1024,
        "description": "RP2040 — Raspberry Pi Pico, 264 KB SRAM, Cortex-M0+",
    },
}

# ---- cc65 toolchain (6502 assembler/linker) ---------------------------------
SRC_CC65 = os.path.join(BASE_DIR, "cc65")
CC65_STAGE = "/tmp/cc65"
CC65_STAGE_BIN = os.path.join(CC65_STAGE, "bin")

app = FastAPI(title="stc-compiler", docs_url="/docs")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


def stage_toolchain():
    """Copy the toolchain into /tmp on cold start. Cheap and idempotent.
    A PARTIAL stage (interrupted copy: bin/ exists, sdcc missing) used to
    stick forever — the copy was skipped because the dir existed. Heal it
    by wiping and recopying."""
    if os.path.isdir(STAGE_BIN) and os.path.exists(os.path.join(STAGE_BIN, "sdcc")):
        return
    os.makedirs(STAGE, exist_ok=True)
    if os.path.isdir(STAGE_BIN) and not os.path.exists(os.path.join(STAGE_BIN, "sdcc")):
        shutil.rmtree(STAGE_BIN, ignore_errors=True)
    if not os.path.isdir(STAGE_BIN):
        shutil.copytree(SRC_BIN, STAGE_BIN)
    if not os.path.isdir(STAGE_SHARE):
        shutil.copytree(SRC_SHARE, STAGE_SHARE)
    for name in os.listdir(STAGE_BIN):
        path = os.path.join(STAGE_BIN, name)
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _make_executable(directory: str):
    for name in os.listdir(directory):
        path = os.path.join(directory, name)
        if os.path.isfile(path):
            os.chmod(path, os.stat(path).st_mode
                     | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


_EXEC_PROBE: dict = {}


def _can_execute(path: str) -> bool:
    """Whether a staged binary actually runs on THIS host. The vendored
    bundles are linux-x86_64 for Vercel; on a mac dev machine they exist
    but exec fails (Errno 8), and returning them anyway shadowed a
    perfectly good Homebrew toolchain on PATH. Probed once, cached."""
    if path in _EXEC_PROBE:
        return _EXEC_PROBE[path]
    try:
        subprocess.run([path, "--version"], capture_output=True, timeout=10)
        _EXEC_PROBE[path] = True
    except OSError:
        _EXEC_PROBE[path] = False
    except Exception:
        _EXEC_PROBE[path] = True   # ran but complained: still executable
    return _EXEC_PROBE[path]


def sdcc_bin_dir() -> str:
    """Where the 8051 toolchain lives ON THIS HOST: the staged bundle in
    production (linux-x86_64), the PATH sdcc on a dev machine where the
    vendored binaries cannot exec. Mirrors stage_avr's dual resolution."""
    stage_toolchain()
    staged = os.path.join(STAGE_BIN, "sdcc")
    if os.path.exists(staged) and _can_execute(staged):
        return STAGE_BIN
    found = shutil.which("sdcc")
    return os.path.dirname(found) if found else STAGE_BIN


def stage_avr() -> str | None:
    """Directory holding avr-gcc, or None if no AVR toolchain is reachable.

    Two ways this resolves, and both matter. In production the bundle is
    vendored under avr/ and staged into /tmp exactly as SDCC's is, because
    Vercel's deployment directory is read-only and drops the executable bit.
    In local development there is usually a system avr-gcc on PATH (Homebrew's
    avr-gcc, Debian's gcc-avr), and falling back to it means the AVR path can
    be exercised without first building a Linux bundle.

    Neither case needs -B or -isystem. fetch-avr-gcc.sh lays the bundle out as
    a real cross-toolchain prefix ($prefix/$target/{bin,include,lib}), so the
    driver resolves cc1, `as`, `ld` and avr-libc by itself. That is deliberate:
    scripts/verify-avr.sh compiles with no flags either, so what CI proves is
    what this function actually runs. Papering over a bad layout with flags
    here would mean the verifier no longer verifies anything.
    """
    if os.path.isdir(SRC_AVR):
        if not os.path.exists(os.path.join(AVR_STAGE_BIN, "avr-gcc")):
            os.makedirs(AVR_STAGE, exist_ok=True)
            for part in os.listdir(SRC_AVR):
                source = os.path.join(SRC_AVR, part)
                destination = os.path.join(AVR_STAGE, part)
                if os.path.isdir(source) and not os.path.isdir(destination):
                    shutil.copytree(source, destination, symlinks=True)
                elif os.path.isfile(source) and not os.path.exists(destination):
                    shutil.copy2(source, destination)
            # cc1, collect2, as and ld are all fork/exec'd and all lose the
            # executable bit on the way through Vercel's deployment.
            for sub in ("bin", os.path.join("lib", "avr", "bin"),
                        os.path.join("lib", "gcc")):
                for root, _dirs, _files in os.walk(os.path.join(AVR_STAGE, sub)):
                    _make_executable(root)
        staged = os.path.join(AVR_STAGE_BIN, "avr-gcc")
        if os.path.exists(staged) and _can_execute(staged):
            return AVR_STAGE_BIN

    found = shutil.which("avr-gcc")
    return os.path.dirname(found) if found else None


def stage_arm() -> str | None:
    """Directory holding arm-none-eabi-gcc, or None if unavailable.

    Same cold-start staging as stage_avr — the bundle ships in git, gets
    copied into /tmp on Vercel (read-only deployment dir, executable bit
    stripped), and falls back to a system arm-none-eabi-gcc for local dev.
    """
    # The vendored bundle is LINUX binaries (built for Vercel's runtime).
    # On any other platform staging it yields tools that cannot exec
    # (ENOEXEC on a dev Mac) — go straight to the system toolchain there.
    if sys.platform.startswith("linux") and os.path.isdir(SRC_ARM):
        if not os.path.exists(os.path.join(ARM_STAGE_BIN, "arm-none-eabi-gcc")):
            os.makedirs(ARM_STAGE, exist_ok=True)
            for part in os.listdir(SRC_ARM):
                source = os.path.join(SRC_ARM, part)
                destination = os.path.join(ARM_STAGE, part)
                if os.path.islink(source):
                    if not os.path.exists(destination):
                        linkto = os.readlink(source)
                        os.symlink(linkto, destination)
                elif os.path.isdir(source) and not os.path.isdir(destination):
                    shutil.copytree(source, destination, symlinks=True)
                elif os.path.isfile(source) and not os.path.exists(destination):
                    shutil.copy2(source, destination)
            for sub in ("bin",
                        os.path.join("lib", "arm-none-eabi", "bin"),
                        os.path.join("lib", "gcc")):
                for root, _dirs, _files in os.walk(
                        os.path.join(ARM_STAGE, sub)):
                    _make_executable(root)
        staged_arm = os.path.join(ARM_STAGE_BIN, "arm-none-eabi-gcc")
        if os.path.exists(staged_arm) and _can_execute(staged_arm):
            return ARM_STAGE_BIN

    found = shutil.which("arm-none-eabi-gcc")
    return os.path.dirname(found) if found else None


def stage_cc65() -> str | None:
    """Directory holding ca65/ld65, or None if unavailable.

    Same pattern as stage_arm: Linux-only staging from a vendored bundle,
    falling back to system ca65 for local dev on macOS/Homebrew.
    """
    if sys.platform.startswith("linux") and os.path.isdir(SRC_CC65):
        if not os.path.exists(os.path.join(CC65_STAGE_BIN, "ca65")):
            os.makedirs(CC65_STAGE, exist_ok=True)
            for part in os.listdir(SRC_CC65):
                source = os.path.join(SRC_CC65, part)
                destination = os.path.join(CC65_STAGE, part)
                if os.path.isdir(source) and not os.path.isdir(destination):
                    shutil.copytree(source, destination, symlinks=True)
                elif os.path.isfile(source) and not os.path.exists(destination):
                    shutil.copy2(source, destination)
            for root, _dirs, _files in os.walk(CC65_STAGE_BIN):
                _make_executable(root)
        staged = os.path.join(CC65_STAGE_BIN, "ca65")
        if os.path.exists(staged) and _can_execute(staged):
            return CC65_STAGE_BIN

    found = shutil.which("ca65")
    return os.path.dirname(found) if found else None


class CompileReq(BaseModel):
    code: str
    # "c" compiles the body as-is; "pseudocode" runs it through the
    # BrickWright-style front end first (see stc_pseudocode.py); "keil"
    # translates the Keil C51 dialect into SDCC's (see keil2sdcc.py).
    language: str = "c"
    target: str = "stc12c5a60s2"
    # Clock in Hz. Emitted as -DFOSC_HZ=<n>UL, which is what the
    # stc12c5a60s2-lab examples expect. None omits it entirely.
    fosc: int | None = 11059200
    # Extra -D defines, as {"NAME": "value"} or {"NAME": None} for bare defines.
    defines: dict[str, str | None] = {}
    # Raw extra SDCC flags, e.g. ["--model-large", "--opt-code-size"].
    options: list[str] = []
    # "ihx" is SDCC's raw output; "hex" runs it through packihx; "bin" through
    # makebin. stcgal accepts hex and bin.
    format: str = "hex"
    # Also return an 8051 disassembly of the image. Off by default: it is
    # several KB of text nobody asked for.
    disassemble: bool = False
    # Also return the debug symbol table (stc_symtab): the addresses of bw_ms,
    # <task>_state and <task>_until, and the code address of every yield point.
    # A browser cannot run sdcc or Python, so this endpoint is the ONLY way a
    # front end can obtain one — and without it a debugger can halt a program
    # but not say where it stopped.
    #
    # This adds --debug to the compile, and --debug CHANGES THE IMAGE: measured
    # on a two-task fixture, 8 of 39 hex records differ and the image grows by
    # two bytes, because SDCC stops tail-merging returns so that line records
    # map cleanly (a `JNB/RET` pair becomes `JB/SJMP`). The program behaves the
    # same — it is the same C — but it is not the same bytes.
    #
    # So a symbol table is only ever valid for the image it was built WITH.
    # Never pair one with a separately compiled release image: every code
    # address in it would be off, and a breakpoint would land somewhere
    # arbitrary. Ask for both together, or neither.
    symbols: bool = False


def build_avr(req: CompileReq, spec: dict, generated_c: str | None,
              f_cpu: int | None, stem: str = "main") -> dict:
    """Compile C for an ATmega with avr-gcc, and return an Intel HEX image.

    Deliberately the same response shape as the SDCC path -- base64 image,
    filename, bytes, log, memory -- so a client that already speaks to this
    service does not learn a second one.
    """
    refusal = reject_path_options(req.options)
    if refusal:
        return refusal

    bin_dir = stage_avr()
    if bin_dir is None:
        return {"success": False, "stage": "compile",
                "error": "no AVR toolchain available; the avr/ bundle is not "
                         "vendored in this deployment and no avr-gcc is on PATH",
                "c": generated_c}

    # cc1 links against libmpc/libmpfr/libgmp and the binutils against libz,
    # none of which exist on Vercel's Amazon Linux 2023. They are vendored in
    # lib-deps/; without this the driver starts and cc1 dies with
    # "error while loading shared libraries". Harmless where the system
    # already provides them -- ours simply win.
    deps = os.path.join(os.path.dirname(bin_dir), "lib-deps")
    env = dict(os.environ)
    if os.path.isdir(deps):
        env["LD_LIBRARY_PATH"] = deps + os.pathsep + env.get("LD_LIBRARY_PATH", "")

    work = os.path.join(tempfile.gettempdir(), f"build-{uuid.uuid4().hex}")
    os.makedirs(work, exist_ok=True)
    src = os.path.join(work, "main.c")
    with open(src, "w", encoding="utf-8") as handle:
        handle.write(req.code)

    elf = os.path.join(work, "main.elf")
    cmd = [os.path.join(bin_dir, "avr-gcc"), f"-mmcu={spec['mcu']}",
           "-Os", "-std=c99", "-Wall", "-Wextra",
           # The cooperative scheduler is a Duff's device: every yield state
           # is a `case` the previous statement falls into. That is the
           # lowering, not a mistake, and SDCC does not warn about it either.
           "-Wno-implicit-fallthrough",
           # gcc-avr 5.4 reports "promoted ~unsigned is always non-zero" for
           # an active-low whole-port read, `(unsigned char)~PINC > 0`. The
           # cast is what makes it a byte and the code is correct; the check
           # fires on every spelling of the complement, so it is the check
           # that goes rather than the expression.
           "-Wno-sign-compare",
           "-ffunction-sections", "-fdata-sections", "-Wl,--gc-sections"]
    # Source that already sets its own clock wins: generated code bakes F_CPU
    # in, and defining it twice from the command line is a warning at best and
    # a conflicting redefinition at worst.
    if f_cpu and "F_CPU" not in req.code:
        cmd.append(f"-DF_CPU={int(f_cpu)}UL")
    if req.symbols or req.disassemble:
        # DWARF is the AVR's .cdb: the line records avr_symtab joins the
        # yield map against. DWARF-2 SPECIFICALLY: gcc 5.4's default DWARF-4
        # produces a .debug_line the bundled binutils 2.26 objdump decodes to
        # NOTHING (section present, zero rows — found live, not guessed).
        # Debug info changes nothing about the image bytes; the image and
        # the table still come from the SAME request, as the 8051 path
        # insists.
        cmd.append("-gdwarf-2")
    for name, value in req.defines.items():
        if not name.replace("_", "").isalnum():
            shutil.rmtree(work, ignore_errors=True)
            return {"success": False, "error": f"bad define name: {name!r}"}
        cmd.append(f"-D{name}" if value is None else f"-D{name}={value}")
    for index, opt in enumerate(req.options):
        opt = str(opt)
        if opt.startswith("-"):
            cmd.append(opt)
            continue
        if index == 0 or not VALUE_RE.fullmatch(opt):
            shutil.rmtree(work, ignore_errors=True)
            return {"success": False,
                    "error": f"options must be flags, or plain values following "
                             f"a flag; rejected {opt!r}"}
        cmd.append(opt)
    cmd += ["-o", elf, src]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=COMPILE_TIMEOUT, cwd=work, env=env)
    except subprocess.TimeoutExpired:
        shutil.rmtree(work, ignore_errors=True)
        return {"success": False, "error": f"compile timed out after {COMPILE_TIMEOUT}s"}

    log = ((result.stdout or "") + (result.stderr or "")).replace(work + os.sep, "")
    if result.returncode != 0 or not os.path.exists(elf):
        shutil.rmtree(work, ignore_errors=True)
        return {"success": False, "error": log.strip() or "compilation failed",
                "log": log, "c": generated_c, "stage": "compile"}

    try:
        objcopy = os.path.join(bin_dir, "avr-objcopy")
        if req.format == "bin":
            out, name, args = (os.path.join(work, "main.bin"),
                               f"{stem}.bin", ["-O", "binary"])
        else:
            # "ihx" and "hex" are one format here: avr-objcopy emits Intel HEX
            # directly, so there is no packihx step to distinguish them.
            out, name, args = (os.path.join(work, "main.hex"),
                               f"{stem}.hex", ["-O", "ihex"])
        subprocess.run([objcopy, *args, "-R", ".eeprom", elf, out],
                       capture_output=True, timeout=10, env=env)
        if not os.path.exists(out):
            return {"success": False, "error": "avr-objcopy produced no image",
                    "log": log, "c": generated_c}
        with open(out, "rb") as handle:
            blob = handle.read()

        # avr-size's own report, so a caller can warn before an image outgrows
        # the part -- the counterpart to SDCC's .mem file.
        mem = ""
        try:
            sized = subprocess.run(
                [os.path.join(bin_dir, "avr-size"), f"--mcu={spec['mcu']}",
                 "--format=avr", elf],
                capture_output=True, text=True, timeout=10, env=env)
            mem = sized.stdout or ""
        except (OSError, subprocess.SubprocessError):
            mem = ""

        symbols = None
        symbols_error = None
        if req.symbols:
            try:
                # objdump -t, not nm: the vendored bundle ships objdump
                # only, and avr_symtab.parse_nm reads both spellings.
                nm = subprocess.run(
                    [os.path.join(bin_dir, "avr-objdump"), "-t", elf],
                    capture_output=True, text=True, timeout=10, env=env)
                decoded = subprocess.run(
                    [os.path.join(bin_dir, "avr-objdump"),
                     "--dwarf=decodedline", elf],
                    capture_output=True, text=True, timeout=15, env=env)
                import avr_symtab
                try:
                    symbols = avr_symtab.build_symbol_table(
                        nm.stdout or "", decoded.stdout or "", req.code,
                        f_cpu=int(f_cpu or 16000000), mcu=spec["mcu"])
                except Exception as inner:
                    # Carry the evidence: the first lines of what the tools
                    # actually said beats guessing at a distance.
                    head = lambda t: " / ".join((t or "").splitlines()[3:14])[:600]
                    raise RuntimeError(
                        f"{inner} [objdump-t: {head(nm.stdout) or head(nm.stderr) or 'EMPTY'}]"
                        f" [decodedline: {head(decoded.stdout) or head(decoded.stderr) or 'EMPTY'}]")
            except Exception as exc:  # noqa: BLE001 - reported, never fatal
                symbols_error = str(exc)

        listing = None
        listing_artifact = None
        if req.disassemble:
            import listing as listing_mod
            listing_artifact = listing_mod.from_objdump(
                bin_dir, "avr", elf, env, "avr-gcc")
            listing = listing_artifact.get("asm")
            if not listing:
                listing = f"(disassembly failed: {listing_artifact.get('error', 'unknown')})"

        return {
            "success": True,
            "c": generated_c,
            "translated": None,
            "unresolved": None,
            "warnings": None,
            "disassembly": listing,
            "listing": listing_artifact,
            "base64": base64.b64encode(blob).decode("ascii"),
            "filename": name,
            "bytes": len(blob),
            "log": log,
            "memory": mem,
            "symbols": symbols,
            "symbols_error": symbols_error,
            "toolchain": "avr-gcc",
            "mcu": spec["mcu"],
        }
    finally:
        shutil.rmtree(work, ignore_errors=True)


def build_arm(req: CompileReq, spec: dict, generated_c: str | None,
              stem: str = "main") -> dict:
    """Compile C for an RP2040 (Cortex-M0+) with arm-none-eabi-gcc.

    Same response shape as build_avr: base64 image, filename, bytes, log,
    memory, plus origin (the SRAM load address) and entry (the ELF entry
    point, which must equal the origin for the emulator to work).

    The image is a raw binary (objcopy -O binary), not Intel HEX — the
    emulator loads it directly into SRAM at 0x20000000. bss is not in the
    file; the emulator zero-fills SRAM before loading.
    """
    refusal = reject_path_options(req.options)
    if refusal:
        return refusal

    bin_dir = stage_arm()
    if bin_dir is None:
        return {"success": False, "stage": "compile",
                "error": "no ARM toolchain available; the arm/ bundle is not "
                         "vendored in this deployment and no arm-none-eabi-gcc "
                         "is on PATH",
                "c": generated_c}

    deps = os.path.join(os.path.dirname(bin_dir), "lib-deps")
    env = dict(os.environ)
    if os.path.isdir(deps):
        env["LD_LIBRARY_PATH"] = deps + os.pathsep + env.get("LD_LIBRARY_PATH", "")

    work = os.path.join(tempfile.gettempdir(), f"build-{uuid.uuid4().hex}")
    os.makedirs(work, exist_ok=True)
    src = os.path.join(work, "main.c")
    with open(src, "w", encoding="utf-8") as handle:
        handle.write(req.code)

    # The linker script lives in the repo root, but on Vercel the deployment
    # dir is read-only. Copy it into the work dir so the linker can find it
    # relative to cwd and without the source path leaking into diagnostics.
    ld_script = os.path.join(work, "pico-sram.ld")
    shutil.copy2(ARM_LINKER_SCRIPT, ld_script)

    elf = os.path.join(work, "main.elf")
    cmd = [os.path.join(bin_dir, "arm-none-eabi-gcc"),
           f"-mcpu={spec['mcu']}", "-mthumb", "-Os", "-ffreestanding",
           "-ffunction-sections", "-nostdlib",
           # Duff's device scheduler: same intentional fallthroughs as AVR/8051
           "-Wno-implicit-fallthrough",
           f"-T{ld_script}",
           ]
    if req.symbols or req.disassemble:
        # Same DWARF-2 trap as AVR: gcc 8's default DWARF-4 produces line
        # records the bundled binutils 2.35 objdump decodes to nothing.
        cmd.append("-gdwarf-2")
    for name, value in req.defines.items():
        if not name.replace("_", "").isalnum():
            shutil.rmtree(work, ignore_errors=True)
            return {"success": False, "error": f"bad define name: {name!r}"}
        cmd.append(f"-D{name}" if value is None else f"-D{name}={value}")
    for index, opt in enumerate(req.options):
        opt = str(opt)
        if opt.startswith("-"):
            cmd.append(opt)
            continue
        if index == 0 or not VALUE_RE.fullmatch(opt):
            shutil.rmtree(work, ignore_errors=True)
            return {"success": False,
                    "error": f"options must be flags, or plain values following "
                             f"a flag; rejected {opt!r}"}
        cmd.append(opt)
    # -lgcc is REQUIRED: the cooperative scheduler does 64-bit division
    # (__aeabi_uldivmod) for bw_now, and the compiler may emit other
    # runtime helpers for shifts and multiplies.
    cmd += ["-o", elf, src, "-lgcc"]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=COMPILE_TIMEOUT, cwd=work, env=env)
    except subprocess.TimeoutExpired:
        shutil.rmtree(work, ignore_errors=True)
        return {"success": False, "error": f"compile timed out after {COMPILE_TIMEOUT}s"}

    log = ((result.stdout or "") + (result.stderr or "")).replace(work + os.sep, "")
    # The linker emits "LOAD segment with RWX permissions" for the SRAM
    # image — harmless, expected, not worth suppressing because doing so
    # would also hide real warnings.
    if result.returncode != 0 or not os.path.exists(elf):
        shutil.rmtree(work, ignore_errors=True)
        return {"success": False, "error": log.strip() or "compilation failed",
                "log": log, "c": generated_c, "stage": "compile"}

    try:
        objcopy = os.path.join(bin_dir, "arm-none-eabi-objcopy")
        objdump = os.path.join(bin_dir, "arm-none-eabi-objdump")
        origin = 0x20000000

        # Check the ELF entry point: main must land at the origin for the
        # emulator (which jumps to 0x20000000 on reset). Thumb bit (bit 0)
        # is expected and stripped for the comparison.
        try:
            hdr = subprocess.run(
                [objdump, "-f", elf], capture_output=True, text=True,
                timeout=10, env=env)
            m = re.search(r"start address\s+0x([0-9a-fA-F]+)", hdr.stdout or "")
            entry = int(m.group(1), 16) if m else None
            if entry is not None and (entry & ~1) != origin:
                shutil.rmtree(work, ignore_errors=True)
                return {"success": False, "stage": "link",
                        "error": f"ELF entry 0x{entry:08x} is not at the SRAM "
                                 f"origin 0x{origin:08x}. main must be the "
                                 f"first function — check the linker script.",
                        "log": log, "c": generated_c}
        except (OSError, subprocess.SubprocessError):
            entry = None

        # Raw binary: text + rodata + data, contiguous. bss is NOT in the
        # file — the emulator zero-fills SRAM.
        out = os.path.join(work, "main.bin")
        subprocess.run([objcopy, "-O", "binary", elf, out],
                       capture_output=True, timeout=10, env=env)
        if not os.path.exists(out):
            return {"success": False, "error": "arm-none-eabi-objcopy produced no image",
                    "log": log, "c": generated_c}
        with open(out, "rb") as handle:
            blob = handle.read()

        # Size report
        mem = ""
        try:
            sized = subprocess.run(
                [os.path.join(bin_dir, "arm-none-eabi-size"), elf],
                capture_output=True, text=True, timeout=10, env=env)
            mem = sized.stdout or ""
        except (OSError, subprocess.SubprocessError):
            mem = ""

        # Symbol extraction: same objdump -t + --dwarf=decodedline as AVR.
        # ARM addresses are absolute — no 0x800000 strip (avr_symtab handles
        # this via its DATA_VMA constant; for ARM we use arm_symtab which
        # has DATA_VMA = 0).
        symbols = None
        symbols_error = None
        if req.symbols:
            try:
                nm = subprocess.run(
                    [objdump, "-t", elf],
                    capture_output=True, text=True, timeout=10, env=env)
                decoded = subprocess.run(
                    [objdump, "--dwarf=decodedline", elf],
                    capture_output=True, text=True, timeout=15, env=env)
                import avr_symtab
                try:
                    symbols = avr_symtab.build_symbol_table(
                        nm.stdout or "", decoded.stdout or "", req.code,
                        f_cpu=125000000, mcu="rp2040",
                        data_vma_override=0)
                except Exception as inner:
                    head = lambda t: " / ".join((t or "").splitlines()[3:14])[:600]
                    raise RuntimeError(
                        f"{inner} [objdump-t: {head(nm.stdout) or head(nm.stderr) or 'EMPTY'}]"
                        f" [decodedline: {head(decoded.stdout) or head(decoded.stderr) or 'EMPTY'}]")
            except Exception as exc:  # noqa: BLE001 - reported, never fatal
                symbols_error = str(exc)

        listing = None
        listing_artifact = None
        if req.disassemble:
            import listing as listing_mod
            listing_artifact = listing_mod.from_objdump(
                bin_dir, "arm-none-eabi", elf, env, "arm-gcc")
            listing = listing_artifact.get("asm")
            if not listing:
                listing = f"(disassembly failed: {listing_artifact.get('error', 'unknown')})"

        return {
            "success": True,
            "c": generated_c,
            "translated": None,
            "unresolved": None,
            "warnings": None,
            "disassembly": listing,
            "listing": listing_artifact,
            "base64": base64.b64encode(blob).decode("ascii"),
            "filename": f"{stem}.bin",
            "bytes": len(blob),
            "log": log,
            "memory": mem,
            "symbols": symbols,
            "symbols_error": symbols_error,
            "toolchain": "arm-none-eabi-gcc",
            "mcu": "rp2040",
            "origin": origin,
            "entry": entry,
        }
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _fosc_from_source(code: str) -> int | None:
    """The clock, read back out of the source.

    The pseudocode path deliberately clears `fosc` on the request because the
    CLOCK line already baked it into the generated C, and defining it twice is
    a warning at best. The symbol table still wants the number, and the C is
    where it now lives.
    """
    m = re.search(r"^\s*#define\s+FOSC_HZ\s+(\d+)", code, re.M)
    return int(m.group(1)) if m else None


def build(req: CompileReq) -> dict:
    """Compile and return the JSON-shaped result. Shared by both endpoints."""
    if len(req.code.encode("utf-8")) > MAX_SOURCE_BYTES:
        return {"success": False, "error": "source too large"}

    refusal = reject_escaping_includes(req.code) or reject_path_options(req.options)
    if refusal:
        return refusal

    stem = "main"
    generated_c = None
    keil_changes: dict = {}
    keil_unresolved: list = []
    keil_warnings: list = []
    if req.language.lower() in ("pseudocode", "pseudo", "bw"):
        try:
            generated_c, program = stc_pseudocode.transpile(req.code)
        except stc_pseudocode.PseudocodeError as exc:
            return {"success": False, "error": str(exc), "line": exc.line,
                    "stage": "transpile"}
        # Transpiling is target-agnostic; compiling is not. The DEVICE line --
        # not the request's `target` field -- decides which toolchain the
        # generated source needs, because it is what decided the dialect.
        chip = program.target
        stem = program.name or "main"
        req = req.model_copy(update={"code": generated_c})
        # The pseudocode's own CLOCK wins; the clock is already baked into the
        # generated C as FOSC_HZ or F_CPU, and defining it twice is a warning
        # at best.
        req = req.model_copy(update={"fosc": None})
        if chip.toolchain == "avr-gcc":
            return build_avr(req, AVR_TARGETS[chip.key], generated_c, None, stem)
        if chip.toolchain == "arm-none-eabi-gcc":
            return build_arm(req, ARM_TARGETS[chip.key], generated_c, stem)
        if chip.toolchain != "sdcc-mcs51":
            return {"success": False, "stage": "compile",
                    "error": f"{chip.display} transpiles here, but building it "
                             f"needs {chip.toolchain}, which this service does "
                             f"not host. Use /transpile to get the source."
                             + (f" {chip.compile_hint}" if chip.compile_hint else ""),
                    "c": generated_c,
                    "toolchain": chip.toolchain,
                    "language": stc_pseudocode.source_language(program),
                    "filename": f"{stem}.{chip.source_extension}"}
    elif req.language.lower() in ("keil", "c51"):
        stage_toolchain()      # SFR addresses come from the staged SDCC headers
        result = keil2sdcc.translate(req.code)
        generated_c = result.text
        keil_changes = result.changes
        keil_unresolved = result.unresolved
        keil_warnings = result.warnings
        req = req.model_copy(update={"code": generated_c})
    elif req.language.lower() != "c":
        return {"success": False,
                "error": f"unknown language '{req.language}'; use 'c' or 'pseudocode'"}

    if req.format not in ("ihx", "hex", "bin"):
        return {"success": False, "error": "format must be ihx, hex or bin"}

    # Hand-written C reaches an AVR part through the `target` field, since
    # there is no DEVICE line to read it from. Keil is 8051-only by
    # definition, so it never routes here.
    avr = AVR_TARGETS.get(req.target.lower())
    if avr is not None:
        if keil_changes:
            return {"success": False,
                    "error": "the Keil C51 dialect is 8051-only; it cannot be "
                             f"compiled for {avr['mcu']}"}
        return build_avr(req, avr, generated_c, req.fosc)

    arm = ARM_TARGETS.get(req.target.lower())
    if arm is not None:
        if keil_changes:
            return {"success": False,
                    "error": "the Keil C51 dialect is 8051-only; it cannot be "
                             f"compiled for {arm['mcu']}"}
        return build_arm(req, arm, generated_c)

    target = TARGETS.get(req.target.lower())
    if target is None:
        known = sorted(list(TARGETS) + list(AVR_TARGETS) + list(ARM_TARGETS))
        return {"success": False,
                "error": f"unknown target '{req.target}'; known: {', '.join(known)}"}

    stage_toolchain()

    work = os.path.join(tempfile.gettempdir(), f"build-{uuid.uuid4().hex}")
    os.makedirs(work, exist_ok=True)
    src = os.path.join(work, "main.c")
    with open(src, "w", encoding="utf-8") as handle:
        handle.write(req.code)

    sdcc_dir = sdcc_bin_dir()
    cmd = [os.path.join(sdcc_dir, "sdcc"), "-mmcs51"]
    # Keil source predates C99 habits and leans on the older grammar; forcing
    # --std-c99 on it costs compiles for nothing.
    if not keil_changes:
        cmd.append("--std-c99")
    cmd += keil2sdcc.shim_args()
    cmd += target["flags"]
    if req.symbols or req.disassemble:
        # --debug is needed for: .cdb (symbol table addresses) AND .rst
        # (source-interleaved listing). It changes the image slightly
        # (SDCC stops tail-merging returns), but the listing artifact
        # requires it.
        cmd.append("--debug")
    if req.fosc:
        cmd.append(f"-DFOSC_HZ={int(req.fosc)}UL")
    for name, value in req.defines.items():
        if not name.replace("_", "").isalnum():
            return {"success": False, "error": f"bad define name: {name!r}"}
        cmd.append(f"-D{name}" if value is None else f"-D{name}={value}")
    # Flags, plus the bare values that some of them take ("--code-size", "64").
    # What we must not allow is a path: sdcc would happily accept it as a
    # second input file and compile something out of the container.
    for index, opt in enumerate(req.options):
        opt = str(opt)
        if opt.startswith("-"):
            cmd.append(opt)
            continue
        if index == 0 or not VALUE_RE.fullmatch(opt):
            return {"success": False,
                    "error": f"options must be flags, or plain values following "
                             f"a flag; rejected {opt!r}"}
        cmd.append(opt)
    cmd += ["-o", work + os.sep, src]

    env = dict(os.environ, PATH=sdcc_dir + os.pathsep + os.environ.get("PATH", ""))

    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=COMPILE_TIMEOUT, cwd=work, env=env)
    except subprocess.TimeoutExpired:
        shutil.rmtree(work, ignore_errors=True)
        return {"success": False, "error": f"compile timed out after {COMPILE_TIMEOUT}s"}

    # The workspace path is an implementation detail; callers should see the
    # same "main.c:12: error" they would get compiling locally.
    log = ((result.stdout or "") + (result.stderr or "")).replace(work + os.sep, "")
    ihx = os.path.join(work, "main.ihx")

    if result.returncode != 0 or not os.path.exists(ihx):
        shutil.rmtree(work, ignore_errors=True)
        return {"success": False, "error": log.strip() or "compilation failed",
                "log": log}

    try:
        if req.format == "ihx":
            out, name = ihx, f"{stem}.ihx"
        elif req.format == "hex":
            out, name = os.path.join(work, "main.hex"), f"{stem}.hex"
            with open(out, "w", encoding="utf-8") as handle:
                packed = subprocess.run([os.path.join(sdcc_dir, "packihx"), ihx],
                                        capture_output=True, text=True, timeout=10)
                handle.write(packed.stdout)
        else:
            out, name = os.path.join(work, "main.bin"), f"{stem}.bin"
            subprocess.run([os.path.join(sdcc_dir, "makebin"), "-p", ihx, out],
                           capture_output=True, timeout=10)

        with open(out, "rb") as handle:
            blob = handle.read()

        symbols = None
        symbols_error = None
        if req.symbols:
            try:
                # objdump -t, not nm: the vendored bundle ships objdump
                # only, and avr_symtab.parse_nm reads both spellings.
                nm = subprocess.run(
                    [os.path.join(bin_dir, "avr-objdump"), "-t", elf],
                    capture_output=True, text=True, timeout=10, env=env)
                decoded = subprocess.run(
                    [os.path.join(bin_dir, "avr-objdump"),
                     "--dwarf=decodedline", elf],
                    capture_output=True, text=True, timeout=15, env=env)
                import avr_symtab
                try:
                    symbols = avr_symtab.build_symbol_table(
                        nm.stdout or "", decoded.stdout or "", req.code,
                        f_cpu=int(f_cpu or 16000000), mcu=spec["mcu"])
                except Exception as inner:
                    # Carry the evidence: the first lines of what the tools
                    # actually said beats guessing at a distance.
                    head = lambda t: " / ".join((t or "").splitlines()[3:14])[:600]
                    raise RuntimeError(
                        f"{inner} [objdump-t: {head(nm.stdout) or head(nm.stderr) or 'EMPTY'}]"
                        f" [decodedline: {head(decoded.stdout) or head(decoded.stderr) or 'EMPTY'}]")
            except Exception as exc:  # noqa: BLE001 - reported, never fatal
                symbols_error = str(exc)

        listing = None
        listing_artifact = None
        if req.disassemble:
            try:
                with open(ihx, encoding="utf-8") as handle:
                    listing = stc_disasm.disassemble_hex(handle.read())
            except (ValueError, OSError) as exc:
                listing = f"(disassembly failed: {exc})"
            # Source-interleaved listing from .rst (SDCC's relocatable listing)
            import listing as listing_mod
            rst_path = os.path.join(work, "main.rst")
            listing_artifact = listing_mod.from_sdcc_rst(rst_path)

        # SDCC's memory map. Useful enough to hand back that callers can warn
        # before an image silently outgrows the part.
        mem = ""
        mem_path = os.path.join(work, "main.mem")
        if os.path.exists(mem_path):
            with open(mem_path, encoding="utf-8", errors="replace") as handle:
                mem = handle.read()

        # The debug symbol table, when asked for. A failure here must NOT fail
        # the compile: the commonest reason is a single-WHEN program, which has
        # no scheduler and therefore no Level 1 position to describe. The image
        # is still perfectly good, so hand it back with the reason attached
        # rather than turning a working build into an error.
        symbols = None
        symbols_error = None
        if req.symbols:
            cdb_path = os.path.join(work, "main.cdb")
            try:
                with open(cdb_path, encoding="utf-8", errors="replace") as handle:
                    cdb_text = handle.read()
                symbols = stc_symtab.build_symbol_table(
                    cdb_text, req.code,
                    fosc=req.fosc or _fosc_from_source(req.code) or 11059200,
                    device=req.target.lower(),
                )
            except FileNotFoundError:
                symbols_error = ("sdcc wrote no main.cdb, so there are no addresses "
                                 "to report. That is a toolchain problem, not a "
                                 "problem with this source.")
            except stc_symtab.SymbolTableError as exc:
                symbols_error = str(exc)

        return {
            "success": True,
            "c": generated_c,          # None unless the source was translated
            "translated": keil_changes or None,
            "unresolved": keil_unresolved or None,
            "warnings": keil_warnings or None,
            "disassembly": listing,     # None unless disassemble was requested
            "listing": listing_artifact,
            "symbols": symbols,         # None unless symbols was requested
            "symbols_error": symbols_error,
            "base64": base64.b64encode(blob).decode("ascii"),
            "filename": name,
            "bytes": len(blob),
            "log": log,
            "memory": mem,
            "symbols": symbols,
            "symbols_error": symbols_error,
        }
    finally:
        shutil.rmtree(work, ignore_errors=True)


@app.post("/compile")
async def compile_source(req: CompileReq):
    """Compile and return the image base64-encoded inside JSON."""
    return build(req)


class DisassembleReq(BaseModel):
    """An Intel HEX image, as text or base64."""
    hex: str | None = None
    base64: str | None = None


@app.post("/disassemble")
async def disassemble_image(req: DisassembleReq):
    """Intel HEX in, 8051 assembly out.

    Handy for checking what actually landed in the image rather than trusting
    the compiler -- and for diffing two builds that should be identical.
    """
    refusal = too_large(req.hex, req.base64)
    if refusal:
        return refusal
    text = req.hex
    if text is None and req.base64:
        try:
            text = base64.b64decode(req.base64).decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001 - reported to the caller
            return {"success": False, "error": f"bad base64: {exc}"}
    if not text:
        return {"success": False, "error": "provide 'hex' or 'base64'"}
    try:
        memory = stc_disasm.parse_hex(text)
        entries = stc_disasm.disassemble(memory)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}
    return {
        "success": True,
        "disassembly": stc_disasm.format_listing(entries),
        "instructions": len(entries),
        "bytes": len(memory),
        "start": min(memory) if memory else 0,
        "end": max(memory) if memory else 0,
    }


class AssembleReq(BaseModel):
    """Raw assembly source for one of the supported architectures."""
    asm: str
    # Target device — determines which assembler chain to use.
    # 8051: sdas8051+sdld; 6502/eater6502: ca65+ld65; atmega*: avr-gcc;
    # nrf52833: arm-none-eabi-gcc
    target: str = "stc12c5a60s2"


ASSEMBLE_TARGETS = {
    # 8051 family → sdas8051
    "stc12c5a60s2": "8051", "stc12c5a16s2": "8051", "stc89c52rc": "8051",
    "stc15f2k60s2": "8051", "stc89c52": "8051", "stc15w408as": "8051",
    "mcs51": "8051",
    # 6502 → ca65+ld65
    "eater6502": "6502", "6502": "6502", "w65c02": "6502",
    # AVR → avr-gcc
    "atmega328p": "avr", "atmega168p": "avr", "atmega2560": "avr",
    "attiny85": "avr",
    # ARM → arm-none-eabi-gcc
    "nrf52833": "arm",
}

ARM_MCU_FOR_TARGET = {
    "nrf52833": "cortex-m4",
}

ARM_LD_FOR_TARGET = {
    "nrf52833": os.path.join(BASE_DIR, "nrf52833.ld"),
}

AVR_MCU_FOR_TARGET = {
    "atmega328p": "atmega328p", "atmega168p": "atmega168p",
    "atmega2560": "atmega2560", "attiny85": "attiny85",
}


@app.post("/assemble")
async def assemble_source(req: AssembleReq):
    """Assemble raw assembly source and return the image.

    Four toolchains: sdas8051+sdld (8051), ca65+ld65 (6502),
    avr-gcc (AVR), arm-none-eabi-gcc (ARM/nRF52833).
    """
    if len(req.asm.encode("utf-8")) > MAX_SOURCE_BYTES:
        return {"success": False, "error": "source too large"}

    import assemble as asm_mod
    target = req.target.lower()
    chain = ASSEMBLE_TARGETS.get(target)

    if chain == "8051":
        stage_toolchain()
        return asm_mod.assemble_8051(req.asm, sdcc_bin_dir())
    elif chain == "6502":
        cc65_bin = stage_cc65()
        if cc65_bin is None:
            return {"success": False,
                    "error": "no cc65 toolchain available"}
        cfg = os.path.join(BASE_DIR, "eater.cfg")
        return asm_mod.assemble_6502(req.asm, cfg, bin_dir=cc65_bin)
    elif chain == "avr":
        avr_bin = stage_avr()
        if avr_bin is None:
            return {"success": False, "error": "no AVR toolchain available"}
        deps = os.path.join(os.path.dirname(avr_bin), "lib-deps")
        env = dict(os.environ)
        if os.path.isdir(deps):
            env["LD_LIBRARY_PATH"] = deps + os.pathsep + env.get("LD_LIBRARY_PATH", "")
        mcu = AVR_MCU_FOR_TARGET.get(target, "atmega328p")
        return asm_mod.assemble_avr(req.asm, mcu, avr_bin, env)
    elif chain == "arm":
        arm_bin = stage_arm()
        if arm_bin is None:
            return {"success": False,
                    "error": "no ARM toolchain available"}
        deps = os.path.join(os.path.dirname(arm_bin), "lib-deps")
        env = dict(os.environ)
        if os.path.isdir(deps):
            env["LD_LIBRARY_PATH"] = deps + os.pathsep + env.get("LD_LIBRARY_PATH", "")
        mcu = ARM_MCU_FOR_TARGET.get(target, "cortex-m4")
        ld = ARM_LD_FOR_TARGET.get(target)
        if not ld or not os.path.exists(ld):
            return {"success": False,
                    "error": f"no linker script for target {target!r}"}
        return asm_mod.assemble_arm(req.asm, mcu, arm_bin, env, ld)
    else:
        known = sorted(ASSEMBLE_TARGETS.keys())
        return {"success": False,
                "error": f"unknown assemble target {req.target!r}; "
                         f"known: {', '.join(known)}"}


@app.post("/translate")
async def translate_keil(req: CompileReq):
    """Keil C51 in, SDCC-dialect C out. No compiler involved."""
    refusal = too_large(req.code)
    if refusal:
        return refusal
    stage_toolchain()
    result = keil2sdcc.translate(req.code)
    return {"success": True, "c": result.text, "translated": result.changes,
            "unresolved": result.unresolved, "warnings": result.warnings}


class ProjectReq(BaseModel):
    """A whole Keil project: {relative path: file content}.

    Paths are used for the header map and the -I layout, so keep the project's
    own directory structure. Non-C files are passed through untouched.
    """
    files: dict[str, str]
    # Also compile every .c and link the project into one image.
    link: bool = False
    target: str = "stc12c5a60s2"
    fosc: int | None = None      # Keil projects usually bake timing themselves
    defines: dict[str, str | None] = {}
    options: list[str] = []
    format: str = "hex"
    disassemble: bool = False


@app.post("/translate-project")
async def translate_project_endpoint(req: ProjectReq):
    """Keil project in, SDCC project out — optionally linked into a .hex.

    This exists because two of the Keil/SDCC differences are project-wide, not
    per-file: include resolution (uVision is case-insensitive and carries a
    project include path) and interrupt vectors (SDCC only emits one when the
    handler is visible in main()'s translation unit; Keil links them
    separately, so a per-file translation compiles cleanly and the interrupt
    never fires).
    """
    total = sum(len(text.encode("utf-8", "replace")) for text in req.files.values())
    if total > 4 * MAX_SOURCE_BYTES:
        return {"success": False, "error": "project too large"}
    for path in req.files:
        clean = path.replace("\\", "/")
        if clean.startswith("/") or ".." in clean.split("/") or not clean:
            return {"success": False, "error": f"bad path: {path!r}"}
    # The file NAMES were already checked; their CONTENTS can name a path too,
    # and this endpoint compiles them.
    for path, text in req.files.items():
        refusal = reject_escaping_includes(text)
        if refusal:
            refusal["error"] = f"{path}: {refusal['error']}"
            return refusal
    refusal = reject_path_options(req.options)
    if refusal:
        return refusal

    stage_toolchain()
    result = keil2sdcc.translate_project(req.files)
    response = {
        "success": True,
        "files": result.files,
        "translated": result.changes or None,
        "unresolved": result.unresolved or None,
        "warnings": result.warnings or None,
        "isr_injected": result.isr_injected or None,
        "sources": result.sources,          # from the .uvproj, when present
        "model_flags": result.model_flags or None,
    }
    if not req.link:
        return response

    target = TARGETS.get(req.target.lower())
    if target is None:
        return {"success": False,
                "error": f"unknown target '{req.target}'; "
                         f"known: {', '.join(sorted(TARGETS))}"}
    if req.format not in ("ihx", "hex", "bin"):
        return {"success": False, "error": "format must be ihx, hex or bin"}

    work = os.path.join(tempfile.gettempdir(), f"project-{uuid.uuid4().hex}")
    try:
        sources = []
        include_dirs = set()
        for path, text in result.files.items():
            clean = path.replace("\\", "/")
            dest = os.path.join(work, *clean.split("/"))
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "w", encoding="utf-8") as handle:
                handle.write(text)
            include_dirs.add(os.path.dirname(dest))
            in_build = (result.sources is None
                        or path in result.sources)
            if clean.lower().endswith(".c") and in_build:
                # main()'s unit first: sdcc wants the module holding main at
                # the front of the link line.
                is_main = bool(keil2sdcc._MAIN_DEF.search(text))
                sources.append((not is_main, dest))
        if not sources:
            return {"success": False, "error": "no .c files in project"}

        sdcc_dir = sdcc_bin_dir()
        base = [os.path.join(sdcc_dir, "sdcc"), "-mmcs51"]
        base += result.model_flags       # Keil memory model, via the .uvproj
        base += keil2sdcc.shim_args()
        for directory in sorted(include_dirs):
            base += ["-I", directory]
        if req.fosc:
            base.append(f"-DFOSC_HZ={int(req.fosc)}UL")
        for name, value in req.defines.items():
            if not name.replace("_", "").isalnum():
                return {"success": False, "error": f"bad define name: {name!r}"}
            base.append(f"-D{name}" if value is None else f"-D{name}={value}")
        for index, opt in enumerate(req.options):
            opt = str(opt)
            if opt.startswith("-"):
                base.append(opt)
                continue
            if index == 0 or not VALUE_RE.fullmatch(opt):
                return {"success": False,
                        "error": f"options must be flags, or plain values "
                                 f"following a flag; rejected {opt!r}"}
            base.append(opt)

        env = dict(os.environ,
                   PATH=sdcc_dir + os.pathsep + os.environ.get("PATH", ""))
        log = ""
        rels = []
        for _, source in sorted(sources):
            rel = source[:-2] + ".rel"
            run = subprocess.run(
                base + ["-c", "-o", rel, source], capture_output=True,
                text=True, timeout=COMPILE_TIMEOUT, cwd=work, env=env)
            log += ((run.stdout or "") + (run.stderr or "")).replace(
                work + os.sep, "")
            if run.returncode != 0:
                return {**response, "success": False, "log": log,
                        "error": f"compile failed: "
                                 f"{os.path.relpath(source, work)}"}
            rels.append(rel)

        ihx = os.path.join(work, "project.ihx")
        run = subprocess.run(
            base + target["flags"] + ["-o", ihx] + rels, capture_output=True,
            text=True, timeout=COMPILE_TIMEOUT, cwd=work, env=env)
        log += ((run.stdout or "") + (run.stderr or "")).replace(work + os.sep, "")
        if run.returncode != 0 or not os.path.exists(ihx):
            return {**response, "success": False, "log": log,
                    "error": "link failed"}

        if req.format == "ihx":
            out, name = ihx, "project.ihx"
        elif req.format == "hex":
            out, name = os.path.join(work, "project.hex"), "project.hex"
            packed = subprocess.run([os.path.join(sdcc_dir, "packihx"), ihx],
                                    capture_output=True, text=True, timeout=10)
            with open(out, "w", encoding="utf-8") as handle:
                handle.write(packed.stdout)
        else:
            out, name = os.path.join(work, "project.bin"), "project.bin"
            subprocess.run([os.path.join(sdcc_dir, "makebin"), "-p", ihx, out],
                           capture_output=True, timeout=10)
        with open(out, "rb") as handle:
            blob = handle.read()

        symbols = None
        symbols_error = None
        if req.symbols:
            try:
                # objdump -t, not nm: the vendored bundle ships objdump
                # only, and avr_symtab.parse_nm reads both spellings.
                nm = subprocess.run(
                    [os.path.join(bin_dir, "avr-objdump"), "-t", elf],
                    capture_output=True, text=True, timeout=10, env=env)
                decoded = subprocess.run(
                    [os.path.join(bin_dir, "avr-objdump"),
                     "--dwarf=decodedline", elf],
                    capture_output=True, text=True, timeout=15, env=env)
                import avr_symtab
                try:
                    symbols = avr_symtab.build_symbol_table(
                        nm.stdout or "", decoded.stdout or "", req.code,
                        f_cpu=int(f_cpu or 16000000), mcu=spec["mcu"])
                except Exception as inner:
                    # Carry the evidence: the first lines of what the tools
                    # actually said beats guessing at a distance.
                    head = lambda t: " / ".join((t or "").splitlines()[3:14])[:600]
                    raise RuntimeError(
                        f"{inner} [objdump-t: {head(nm.stdout) or head(nm.stderr) or 'EMPTY'}]"
                        f" [decodedline: {head(decoded.stdout) or head(decoded.stderr) or 'EMPTY'}]")
            except Exception as exc:  # noqa: BLE001 - reported, never fatal
                symbols_error = str(exc)

        listing = None
        if req.disassemble:
            try:
                with open(ihx, encoding="utf-8") as handle:
                    listing = stc_disasm.disassemble_hex(handle.read())
            except (ValueError, OSError) as exc:
                listing = f"(disassembly failed: {exc})"
        mem = ""
        mem_path = os.path.join(work, "project.mem")
        if os.path.exists(mem_path):
            with open(mem_path, encoding="utf-8", errors="replace") as handle:
                mem = handle.read()

        response.update(base64=base64.b64encode(blob).decode("ascii"),
                        filename=name, bytes=len(blob), log=log, memory=mem,
                        disassembly=listing)
        return response
    except subprocess.TimeoutExpired:
        return {**response, "success": False,
                "error": f"compile timed out after {COMPILE_TIMEOUT}s"}
    finally:
        shutil.rmtree(work, ignore_errors=True)


@app.post("/decompile")
async def decompile_pseudocode(req: CompileReq):
    """Pseudocode in, canonical pseudocode out.

    The front end's AST is the source of truth, so this is `parse` followed by
    `emit_pseudocode`: normalised layout, comments dropped, and a fixed point
    -- feeding the result back in returns it unchanged.
    """
    refusal = too_large(req.code)
    if refusal:
        return refusal
    try:
        return {"success": True, "pseudocode": stc_pseudocode.decompile(req.code)}
    except stc_pseudocode.PseudocodeError as exc:
        return {"success": False, "error": str(exc), "line": exc.line}


@app.post("/transpile")
async def transpile_only(req: CompileReq):
    """Pseudocode in, C out. No compiler involved -- useful for seeing exactly
    what the front end produced before handing it to SDCC."""
    refusal = too_large(req.code)
    if refusal:
        return refusal
    try:
        code, program = stc_pseudocode.transpile(req.code)
    except stc_pseudocode.PseudocodeError as exc:
        return {"success": False, "error": str(exc), "line": exc.line}
    return {
        "success": True,
        "c": code,
        # Not always C any more: a micro:bit transpiles to MicroPython. The
        # field keeps its name so existing callers keep working, and this says
        # what is actually in it.
        "language": stc_pseudocode.source_language(program),
        "isp": program.target.isp_protocol,
        "filename": f"{program.name or 'main'}.{program.target.source_extension}",
        "part": program.part,
        "clock": program.clock,
        # `where` is the target-neutral location token ("P1.0", "D13", "A0").
        # `sfr` is reported only by targets that have one, so an 8051 consumer
        # keeps the field it already reads and a non-8051 device does not have
        # to invent a register name to fill it.
        "pins": {name: {"where": pin.where, "direction": pin.direction,
                        "active_low": pin.active_low,
                        **({"sfr": pin.sfr} if hasattr(pin, "sfr") else {})}
                 for name, pin in program.pins.items()},
        "variables": program.variables,
    }


@app.post("/download")
async def download(req: CompileReq):
    """Same as /compile, but returns the raw file with a filename attached.

    Saves callers from base64-decoding by hand:
        curl -X POST .../download -H 'Content-Type: application/json' \
             -d @req.json -OJ
    """
    result = build(req)
    if not result["success"]:
        # A target that transpiles but cannot be built here is not a failure
        # to report as one: the generated source IS the deliverable, and a
        # micro:bit .py or an Arduino .ino is exactly what the caller wanted.
        # Anything else -- a syntax error, an unknown target -- is still 400.
        if result.get("c") and result.get("toolchain"):
            name = result.get("filename", "main.txt")
            return Response(
                content=result["c"].encode("utf-8"),
                media_type="text/x-python" if name.endswith(".py") else "text/plain",
                headers={
                    "Content-Disposition": f'attachment; filename="{name}"',
                    "X-Source-Only": result["toolchain"],
                    "Access-Control-Expose-Headers":
                        "Content-Disposition, X-Source-Only",
                },
            )
        return PlainTextResponse(result["error"], status_code=400)
    return Response(
        content=base64.b64decode(result["base64"]),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{result["filename"]}"',
            "X-Image-Bytes": str(result["bytes"]),
            # So a browser fetch() can read the size without a preflight dance.
            "Access-Control-Expose-Headers": "Content-Disposition, X-Image-Bytes",
        },
    )


# ----------------------------------------------------------------- about
# Mirrors the About screen of the sibling apps (CrisperWeaver, CrispSorter):
# service provider, contact, privacy, disclaimer, licence, then the list of
# third-party components. Bilingual, like the rest of this project's docs.
#
# The provider block can be overridden per-deployment so a fork does not
# publish someone else's details:  IMPRINT_NAME / _ADDRESS / _EMAIL / _PHONE.
PROVIDER_NAME = os.environ.get("IMPRINT_NAME", "Christian Ströbele")
PROVIDER_ADDRESS = os.environ.get(
    "IMPRINT_ADDRESS", "Nikolausstr. 5\n70190 Stuttgart\nDeutschland / Germany")
PROVIDER_EMAIL = os.environ.get("IMPRINT_EMAIL", "postmaster@crispstro.be")
PROVIDER_PHONE = os.environ.get("IMPRINT_PHONE", "+49 176 6421 8601")

SDCC_VERSION_LABEL = "4.0.0+dfsg-2"

LICENCE_ROWS = [
    ("This service &mdash; API, browser UI, build scripts",
     "MIT", "https://github.com/CrispStrobe/stc-compiler/blob/main/LICENSE"),
    ("<a href='https://sdcc.sourceforge.net/' target=_blank rel=noopener>SDCC</a> 4.0.0 &mdash; "
     "<code>sdcc</code>, <code>sdcpp</code>, <code>sdas8051</code>, <code>sdld</code>, "
     "<code>packihx</code>, <code>makebin</code>",
     "GPL-2.0-or-later", "https://www.gnu.org/licenses/old-licenses/gpl-2.0.html"),
    ("SDCC runtime headers and libraries, including <code>mcs51/stc12.h</code>",
     "GPL-2.0-or-later<br>with linking exception",
     "https://www.gnu.org/licenses/old-licenses/gpl-2.0.html"),
    ("<a href='https://github.com/fastapi/fastapi' target=_blank rel=noopener>FastAPI</a>, "
     "<a href='https://github.com/pydantic/pydantic' target=_blank rel=noopener>Pydantic</a>",
     "MIT", "https://opensource.org/license/mit"),
    ("<a href='https://github.com/encode/starlette' target=_blank rel=noopener>Starlette</a>, "
     "<a href='https://github.com/encode/uvicorn' target=_blank rel=noopener>Uvicorn</a>",
     "BSD-3-Clause", "https://opensource.org/license/bsd-3-clause"),
]

LINKING_EXCEPTION = (
    "As a special exception, if you link this library with other files, some of which "
    "are compiled with SDCC, to produce an executable, this library does not by itself "
    "cause the resulting executable to be covered by the GNU General Public License."
)

TEXT = {
    "en": {
        "tagline": "Compiles C to Intel HEX for the STC12C5A60S2 and other 8051 parts. "
                   "The compile side of a Scratch-blocks-to-8051 back-end for BrickWright.",
        "provider": "Service Provider",
        "contact": "Contact",
        "email": "Email",
        "phone": "Phone",
        "project": "Project",
        "privacy": "Privacy",
        "privacy_text":
            "Source you submit is compiled in an ephemeral container and the workspace is "
            "deleted as soon as the response is sent. Nothing is stored, put in a database, "
            "or used for anything else. No cookies, no analytics, no tracking. The hosting "
            "provider may keep ordinary request metadata such as IP address and timestamps "
            "in its own logs.",
        "disclaimer": "Disclaimer",
        "disclaimer_text":
            "This software is provided \u201cas is\u201d, without warranty of any kind, express "
            "or implied, including but not limited to the warranties of merchantability, "
            "fitness for a particular purpose and noninfringement. In no event shall the "
            "authors be liable for any claim, damages or other liability arising from, out "
            "of or in connection with the software or its use.",
        "hardware": "You are responsible for what you flash and for the hardware you flash it to:",
        "hw_1": "The <strong>STC12C5A60S2 is a 5&nbsp;V part</strong> (3.5&ndash;5.5&nbsp;V). "
                "The <strong>STC12LE5A60S2 is not</strong> (2.1&ndash;3.6&nbsp;V) and 5&nbsp;V "
                "will destroy it. Check the marking on your chip.",
        "hw_2": "A wrong <code>FOSC</code> produces working code that keeps the wrong time. "
                "Verify it against a clock before trusting anything time-critical.",
        "hw_3": "Nothing here is validated for safety-critical, medical or industrial use.",
        "licence": "Licence",
        "licence_text":
            "This service is free software under the MIT licence. It bundles the SDCC "
            "compiler, which is GPL-2.0-or-later.",
        "output_yours": "What you compile here is yours.",
        "output_text":
            "SDCC is GPL, but its runtime libraries and headers &mdash; the ones your program "
            "actually links against &mdash; carry an explicit exception:",
        "source_offer":
            "The SDCC binaries are unmodified, from Debian bullseye. Corresponding source: ",
        "components": "Third-party components",
        "affil": "Not affiliated",
        "affil_text":
            "Not affiliated with, endorsed by or connected to STC MCU Limited (Hongjing "
            "Technology), the SDCC project, or the LEGO Group. All trademarks belong to "
            "their respective owners.",
        "close": "Close",
    },
    "de": {
        "tagline": "Übersetzt C nach Intel HEX für den STC12C5A60S2 und andere 8051-Typen. "
                   "Die Compiler-Seite eines Scratch-Blöcke-nach-8051-Backends für BrickWright.",
        "provider": "Anbieter",
        "contact": "Kontakt",
        "email": "E-Mail",
        "phone": "Telefon",
        "project": "Projekt",
        "privacy": "Datenschutz",
        "privacy_text":
            "Eingesendeter Quelltext wird in einem flüchtigen Container übersetzt; das "
            "Arbeitsverzeichnis wird gelöscht, sobald die Antwort verschickt ist. Es wird "
            "nichts gespeichert, in eine Datenbank gelegt oder anderweitig verwendet. Keine "
            "Cookies, keine Analyse, kein Tracking. Der Hosting-Anbieter kann übliche "
            "Verbindungsdaten wie IP-Adresse und Zeitstempel in seinen eigenen Protokollen "
            "vorhalten.",
        "disclaimer": "Haftungsausschluss",
        "disclaimer_text":
            "Diese Software wird \u201ewie besehen\u201c zur Verfügung gestellt, ohne jegliche "
            "ausdrückliche oder stillschweigende Gewährleistung, insbesondere der "
            "Marktgängigkeit, Eignung für einen bestimmten Zweck oder der Nichtverletzung "
            "von Rechten Dritter. In keinem Fall haften die Autor:innen für Ansprüche, "
            "Schäden oder sonstige Haftungen, die sich aus oder im Zusammenhang mit der "
            "Software oder deren Nutzung ergeben.",
        "hardware": "Sie sind selbst dafür verantwortlich, was Sie flashen und auf welche Hardware:",
        "hw_1": "Der <strong>STC12C5A60S2 ist ein 5-V-Typ</strong> (3,5&ndash;5,5&nbsp;V). Der "
                "<strong>STC12LE5A60S2 ist es nicht</strong> (2,1&ndash;3,6&nbsp;V), 5&nbsp;V "
                "zerstören ihn. Prüfen Sie den Aufdruck auf dem Chip.",
        "hw_2": "Ein falsches <code>FOSC</code> ergibt lauffähigen Code mit falschen Zeiten. "
                "Vor zeitkritischem Einsatz gegen eine Uhr prüfen.",
        "hw_3": "Nichts hiervon ist für sicherheitskritische, medizinische oder industrielle "
                "Anwendungen geprüft.",
        "licence": "Lizenz",
        "licence_text":
            "Dieser Dienst ist freie Software unter der MIT-Lizenz. Er bündelt den "
            "SDCC-Compiler, der unter GPL-2.0-or-later steht.",
        "output_yours": "Was Sie hier übersetzen, gehört Ihnen.",
        "output_text":
            "SDCC steht unter der GPL, seine Laufzeitbibliotheken und Header &mdash; also das, "
            "wogegen Ihr Programm tatsächlich gelinkt wird &mdash; tragen jedoch eine "
            "ausdrückliche Ausnahme:",
        "source_offer":
            "Die SDCC-Binaries stammen unverändert aus Debian bullseye. Zugehöriger Quelltext: ",
        "components": "Fremdkomponenten",
        "affil": "Keine Verbindung",
        "affil_text":
            "Keine Verbindung zu, Billigung durch oder Zugehörigkeit zu STC MCU Limited "
            "(Hongjing Technology), dem SDCC-Projekt oder der LEGO-Gruppe. Alle Marken "
            "gehören ihren jeweiligen Inhabern.",
        "close": "Schließen",
    },
}

REPO_LINKS = (
    '<a href="https://github.com/CrispStrobe" target=_blank rel=noopener>CrispStrobe</a> &middot; '
    '<a href="https://github.com/CrispStrobe/stc-compiler" target=_blank rel=noopener>stc-compiler</a> &middot; '
    '<a href="https://github.com/CrispStrobe/stc12c5a60s2-lab" target=_blank rel=noopener>stc12c5a60s2-lab</a> &middot; '
    '<a href="https://github.com/CrispStrobe/brickwright" target=_blank rel=noopener>BrickWright</a> &middot; '
    '<a href="https://github.com/CrispStrobe/legacy-lego-compiler" target=_blank rel=noopener>legacy-lego-compiler</a>'
)


def about_pane(lang: str) -> str:
    """One language's worth of About content."""
    s = TEXT[lang]
    address = "<br>".join(html.escape(line) for line in PROVIDER_ADDRESS.splitlines() if line.strip())
    mail = html.escape(PROVIDER_EMAIL)
    tel = html.escape(PROVIDER_PHONE)
    tel_href = "tel:" + re.sub(r"[^+0-9]", "", PROVIDER_PHONE)

    rows = "".join(
        f"<tr><td>{what}</td><td><a href='{url}' target=_blank rel=noopener>{lic}</a></td></tr>"
        for what, lic, url in LICENCE_ROWS
    )

    return f"""<div class=pane data-lang="{lang}">
  <p class=dim>{s['tagline']}</p>

  <h3>{s['provider']}</h3>
  <address><strong>{html.escape(PROVIDER_NAME)}</strong><br>{address}</address>

  <h3>{s['contact']}</h3>
  <p>{s['email']}: <a href="mailto:{mail}">{mail}</a><br>
     {s['phone']}: <a href="{tel_href}">{tel}</a></p>

  <h3>{s['project']}</h3>
  <p>{REPO_LINKS}</p>

  <h3>{s['privacy']}</h3>
  <p>{s['privacy_text']}</p>

  <h3>{s['disclaimer']}</h3>
  <p>{s['disclaimer_text']}</p>
  <p>{s['hardware']}</p>
  <ul><li>{s['hw_1']}</li><li>{s['hw_2']}</li><li>{s['hw_3']}</li></ul>

  <h3>{s['licence']}</h3>
  <p>{s['licence_text']}</p>
  <p><strong>{s['output_yours']}</strong> {s['output_text']}</p>
  <blockquote>{LINKING_EXCEPTION}</blockquote>
  <p>{s['source_offer']}<code>apt-get source sdcc={SDCC_VERSION_LABEL}</code> &middot;
     <a href="https://github.com/CrispStrobe/stc-compiler/blob/main/NOTICE.md"
        target=_blank rel=noopener>NOTICE.md</a></p>

  <h3>{s['components']}</h3>
  <table>{rows}</table>

  <h3>{s['affil']}</h3>
  <p>{s['affil_text']}</p>
</div>"""


def about_html() -> str:
    return about_pane("en") + about_pane("de")


@app.get("/health")
async def health():
    stage_toolchain()
    try:
        version = subprocess.run([os.path.join(STAGE_BIN, "sdcc"), "--version"],
                                 capture_output=True, text=True, timeout=10).stdout
    except Exception as exc:  # noqa: BLE001 - surfaced to the caller as text
        return {"ok": False, "error": str(exc)}
    # The AVR side is reported separately and never fails the health check:
    # a deployment without the avr/ bundle is a perfectly good 8051 compiler,
    # and saying so plainly beats a red light nobody can act on.
    avr_bin = stage_avr()
    avr_version = ""
    if avr_bin:
        try:
            deps = os.path.join(os.path.dirname(avr_bin), "lib-deps")
            health_env = dict(os.environ)
            if os.path.isdir(deps):
                health_env["LD_LIBRARY_PATH"] = (
                    deps + os.pathsep + health_env.get("LD_LIBRARY_PATH", ""))
            # -print-prog-name=cc1 resolves it; running --version on the DRIVER
            # would pass even when cc1 cannot load its libraries, which is the
            # exact way this looked healthy while being broken.
            probe = subprocess.run(
                [os.path.join(avr_bin, "avr-gcc"), "-print-prog-name=cc1"],
                capture_output=True, text=True, timeout=10, env=health_env)
            cc1 = (probe.stdout or "").strip()
            if cc1 and os.path.exists(cc1):
                started = subprocess.run([cc1, "--version"], capture_output=True,
                                         text=True, timeout=10, env=health_env)
                if started.returncode != 0:
                    avr_version = f"BROKEN: {(started.stderr or '').strip()[:120]}"
                else:
                    avr_version = subprocess.run(
                        [os.path.join(avr_bin, "avr-gcc"), "--version"],
                        capture_output=True, text=True, timeout=10,
                        env=health_env).stdout
            else:
                avr_version = subprocess.run(
                    [os.path.join(avr_bin, "avr-gcc"), "--version"],
                    capture_output=True, text=True, timeout=10,
                    env=health_env).stdout
        except Exception:  # noqa: BLE001 - absence is reported, not raised
            avr_version = ""

    # ARM side — same pattern as AVR.
    arm_bin = stage_arm()
    arm_version = ""
    if arm_bin:
        try:
            deps = os.path.join(os.path.dirname(arm_bin), "lib-deps")
            health_env = dict(os.environ)
            if os.path.isdir(deps):
                health_env["LD_LIBRARY_PATH"] = (
                    deps + os.pathsep + health_env.get("LD_LIBRARY_PATH", ""))
            probe = subprocess.run(
                [os.path.join(arm_bin, "arm-none-eabi-gcc"),
                 "-print-prog-name=cc1"],
                capture_output=True, text=True, timeout=10, env=health_env)
            cc1 = (probe.stdout or "").strip()
            if cc1 and os.path.exists(cc1):
                started = subprocess.run([cc1, "--version"], capture_output=True,
                                         text=True, timeout=10, env=health_env)
                if started.returncode != 0:
                    arm_version = f"BROKEN: {(started.stderr or '').strip()[:120]}"
                else:
                    arm_version = subprocess.run(
                        [os.path.join(arm_bin, "arm-none-eabi-gcc"), "--version"],
                        capture_output=True, text=True, timeout=10,
                        env=health_env).stdout
            else:
                arm_version = subprocess.run(
                    [os.path.join(arm_bin, "arm-none-eabi-gcc"), "--version"],
                    capture_output=True, text=True, timeout=10,
                    env=health_env).stdout
        except Exception:  # noqa: BLE001 - absence is reported, not raised
            arm_version = ""

    # cc65 side — simpler than gcc, just check ca65 --version.
    cc65_bin = stage_cc65()
    cc65_version = ""
    if cc65_bin:
        try:
            cc65_version = subprocess.run(
                [os.path.join(cc65_bin, "ca65"), "--version"],
                capture_output=True, text=True, timeout=10).stderr or ""
        except Exception:  # noqa: BLE001
            cc65_version = ""

    return {
        "ok": True,
        "version": os.environ.get("VERCEL_GIT_COMMIT_SHA", "")[:7] or "unknown",
        "sdcc": version.strip().splitlines()[0] if version else "",
        "avr_gcc": avr_version.strip().splitlines()[0] if avr_version else None,
        "arm_gcc": arm_version.strip().splitlines()[0] if arm_version else None,
        "cc65": cc65_version.strip().splitlines()[0] if cc65_version else None,
        "targets": {name: cfg["description"] for name, cfg in TARGETS.items()},
        "avr_targets": ({name: cfg["description"] for name, cfg in AVR_TARGETS.items()}
                        if avr_bin else {}),
        "arm_targets": ({name: cfg["description"] for name, cfg in ARM_TARGETS.items()}
                        if arm_bin else {}),
        "devices": sorted(stc_pseudocode.TARGETS),
        "assemble_targets": sorted(ASSEMBLE_TARGETS.keys()),
    }


EXAMPLE = """#include <stc12.h>

/* Blink two LEDs on P1.0 (pin 1) and P1.1 (pin 2), wired active-low:
   +5V --[1k]--|>|-- pin.  Writing 0 lights the LED. */

#define T0_RELOAD (65536UL - (FOSC_HZ / 12UL / 1000UL))

static void delay_ms(unsigned int ms)
{
    while (ms--) {
        TL0 = (unsigned char)(T0_RELOAD & 0xFF);
        TH0 = (unsigned char)(T0_RELOAD >> 8);
        TF0 = 0;
        TR0 = 1;
        while (!TF0) ;
        TR0 = 0;
        TF0 = 0;
    }
}

void main(void)
{
    P1M1 &= ~0x03;              /* P1.0, P1.1 -> push-pull */
    P1M0 |=  0x03;
    AUXR &= ~0x80;              /* Timer 0 at FOSC/12 */
    TMOD  = (TMOD & 0xF0) | 0x01;

    for (;;) {
        P1_0 = 0; P1_1 = 1; delay_ms(500);
        P1_0 = 1; P1_1 = 0; delay_ms(500);
    }
}
"""


# The page is a plain string with a __EXAMPLE__ placeholder rather than an
# f-string: JavaScript is mostly braces, and doubling every one of them to
# survive f-string interpolation makes it unreadable and easy to break.
PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>stc-compiler</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin:0; font:14px/1.5 -apple-system,BlinkMacSystemFont,system-ui,sans-serif;
         background:#14161a; color:#e6e6e6; display:flex; flex-direction:column; height:100vh; }
  header { padding:10px 18px; background:#1c1f26; border-bottom:1px solid #2a2e37;
           display:flex; gap:14px; align-items:center; flex-wrap:wrap; }
  h1 { font-size:15px; margin:0 auto 0 0; font-weight:600; white-space:nowrap; }
  h1 small { font-weight:400; color:#8a91a0; margin-left:8px; }
  main { flex:1; display:flex; min-height:0; }
  textarea { flex:1; border:0; padding:16px; resize:none; background:#14161a; color:#e6e6e6;
             font:13px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace; outline:none; }
  aside { width:44%; min-width:300px; border-left:1px solid #2a2e37;
          display:flex; flex-direction:column; background:#101216; }
  #tabs { display:flex; gap:2px; padding:8px 10px 0; border-bottom:1px solid #2a2e37; }
  #tabs button { background:none; border:0; border-bottom:2px solid transparent;
                 color:#8a91a0; padding:6px 12px; font-size:12px; cursor:pointer; }
  #tabs button.on { color:#e6e6e6; border-bottom-color:#3b82f6; }
  #panes { flex:1; overflow:auto; padding:14px 16px; }
  pre { margin:0; white-space:pre-wrap; word-break:break-all;
        font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace; color:#b9c0cc; }
  button.primary { background:#3b82f6; color:#fff; border:0; padding:7px 16px;
                   border-radius:6px; font-size:13px; font-weight:500; cursor:pointer; }
  button.ghost { background:#22262f; color:#e6e6e6; border:1px solid #333945;
                 padding:7px 14px; border-radius:6px; font-size:13px; cursor:pointer; }
  button:disabled { opacity:.4; cursor:default; }
  select, input { background:#22262f; color:#e6e6e6; border:1px solid #333945;
                  border-radius:6px; padding:6px 8px; font-size:13px; }
  label { color:#8a91a0; font-size:12px; display:flex; align-items:center; gap:5px; }
  .ok { color:#4ade80; } .err { color:#f87171; } .dim { color:#8a91a0; }
  a { color:#7aa2f7; }
  button.link { background:none; border:0; color:#8a91a0; font-size:13px;
                cursor:pointer; text-decoration:underline; padding:4px; }
  button.link:hover { color:#e6e6e6; }
  dialog { background:#181b21; color:#e6e6e6; border:1px solid #2a2e37; border-radius:10px;
           max-width:640px; width:calc(100% - 32px); padding:0; }
  dialog::backdrop { background:rgba(0,0,0,.6); }
  .sheet { padding:22px 26px 26px; max-height:78vh; overflow:auto; }
  dialog h2 { margin:0 0 4px; font-size:17px; }
  dialog h3 { margin:22px 0 6px; font-size:13px; text-transform:uppercase;
              letter-spacing:.06em; color:#8a91a0; }
  dialog p, dialog li { font-size:13px; line-height:1.6; color:#c8cedb; margin:6px 0; }
  dialog ul { margin:6px 0; padding-left:18px; }
  dialog table { width:100%; border-collapse:collapse; margin:8px 0; font-size:12.5px; }
  dialog td { padding:5px 8px 5px 0; border-bottom:1px solid #23272f;
              vertical-align:top; color:#c8cedb; }
  dialog td:last-child { text-align:right; white-space:nowrap; color:#8a91a0; }
  dialog address { font-style:normal; font-size:13px; line-height:1.65; color:#c8cedb; }
  dialog blockquote { margin:8px 0; padding:8px 12px; border-left:2px solid #3b82f6;
                      background:#14161a; font-size:12.5px; color:#b9c0cc; }
  .sheet-head { display:flex; align-items:center; gap:12px; padding:18px 26px 12px;
               border-bottom:1px solid #2a2e37; }
  .sheet-head h2 { margin:0 auto 0 0; font-size:17px; }
  .langs { display:flex; gap:4px; }
  button.lang { background:none; border:1px solid #2a2e37; color:#8a91a0;
                border-radius:6px; padding:4px 10px; font-size:12px; cursor:pointer; }
  button.lang.on { color:#e6e6e6; border-color:#3b82f6; }
  .pane[hidden] { display:none; }
  .sheet-foot { display:flex; justify-content:flex-end; gap:8px;
                padding:12px 26px; border-top:1px solid #2a2e37; background:#14161a; }
  code { font:12px ui-monospace,SFMono-Regular,Menlo,monospace; color:#b9c0cc; }
</style></head><body>
<header>
  <h1>stc-compiler <small>C &rarr; Intel HEX for STC12 / 8051, via SDCC</small></h1>
  <label>language
    <select id=language>
      <option value=pseudocode>Pseudocode</option>
      <option value=c selected>C</option>
      <option value=keil>Keil C51</option>
    </select>
  </label>
  <label>target
    <select id=target>
      <optgroup label="8051 (SDCC)">
        <option value=stc12c5a60s2>STC12C5A60S2</option>
        <option value=stc12c5a16s2>STC12C5A16S2</option>
        <option value=stc89c52rc>STC89C52RC</option>
        <option value=stc15f2k60s2>STC15F2K60S2</option>
        <option value=mcs51>generic 8051</option>
      </optgroup>
      <optgroup label="AVR (avr-gcc)">
        <option value=atmega328p>ATmega328P &mdash; Uno / Nano</option>
        <option value=atmega168p>ATmega168P</option>
      </optgroup>
    </select>
  </label>
  <label>FOSC <input id=fosc value=11059200 size=9></label>
  <label>format
    <select id=format>
      <option value=hex>.hex (packihx)</option>
      <option value=ihx>.ihx (raw)</option>
      <option value=bin>.bin</option>
    </select>
  </label>
  <button class=primary id=go>Compile</button>
  <button class=ghost id=proj title="Pick a whole Keil project's .c/.h/.uvproj files; translate, link and download one image">Keil project&hellip;</button>
  <input type=file id=projfiles multiple hidden>
  <button class=ghost id=dl disabled>Download</button>
  <button class=ghost id=copy disabled>Copy</button>
  <span id=status class=dim>ready</span>
  <button class=link id=aboutBtn title="Licences, disclaimer and imprint">About</button>
</header>
<main>
  <textarea id=code spellcheck=false>__EXAMPLE__</textarea>
  <aside>
    <div id=tabs>
      <button class=on data-pane=out>Output</button>
      <button data-pane=cgen hidden>C</button>
      <button data-pane=asm hidden>Disassembly</button>
      <button data-pane=mem>Memory</button>
      <button data-pane=log>Log</button>
    </div>
    <div id=panes>
      <pre id=out>Press Compile, or POST to <code>/compile</code> for JSON and
<code>/download</code> for the raw file.

See <a href="/docs">/docs</a> for the schema and <a href="/health">/health</a> for the SDCC version.</pre>
      <pre id=cgen hidden></pre>
      <pre id=asm hidden></pre>
      <pre id=mem hidden></pre>
      <pre id=log hidden></pre>
    </div>
  </aside>
</main>

<dialog id=about>
  <div class=sheet-head>
    <h2>stc-compiler</h2>
    <div class=langs>
      <button class="lang on" data-lang=en>English</button>
      <button class=lang data-lang=de>Deutsch</button>
    </div>
  </div>
  <div class=sheet>__ABOUT__</div>
  <div class=sheet-foot><button class=primary id=aboutClose>Close</button></div>
</dialog>

<script>
const $ = id => document.getElementById(id);
let image = null;          // {bytes: Uint8Array, filename: string}

document.querySelectorAll('#tabs button').forEach(tab => {
  tab.onclick = () => {
    document.querySelectorAll('#tabs button').forEach(b => b.classList.toggle('on', b === tab));
    ['out', 'cgen', 'asm', 'mem', 'log'].forEach(p => { $(p).hidden = (p !== tab.dataset.pane); });
  };
});

function bytesFromBase64(b64) {
  const binary = atob(b64);
  const out = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) out[i] = binary.charCodeAt(i);
  return out;
}

// .bin is not text, so show a classic offset/hex/ascii dump instead of mojibake.
function hexdump(bytes, limit = 4096) {
  const lines = [];
  const n = Math.min(bytes.length, limit);
  for (let off = 0; off < n; off += 16) {
    const row = bytes.subarray(off, Math.min(off + 16, n));
    const hex = [...row].map(b => b.toString(16).padStart(2, '0')).join(' ').padEnd(47);
    const ascii = [...row].map(b => (b >= 32 && b < 127) ? String.fromCharCode(b) : '.').join('');
    lines.push(off.toString(16).padStart(6, '0') + '  ' + hex + '  |' + ascii + '|');
  }
  if (bytes.length > limit) lines.push(`... ${bytes.length - limit} more bytes`);
  return lines.join('\n');
}

function setBusy(busy) {
  $('go').disabled = busy;
  if (busy) { $('dl').disabled = true; $('copy').disabled = true; }
}

$('go').onclick = async () => {
  setBusy(true);
  $('status').className = 'dim';
  $('status').textContent = 'compiling...';
  image = null;
  const format = $('format').value;
  try {
    const response = await fetch('/compile', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        code: $('code').value,
        language: $('language').value,
        target: $('target').value,
        fosc: parseInt($('fosc').value, 10) || null,
        format,
        disassemble: true,
      })
    });
    const data = await response.json();
    if (data.success) {
      image = {bytes: bytesFromBase64(data.base64), filename: data.filename};
      $('status').className = 'ok';
      $('status').textContent = data.bytes + ' bytes → ' + data.filename;
      $('out').textContent = (format === 'bin')
        ? hexdump(image.bytes)
        : new TextDecoder().decode(image.bytes);
      $('asm').textContent = data.disassembly || '';
      document.querySelector('#tabs button[data-pane=asm]').hidden = !data.disassembly;
      $('cgen').textContent = data.c || '';
      document.querySelector('#tabs button[data-pane=cgen]').hidden = !data.c;
      $('mem').textContent = data.memory || '(none)';
      $('log').textContent = data.log || '(no warnings)';
      $('dl').disabled = false;
      $('copy').disabled = (format === 'bin');
    } else if (data.c && data.toolchain) {
      // Transpiled fine; this service just cannot BUILD it -- a micro:bit is
      // interpreted, an Arduino sketch needs the Arduino build system. The
      // source is the result, so this is a success with a different artefact,
      // not a failure. Download hands back main.py / main.ino.
      image = {bytes: new TextEncoder().encode(data.c),
               filename: data.filename || 'main.txt'};
      $('status').className = 'ok';
      $('status').textContent = image.filename + ' \u2014 source only, needs '
                              + data.toolchain;
      $('out').textContent = data.c;
      $('cgen').textContent = data.c;
      document.querySelector('#tabs button[data-pane=cgen]').hidden = false;
      document.querySelector('#tabs button[data-pane=asm]').hidden = true;
      $('mem').textContent = '(nothing was compiled)';
      $('log').textContent = data.error || '';
      $('dl').disabled = false;
      $('copy').disabled = false;
    } else {
      $('status').className = 'err';
      $('status').textContent = 'failed';
      $('out').textContent = data.error || 'unknown error';
      $('log').textContent = data.log || data.error || '';
    }
  } catch (err) {
    $('status').className = 'err';
    $('status').textContent = 'error';
    $('out').textContent = String(err);
  }
  setBusy(false);
};

$('proj').onclick = () => $('projfiles').click();
$('projfiles').onchange = async () => {
  const picked = Array.from($('projfiles').files);
  if (!picked.length) return;
  setBusy(true);
  $('status').className = 'dim';
  $('status').textContent = 'translating ' + picked.length + ' files...';
  image = null;
  try {
    const files = {};
    for (const f of picked) files[f.name] = await f.text();
    const response = await fetch('/translate-project', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        files, link: true,
        target: $('target').value,
        format: $('format').value,
        disassemble: true,
      })
    });
    const data = await response.json();
    const notes = []
      .concat(data.isr_injected ? ['ISR prototypes injected:',
              ...data.isr_injected.map(s => '  ' + s), ''] : [])
      .concat(data.warnings ? ['warnings:',
              ...data.warnings.map(s => '  ' + s), ''] : [])
      .concat(data.log ? [data.log] : []);
    $('log').textContent = notes.join('\n') || '(no warnings)';
    if (data.success && data.base64) {
      image = {bytes: bytesFromBase64(data.base64), filename: data.filename};
      $('status').className = 'ok';
      $('status').textContent = data.bytes + ' bytes → ' + data.filename;
      $('out').textContent = ($('format').value === 'bin')
        ? hexdump(image.bytes)
        : new TextDecoder().decode(image.bytes);
      $('asm').textContent = data.disassembly || '';
      document.querySelector('#tabs button[data-pane=asm]').hidden = !data.disassembly;
      $('mem').textContent = data.memory || '(none)';
      $('dl').disabled = false;
      $('copy').disabled = ($('format').value === 'bin');
    } else {
      $('status').className = 'err';
      $('status').textContent = data.success === false ? 'failed' : 'translated, link failed';
      $('out').textContent = data.error || 'unknown error';
    }
  } catch (err) {
    $('status').className = 'err';
    $('status').textContent = 'error';
    $('out').textContent = String(err);
  }
  $('projfiles').value = '';
  setBusy(false);
};

$('dl').onclick = () => {
  if (!image) return;
  const url = URL.createObjectURL(new Blob([image.bytes], {type: 'application/octet-stream'}));
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = image.filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  // Revoke on the next tick; Safari cancels the download if it goes too early.
  setTimeout(() => URL.revokeObjectURL(url), 1000);
};

$('copy').onclick = async () => {
  if (!image) return;
  await navigator.clipboard.writeText(new TextDecoder().decode(image.bytes));
  const previous = $('copy').textContent;
  $('copy').textContent = 'Copied';
  setTimeout(() => { $('copy').textContent = previous; }, 1200);
};

const STARTERS = {c: $('code').value, pseudocode: __PSEUDO_EXAMPLE__};
let lastLanguage = $('language').value;
$('language').onchange = () => {
  const next = $('language').value;
  // Only clobber the editor if it still holds the untouched starter program.
  if ($('code').value.trim() === (STARTERS[lastLanguage] || '').trim()) {
    $('code').value = STARTERS[next];
  }
  lastLanguage = next;
  $('fosc').disabled = (next === 'pseudocode');   // pseudocode carries CLOCK
};
$('language').onchange();

$('aboutBtn').onclick = () => $('about').showModal();
document.querySelectorAll('button.lang').forEach(btn => {
  btn.onclick = () => {
    document.querySelectorAll('button.lang').forEach(b => b.classList.toggle('on', b === btn));
    document.querySelectorAll('#about .pane').forEach(pane => {
      pane.hidden = (pane.dataset.lang !== btn.dataset.lang);
    });
    $('aboutClose').textContent = (btn.dataset.lang === 'de') ? 'Schlie\u00dfen' : 'Close';
  };
});
// Start on the browser's language if it is German.
if ((navigator.language || '').toLowerCase().startsWith('de')) {
  document.querySelector('button.lang[data-lang=de]').click();
} else {
  document.querySelectorAll('#about .pane').forEach(p => { p.hidden = (p.dataset.lang !== 'en'); });
}
$('aboutClose').onclick = () => $('about').close();
// Click outside the sheet closes it; <dialog> already handles Escape.
$('about').addEventListener('click', event => {
  if (event.target === $('about')) $('about').close();
});

// Cmd/Ctrl-Enter compiles, like every other editor.
$('code').addEventListener('keydown', event => {
  if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') { event.preventDefault(); $('go').click(); }
});
</script></body></html>
"""


@app.get("/", response_class=HTMLResponse)
async def index():
    # RCDATA inside <textarea> tolerates a bare "<", but "&" would be read as
    # an entity -- and the example has "&=" in it. Escape properly.
    return (PAGE.replace("__EXAMPLE__", html.escape(EXAMPLE))
                .replace("__PSEUDO_EXAMPLE__", json.dumps(stc_pseudocode.EXAMPLE))
                .replace("__ABOUT__", about_html()))
