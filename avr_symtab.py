#!/usr/bin/env python3
"""avr_symtab -- the debugger's symbol table for AVR builds.

The exact analogue of stc_symtab, with the toolchain swapped underneath:

  8051:  sdcc --debug  ->  main.cdb   (L: records = line -> address)
  AVR:   avr-gcc -g    ->  DWARF      (objdump --dwarf=decodedline)

Everything SOURCE-side is shared with the 8051 path -- `scan_tasks` finds the
`case` labels of the cooperative scheduler and `scan_yield_map` reads the
`@bw` block map, both imported from stc_symtab, because the generated C is
deliberately the same shape on every core. Only the address plumbing differs:

  * variables come from `avr-nm -S` (address, size, name). Data-space symbols
    carry the linker's 0x800000 VMA offset; stripping it yields the address
    `cpu.data[]` / the debug target's `readMem('sram', ...)` use directly.
  * yield code addresses come from DWARF decoded lines: the same
    (file, line) -> address join the .cdb provides on the 8051. DWARF
    addresses are BYTE addresses, which is the convention the whole AVR
    boundary speaks (avr8js's word PC is converted in exactly one place,
    in bw-board's debug target).

The drift check is replicated deliberately: a @bw map that disagrees with the
case labels in the same file was written by a DIFFERENT build, and a
confidently wrong block id is worse than none. Refuse rather than emit.
"""

from __future__ import annotations

import re

from stc_symtab import scan_tasks, scan_yield_map, SymbolTableError

DATA_VMA = 0x800000  # avr-ld places the data segment at this virtual address


# ------------------------------------------------------------------ parsers

# Two symbol-table spellings, one parser. The vendored bundle carries
# avr-objdump but NOT avr-nm, so objdump -t is the production path; nm -S
# stays supported because local dev boxes have it and fixtures exist.
#
#   nm -S:        00800104 00000002 b bw_task0_state
#   objdump -t:   00800104 l     O .bss\t00000002 bw_task0_state
_NM_RE = re.compile(
    r"^([0-9a-fA-F]{8})\s+(?:([0-9a-fA-F]{8})\s+)?([A-Za-z])\s+(\S+)\s*$")
_OBJDUMP_RE = re.compile(
    r"^([0-9a-fA-F]{8})\s+\S+(?:\s+\S+)*?\s+(\.\S+)\s+([0-9a-fA-F]{8})\s+(\S+)\s*$")

_SECTION_KIND = {".bss": "b", ".data": "d", ".text": "t", ".noinit": "b"}


def parse_nm(sym_text: str) -> dict[str, dict]:
    """name -> {addr, size, kind} from nm -S OR objdump -t output.
    Data-space VMA offset already stripped."""
    out: dict[str, dict] = {}
    for line in sym_text.splitlines():
        m = _OBJDUMP_RE.match(line)
        if m:
            addr = int(m.group(1), 16)
            kind = _SECTION_KIND.get(m.group(2), "?")
            size = int(m.group(3), 16) or None
            name = m.group(4)
        else:
            m = _NM_RE.match(line)
            if not m:
                continue
            addr = int(m.group(1), 16)
            size = int(m.group(2), 16) if m.group(2) else None
            kind = m.group(3)
            name = m.group(4)
        if addr >= DATA_VMA:
            addr -= DATA_VMA
        out[name] = {"addr": addr, "size": size, "kind": kind}
    return out


# `avr-objdump --dwarf=decodedline` row:
#   main.c    92    0x18a   [view] [x]
# File names may repeat a directory prefix; the LOWEST address per
# (file, line) wins -- a line can open several address ranges and the
# breakpoint belongs at its first instruction.
_LINE_RE = re.compile(
    r"^(\S+)\s+(\d+)\s+0x([0-9a-fA-F]+)")


def parse_decodedline(dump_text: str) -> dict[tuple[str, int], int]:
    lines: dict[tuple[str, int], int] = {}
    for raw in dump_text.splitlines():
        m = _LINE_RE.match(raw.strip())
        if not m:
            continue
        fname = m.group(1).split("/")[-1]
        key = (fname, int(m.group(2)))
        addr = int(m.group(3), 16)
        if key not in lines or addr < lines[key]:
            lines[key] = addr
    return lines


# ------------------------------------------------------------------ builder

def _loc(syms: dict, name: str, default_size: int) -> dict:
    entry = syms.get(name)
    if entry is None:
        raise SymbolTableError(
            f"'{name}' is not in the ELF symbol table. Either the build was "
            "not the scheduler form (single WHEN without debug), or the "
            "linker discarded it -- check -g was on and gc-sections kept it.")
    return {"space": "sram", "addr": entry["addr"],
            "size": entry["size"] or default_size}


def build_symbol_table(nm_text: str, decodedline_text: str, c_source: str, *,
                       f_cpu: int, mcu: str,
                       source_name: str = "main.c") -> dict:
    tasks = scan_tasks(c_source)
    yield_map = scan_yield_map(c_source)

    if not tasks:
        raise SymbolTableError(
            "no bw_taskN functions in this source. A single-WHEN program "
            "compiles to a plain loop with no scheduler and no per-task "
            "state, so there is no Level 1 position to describe. Build with "
            "debug=true to force the scheduler form.")

    syms = parse_nm(nm_text)
    lines = parse_decodedline(decodedline_text)
    if not lines:
        raise SymbolTableError(
            "the ELF carries no DWARF line records, so nothing has a code "
            "address. Check the compile ran with -g.")

    # The same refusal the 8051 path makes, for the same reason.
    if yield_map:
        from_header = {(t, s) for t, states in yield_map.items() for s in states}
        from_source = {(t, s) for t, cases in tasks.items() for s, _, _ in cases}
        if from_header != from_source:
            only_header = sorted(from_header - from_source)
            only_source = sorted(from_source - from_header)
            raise SymbolTableError(
                "the @bw yield map disagrees with the case labels in the "
                "same file. It was written by a different build than this C, "
                "so every block id in it is suspect.\n"
                f"  in the header but not the source: {only_header}\n"
                f"  in the source but not the header: {only_source}")

    out_tasks = []
    for name in sorted(tasks, key=lambda n: int(n[len("bw_task"):])):
        yields = []
        for state, lineno, label in sorted(tasks[name]):
            entry: dict = {"state": state, "label": label}
            # A bare `case N:` label emits no instruction, so DWARF may have
            # no record for ITS line; the state's code address is the first
            # statement after it. Walk forward a few lines — the nearest
            # following record is the breakpoint the label means.
            addr = None
            for probe in range(lineno, lineno + 6):
                addr = lines.get((source_name, probe))
                if addr is not None:
                    break
            if addr is not None:
                entry["addr"] = addr
            block = yield_map.get(name, {}).get(state)
            if block is not None:
                entry["block"] = block
            yields.append(entry)
        out_tasks.append({
            "name": name,
            "state": _loc(syms, f"{name}_state", 2),
            "until": _loc(syms, f"{name}_until", 2),
            "yields": yields,
        })

    table = {
        "fosc": f_cpu,
        "device": mcu,
        "scheduler": {
            # uint32_t on the AVR -- the 8051's is 2 bytes; consumers read
            # the size field rather than assuming (bw-board's target does).
            "bw_ms": _loc(syms, "bw_ms", 4),
            "tasks": out_tasks,
        },
    }

    variables = []
    for name, entry in sorted(syms.items()):
        if entry["kind"] not in "bBdD":
            continue
        if name == "bw_ms" or re.fullmatch(r"bw_task\d+_(state|until)", name):
            continue
        if name.startswith(("__", "_edata", "_end")):
            continue
        variables.append({"name": name, "space": "sram",
                          "addr": entry["addr"],
                          "size": entry["size"] or 2})
    if variables:
        table["variables"] = variables
    return table
