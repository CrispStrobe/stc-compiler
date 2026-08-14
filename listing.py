"""listing.py — extract the {asm, lineMap, format, v} artifact.

Three consumers: the assembler-lane R1 asm view, the R3 debugger stepping
map, and the AVR/ARM debug targets' disasm panes.

Per toolchain:
  sdcc  — reads the .rst file (source-interleaved relocatable listing)
  avr   — runs avr-objdump -dS on the ELF + --dwarf=decodedline
  arm   — runs arm-none-eabi-objdump -dS on the ELF + --dwarf=decodedline
"""
import os
import re
import subprocess

# Response version.
VERSION = 1


def from_objdump(bin_dir: str, prefix: str, elf: str, env: dict,
                 format_id: str) -> dict:
    """Extract listing from gcc toolchains (AVR, ARM) via objdump.

    Returns {asm, lineMap, format, v} or {error}.
    """
    objdump = os.path.join(bin_dir, f"{prefix}-objdump")

    # Source-interleaved disassembly
    try:
        dumped = subprocess.run(
            [objdump, "-d", "-S", elf],
            capture_output=True, text=True, timeout=20, env=env)
        asm = dumped.stdout or ""
    except (OSError, subprocess.SubprocessError) as exc:
        return {"error": f"objdump -dS failed: {exc}"}

    # DWARF decoded line table → lineMap
    try:
        decoded = subprocess.run(
            [objdump, "--dwarf=decodedline", elf],
            capture_output=True, text=True, timeout=15, env=env)
        line_map = _parse_decodedline(decoded.stdout or "")
    except (OSError, subprocess.SubprocessError) as exc:
        return {"error": f"objdump --dwarf=decodedline failed: {exc}"}

    return {"asm": asm, "lineMap": line_map, "format": format_id, "v": VERSION}


def from_sdcc_rst(rst_path: str) -> dict:
    """Extract listing from SDCC's .rst (relocatable source listing).

    The .rst format interleaves C source lines (prefixed with ;) with
    assembled output (addr hex [cycles] line asm). We parse both into
    the asm text and a lineMap.

    Returns {asm, lineMap, format, v} or {error}.
    """
    try:
        with open(rst_path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except FileNotFoundError:
        return {"error": f"no .rst file at {os.path.basename(rst_path)}"}

    line_map = []
    # SDCC .rst lines with addresses look like:
    #   000062 C2 90            [12]  649 	clr	_P1_0
    # Source reference lines look like:
    #   ;	/tmp/sdcc-test.c:3: void main(void) { ...
    current_file = None
    current_line = None
    for row in text.splitlines():
        # Track source-line references. Format:
        #   <spaces> <rst_lineno> ;\t<path>:<line>: <source text>
        src_m = re.search(r";\s*(.+?):(\d+):\s", row)
        if src_m and not re.match(r"\s*[0-9A-Fa-f]{4,6}\s", row):
            current_file = os.path.basename(src_m.group(1))
            current_line = int(src_m.group(2))
            continue
        # Track code lines with addresses. Format:
        #   <spaces> <addr> <hex bytes> [cycles] <rst_lineno> <asm>
        addr_m = re.match(r"\s*([0-9A-Fa-f]{4,6})\s+[0-9A-Fa-f]{2}", row)
        if addr_m and current_file and current_line is not None:
            addr = int(addr_m.group(1), 16)
            line_map.append({"addr": addr, "file": current_file, "line": current_line})

    return {"asm": text, "lineMap": line_map, "format": "sdcc", "v": VERSION}


def _parse_decodedline(text: str) -> list[dict]:
    """Parse objdump --dwarf=decodedline into [{addr, file, line}].

    Example input:
        CU: main.c:
        File name                            Line number    Starting address
        main.c                                        4               0x100
        main.c                                        5               0x100
    """
    entries = []
    for m in re.finditer(
            r"^(\S+)\s+(\d+)\s+0x([0-9a-fA-F]+)",
            text, re.MULTILINE):
        entries.append({
            "addr": int(m.group(3), 16),
            "file": m.group(1),
            "line": int(m.group(2)),
        })
    return entries
