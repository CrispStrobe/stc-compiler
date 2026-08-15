"""assemble.py — raw-assemble for four toolchains.

Request: {asm source, target/toolchain}
Response mirrors compile: {success, base64, errors: [{line, message}],
  listing: {asm, lineMap, format, v:1}, filename, bytes}

Toolchains:
  8051:  sdas8051 + sdld → Intel HEX
  6502:  ca65 + ld65 (eater.cfg) → raw binary + ld65 -Ln labels
  AVR:   avr-gcc -x assembler-with-cpp → Intel HEX via objcopy
  ARM:   arm-none-eabi-gcc -x assembler-with-cpp → Intel HEX via objcopy
"""
import base64
import os
import re
import shutil
import subprocess
import tempfile
import uuid

import listing as listing_mod

COMPILE_TIMEOUT = 30

# ---- error parsers (normalize assembler stderr to [{line, message}]) --------

def _parse_sdas_errors(text: str, src_basename: str) -> list[dict]:
    """sdas8051: file:line: Error: message"""
    errors = []
    for m in re.finditer(r"^(.+?):(\d+):\s*(?:Error|Warning):\s*(.+)", text, re.M):
        if src_basename in m.group(1):
            errors.append({"line": int(m.group(2)), "message": m.group(3).strip()})
    # Also catch linker errors (sdld prints differently)
    for m in re.finditer(r"^\?ASlink-Error-.+", text, re.M):
        errors.append({"line": 0, "message": m.group(0).strip()})
    return errors


def _parse_ca65_errors(text: str, src_basename: str) -> list[dict]:
    """ca65: file(line): Error: message"""
    errors = []
    for m in re.finditer(r"^(.+?)\((\d+)\):\s*(?:Error|Warning):\s*(.+)", text, re.M):
        if src_basename in m.group(1):
            errors.append({"line": int(m.group(2)), "message": m.group(3).strip()})
    # ld65 errors (skip the harmless STARTUP segment warning)
    for m in re.finditer(r"^ld65:\s*(?:Error|Warning):\s*(.+)", text, re.M):
        msg = m.group(1).strip()
        if "Segment 'STARTUP' does not exist" in msg:
            continue
        errors.append({"line": 0, "message": msg})
    return errors


def _parse_gas_errors(text: str, src_basename: str) -> list[dict]:
    """GNU as (via gcc): file:line: Error: message"""
    errors = []
    for m in re.finditer(r"^(.+?):(\d+):\s*(?:Error|Warning):\s*(.+)", text, re.M):
        if src_basename in m.group(1):
            errors.append({"line": int(m.group(2)), "message": m.group(3).strip()})
    return errors


# ---- per-toolchain assemble functions ---------------------------------------

def assemble_8051(source: str, bin_dir: str | None = None) -> dict:
    """Assemble 8051 source with sdas8051 + sdld."""
    work = os.path.join(tempfile.gettempdir(), f"asm-{uuid.uuid4().hex}")
    os.makedirs(work, exist_ok=True)
    src = os.path.join(work, "main.asm")
    with open(src, "w", encoding="utf-8") as f:
        f.write(source)

    try:
        # Assemble: -p (paging) -l (listing) -o (object) -s (symbols) -g (debug) -ff (flat)
        rel = os.path.join(work, "main.rel")
        lst = os.path.join(work, "main.lst")
        sdas = os.path.join(bin_dir, "sdas8051") if bin_dir else "sdas8051"
        sdld = os.path.join(bin_dir, "sdld") if bin_dir else "sdld"
        result = subprocess.run(
            [sdas, "-plosgff", src],
            capture_output=True, text=True, timeout=COMPILE_TIMEOUT, cwd=work)
        stderr = (result.stdout or "") + (result.stderr or "")
        stderr = stderr.replace(work + os.sep, "")

        if not os.path.exists(rel):
            return {"success": False,
                    "errors": _parse_sdas_errors(stderr, "main.asm") or
                              [{"line": 0, "message": stderr.strip() or "assembly failed"}],
                    "log": stderr}

        # Link
        ihx = os.path.join(work, "main.ihx")
        link_result = subprocess.run(
            [sdld, "-n", "-i", ihx, rel],
            capture_output=True, text=True, timeout=COMPILE_TIMEOUT, cwd=work)
        link_stderr = ((link_result.stdout or "") + (link_result.stderr or "")).replace(work + os.sep, "")
        stderr += link_stderr

        if not os.path.exists(ihx):
            return {"success": False,
                    "errors": _parse_sdas_errors(stderr, "main.asm") or
                              [{"line": 0, "message": link_stderr.strip() or "link failed"}],
                    "log": stderr}

        with open(ihx, "rb") as f:
            blob = f.read()

        # Listing from .lst
        listing_artifact = None
        if os.path.exists(lst):
            listing_artifact = listing_mod.from_sdcc_rst(lst)

        return {"success": True,
                "base64": base64.b64encode(blob).decode("ascii"),
                "filename": "main.ihx",
                "bytes": len(blob),
                "errors": _parse_sdas_errors(stderr, "main.asm"),
                "listing": listing_artifact,
                "log": stderr,
                "toolchain": "sdas8051"}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def assemble_z80(source: str, bin_dir: str | None = None) -> dict:
    """Assemble Z80 source with sdasz80 + sdldz80.

    Output is a raw binary (via makebin on the Intel HEX), suitable for
    loading into the Z80 machine's ROM.
    """
    work = os.path.join(tempfile.gettempdir(), f"asm-{uuid.uuid4().hex}")
    os.makedirs(work, exist_ok=True)
    src = os.path.join(work, "main.asm")
    with open(src, "w", encoding="utf-8") as f:
        f.write(source)

    try:
        rel = os.path.join(work, "main.rel")
        lst = os.path.join(work, "main.lst")
        sdas = os.path.join(bin_dir, "sdasz80") if bin_dir else "sdasz80"
        sdld = os.path.join(bin_dir, "sdldz80") if bin_dir else "sdldz80"
        mkbin = os.path.join(bin_dir, "makebin") if bin_dir else "makebin"
        result = subprocess.run(
            [sdas, "-plosgff", src],
            capture_output=True, text=True, timeout=COMPILE_TIMEOUT, cwd=work)
        stderr = (result.stdout or "") + (result.stderr or "")
        stderr = stderr.replace(work + os.sep, "")

        if not os.path.exists(rel):
            return {"success": False,
                    "errors": _parse_sdas_errors(stderr, "main.asm") or
                              [{"line": 0, "message": stderr.strip()
                                or "assembly failed"}],
                    "log": stderr}

        # Link to Intel HEX
        ihx = os.path.join(work, "main.ihx")
        link_result = subprocess.run(
            [sdld, "-n", "-i", ihx, rel],
            capture_output=True, text=True, timeout=COMPILE_TIMEOUT, cwd=work)
        link_stderr = ((link_result.stdout or "") +
                       (link_result.stderr or "")).replace(work + os.sep, "")
        stderr += link_stderr

        if not os.path.exists(ihx):
            return {"success": False,
                    "errors": _parse_sdas_errors(stderr, "main.asm") or
                              [{"line": 0, "message": link_stderr.strip()
                                or "link failed"}],
                    "log": stderr}

        # Convert IHX → raw binary via makebin
        out_bin = os.path.join(work, "main.bin")
        mk_result = subprocess.run(
            [mkbin, "-s", "32768", ihx],
            capture_output=True, timeout=COMPILE_TIMEOUT, cwd=work)
        if mk_result.returncode != 0:
            return {"success": False,
                    "errors": [{"line": 0,
                                "message": "makebin failed"}],
                    "log": stderr}
        with open(out_bin, "wb") as f:
            f.write(mk_result.stdout)

        blob = mk_result.stdout

        # Listing from .lst
        listing_artifact = None
        if os.path.exists(lst):
            listing_artifact = listing_mod.from_sdcc_rst(lst)

        return {"success": True,
                "base64": base64.b64encode(blob).decode("ascii"),
                "filename": "main.bin",
                "bytes": len(blob),
                "errors": _parse_sdas_errors(stderr, "main.asm"),
                "listing": listing_artifact,
                "log": stderr,
                "toolchain": "sdasz80"}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def assemble_6502(source: str, cfg_path: str,
                   bin_dir: str | None = None) -> dict:
    """Assemble 6502 source with ca65 + ld65.

    bin_dir: directory holding the cc65 binaries (ca65, ld65).
    Falls back to system PATH when None.
    """
    ca65_bin = os.path.join(bin_dir, "ca65") if bin_dir else "ca65"
    ld65_bin = os.path.join(bin_dir, "ld65") if bin_dir else "ld65"

    work = os.path.join(tempfile.gettempdir(), f"asm-{uuid.uuid4().hex}")
    os.makedirs(work, exist_ok=True)
    src = os.path.join(work, "main.s")
    with open(src, "w", encoding="utf-8") as f:
        f.write(source)

    try:
        # Assemble
        obj = os.path.join(work, "main.o")
        lst = os.path.join(work, "main.lst")
        result = subprocess.run(
            [ca65_bin, "--cpu", "65C02", "-l", lst, "-o", obj, src],
            capture_output=True, text=True, timeout=COMPILE_TIMEOUT, cwd=work)
        stderr = (result.stderr or "").replace(work + os.sep, "")

        if result.returncode != 0 or not os.path.exists(obj):
            return {"success": False,
                    "errors": _parse_ca65_errors(stderr, "main.s") or
                              [{"line": 0, "message": stderr.strip() or "assembly failed"}],
                    "log": stderr}

        # Link
        out_bin = os.path.join(work, "main.bin")
        labels = os.path.join(work, "main.labels")
        # Copy the config into work so paths don't leak
        local_cfg = os.path.join(work, "eater.cfg")
        shutil.copy2(cfg_path, local_cfg)
        link_result = subprocess.run(
            [ld65_bin, "-C", local_cfg, "-Ln", labels, "-o", out_bin, obj],
            capture_output=True, text=True, timeout=COMPILE_TIMEOUT, cwd=work)
        link_stderr = (link_result.stderr or "").replace(work + os.sep, "")
        stderr += link_stderr

        if link_result.returncode != 0 or not os.path.exists(out_bin):
            return {"success": False,
                    "errors": _parse_ca65_errors(stderr, "main.s") or
                              [{"line": 0, "message": link_stderr.strip() or "link failed"}],
                    "log": stderr}

        with open(out_bin, "rb") as f:
            blob = f.read()

        # Labels file
        labels_text = None
        if os.path.exists(labels):
            with open(labels, encoding="utf-8") as f:
                labels_text = f.read()

        # Listing from ca65 -l output
        listing_artifact = None
        if os.path.exists(lst):
            listing_artifact = _listing_from_ca65(lst)

        return {"success": True,
                "base64": base64.b64encode(blob).decode("ascii"),
                "filename": "main.bin",
                "bytes": len(blob),
                "errors": _parse_ca65_errors(stderr, "main.s"),
                "listing": listing_artifact,
                "labels": labels_text,
                "log": stderr,
                "toolchain": "ca65"}
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _listing_from_ca65(lst_path: str) -> dict:
    """Parse ca65's listing output into the standard {asm, lineMap, format, v} shape."""
    try:
        with open(lst_path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except FileNotFoundError:
        return {"error": "no listing file"}

    line_map = []
    # ca65 listing format:
    #   000000r 1  A9 FF            lda #$FF
    # addr(r=relocatable) file_idx code... instruction
    for m in re.finditer(
            r"^([0-9A-Fa-f]{6})[r ]?\s+(\d+)\s+[0-9A-Fa-f]{2}", text, re.M):
        addr = int(m.group(1), 16)
        line_num = int(m.group(2))
        line_map.append({"addr": addr, "file": "main.s", "line": line_num})

    return {"asm": text, "lineMap": line_map, "format": "ca65", "v": listing_mod.VERSION}


def assemble_avr(source: str, mcu: str, bin_dir: str, env: dict) -> dict:
    """Assemble AVR source with avr-gcc -x assembler-with-cpp."""
    work = os.path.join(tempfile.gettempdir(), f"asm-{uuid.uuid4().hex}")
    os.makedirs(work, exist_ok=True)
    src = os.path.join(work, "main.S")
    with open(src, "w", encoding="utf-8") as f:
        f.write(source)

    try:
        elf = os.path.join(work, "main.elf")
        gcc = os.path.join(bin_dir, "avr-gcc")
        cmd = [gcc, f"-mmcu={mcu}", "-nostdlib", "-gdwarf-2",
               "-x", "assembler-with-cpp", "-o", elf, src]
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=COMPILE_TIMEOUT, cwd=work, env=env)
        stderr = ((result.stdout or "") + (result.stderr or "")).replace(work + os.sep, "")

        if result.returncode != 0 or not os.path.exists(elf):
            return {"success": False,
                    "errors": _parse_gas_errors(stderr, "main.S") or
                              [{"line": 0, "message": stderr.strip() or "assembly failed"}],
                    "log": stderr}

        # Extract hex
        ihx = os.path.join(work, "main.hex")
        objcopy = os.path.join(bin_dir, "avr-objcopy")
        subprocess.run([objcopy, "-O", "ihex", "-R", ".eeprom", elf, ihx],
                       capture_output=True, timeout=10, env=env)
        if not os.path.exists(ihx):
            return {"success": False,
                    "errors": [{"line": 0, "message": "objcopy produced no image"}],
                    "log": stderr}

        with open(ihx, "rb") as f:
            blob = f.read()

        # Listing via objdump
        listing_artifact = listing_mod.from_objdump(
            bin_dir, "avr", elf, env, "avr-gcc")

        return {"success": True,
                "base64": base64.b64encode(blob).decode("ascii"),
                "filename": "main.hex",
                "bytes": len(blob),
                "errors": _parse_gas_errors(stderr, "main.S"),
                "listing": listing_artifact,
                "log": stderr,
                "toolchain": "avr-gcc"}
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---- bare-metal rejection patterns -----------------------------------------

_CODAL_SOFTDEVICE_RE = re.compile(
    r"\b(?:MICROBIT_\w|codal_\w|MicroBit[A-Z]\w|DEVICE_COMPONENT_\w|"
    r"nrf_sdh_\w|softdevice|NRF_SDH_\w|CODAL_\w)", re.I)


def _reject_codal_softdevice(source: str) -> str | None:
    """Return a human-readable reason if the source needs CODAL or SoftDevice."""
    m = _CODAL_SOFTDEVICE_RE.search(source)
    if m:
        return (f"source references '{m.group(0)}' which requires "
                f"CODAL or SoftDevice runtime; this endpoint is bare-metal "
                f"only (no runtime linked, like the Pico contract)")
    return None


def assemble_arm(source: str, mcu: str, bin_dir: str, env: dict,
                 ld_script: str) -> dict:
    """Assemble ARM source with arm-none-eabi-gcc -x assembler-with-cpp.

    Output is Intel HEX (objcopy -O ihex) — the format DAPLink MSD
    drag-flash accepts on micro:bit V2.
    """
    reason = _reject_codal_softdevice(source)
    if reason:
        return {"success": False,
                "errors": [{"line": 0, "message": reason}],
                "toolchain": "arm-none-eabi-gcc"}

    work = os.path.join(tempfile.gettempdir(), f"asm-{uuid.uuid4().hex}")
    os.makedirs(work, exist_ok=True)
    src = os.path.join(work, "main.s")
    with open(src, "w", encoding="utf-8") as f:
        f.write(source)

    # Copy linker script into work dir so paths don't leak into diagnostics
    local_ld = os.path.join(work, os.path.basename(ld_script))
    shutil.copy2(ld_script, local_ld)

    try:
        elf = os.path.join(work, "main.elf")
        gcc = os.path.join(bin_dir, "arm-none-eabi-gcc")
        cmd = [gcc, f"-mcpu={mcu}", "-mthumb", "-nostdlib",
               "-gdwarf-2",
               "-x", "assembler-with-cpp",
               f"-T{local_ld}", "-o", elf, src]
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=COMPILE_TIMEOUT, cwd=work, env=env)
        stderr = ((result.stdout or "") + (result.stderr or "")).replace(
            work + os.sep, "")

        if result.returncode != 0 or not os.path.exists(elf):
            return {"success": False,
                    "errors": _parse_gas_errors(stderr, "main.s") or
                              [{"line": 0, "message": stderr.strip()
                                or "assembly failed"}],
                    "log": stderr}

        # Extract Intel HEX (DAPLink drag-flash format)
        ihx = os.path.join(work, "main.hex")
        objcopy = os.path.join(bin_dir, "arm-none-eabi-objcopy")
        subprocess.run([objcopy, "-O", "ihex", elf, ihx],
                       capture_output=True, timeout=10, env=env)
        if not os.path.exists(ihx):
            return {"success": False,
                    "errors": [{"line": 0,
                                "message": "objcopy produced no image"}],
                    "log": stderr}

        with open(ihx, "rb") as f:
            blob = f.read()

        # Listing via objdump
        listing_artifact = listing_mod.from_objdump(
            bin_dir, "arm-none-eabi", elf, env, "arm-gcc")

        return {"success": True,
                "base64": base64.b64encode(blob).decode("ascii"),
                "filename": "main.hex",
                "bytes": len(blob),
                "errors": _parse_gas_errors(stderr, "main.s"),
                "listing": listing_artifact,
                "log": stderr,
                "toolchain": "arm-none-eabi-gcc"}
    finally:
        shutil.rmtree(work, ignore_errors=True)
