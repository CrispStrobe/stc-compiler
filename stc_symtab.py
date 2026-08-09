"""
stc_symtab — build the debug symbol table that the emulators and the on-chip
monitor all read, out of SDCC's own debug output.

Why this lives here. `stc/docs/DEBUG-CONTROL-MODEL.md` §2 says a debugger's
first question is "where am I", and that on this toolchain the answer is
already sitting in RAM: the cooperative scheduler keeps its position in
`bw_ms`, `<task>_state` and `<task>_until`. Reading them needs their addresses,
and the `case` label addresses for yield breakpoints. Neither emulator parses
`.cdb` — by agreement, both take a symbol table as input (`ucsim-stc`
spec-updates/004). Producing it needs both halves of the problem: the emitter's
knowledge of what a task looks like, and the linker's knowledge of where things
landed. Only this repo has both.

Output is the 004 format:

    {
      "fosc": 11059200,
      "device": "stc12c5a60s2",
      "scheduler": {
        "bw_ms": {"space": "iram", "addr": 8, "size": 2},
        "tasks": [
          {"name": "bw_task0", "func_addr": 226,
           "state": {"space": "iram", "addr": 12, "size": 2},
           "until": {"space": "iram", "addr": 14, "size": 2},
           "yields": [{"state": 0, "label": "entry", "addr": 261,
                       "block": "j:p^E*hF,qR%nT.b|"}, ...]}
        ]
      }
    }

`block` is the Scratch block id this yield belongs to, and it is what lets a
front end glow the block a halted program is sitting on rather than print a
number (`sb3-creator/reference/debugger-ui.md` §2). It appears only when the C
carries sb3-creator's `@bw yield` header — a debug build of a Brickwright
project. Hand-written firmware has no blocks to point at, so its symbol table
simply has no `block` keys, which is not an error.

The on-chip monitor reads a subset of the same file: it has no code
breakpoints, so it ignores `yields[].addr` and matches `(task, state)` in its
dispatch loop instead. One file, three consumers.

Usage:

    python3 stc_symtab.py --cdb build/main.cdb --source build/main.c \\
        --fosc 11059200 --device stc12c5a60s2 -o symbols.json

The `.cdb` must be the LINKED one (sdcc --debug leaves it beside the .ihx);
the compile-only `.cdb` has the symbols but not the addresses.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from urllib.parse import unquote

# --------------------------------------------------------------- address spaces
#
# The letter after the type parentheses in an S: record is SDCC's address
# space. Two of these are load bearing here and were both confirmed against
# real builds rather than taken from documentation, because getting one wrong
# does not fail loudly — it silently reads the wrong memory:
#
#   E  a `static unsigned int` in the default data model   -> DSEG, direct IRAM
#   F  a `__xdata` array                                   -> external RAM
#
# The rest are mapped for completeness. Anything not listed raises, rather
# than being guessed at: `iram` and `sfr` overlap numerically at 0x80..0xFF and
# are different memories, so a wrong guess here is undetectable downstream.
CDB_SPACE = {
    "B": "iram",    # internal stack
    "C": "code",
    "D": "code",    # code / static segment
    "E": "iram",    # internal ram, directly addressable  (verified)
    "F": "xram",    # external ram                        (verified)
    "G": "iram",    # internal ram, indirect only (idata)
    "H": "bit",
    "I": "sfr",
    "J": "bit",     # sbit
}

TASK_RE = re.compile(r"^\s*static\s+void\s+(bw_task\d+)\s*\(\s*void\s*\)\s*$")
CASE_RE = re.compile(r"^\s*case\s+(\d+)\s*:")

# `@bw yield <task> <state> <percent-encoded block id> <kind>` in sb3-creator's
# marker header. The id is percent-encoded because Scratch's block-id alphabet
# contains both `*` and `/`, which would otherwise close the C comment the
# header lives in.
YIELD_RE = re.compile(r"@bw\s+yield\s+(\w+)\s+(\d+)\s+(\S+)\s+(\S+)\s*$")

# `@bw var <c name> "<original name>" [sprite "<sprite>"]`. The C name is
# mangled and the original is what the user typed, so only the emitter can
# relate them -- and the ORIGINAL is the only one a front end should ever show.
VAR_RE = re.compile(r'@bw\s+var\s+(\w+)\s+"((?:[^"\\]|\\.)*)"'
                    r'(?:\s+sprite\s+"((?:[^"\\]|\\.)*)")?')


class SymbolTableError(Exception):
    pass


# ------------------------------------------------------------------ .cdb parsing


class Cdb:
    """The three record kinds we need out of an SDCC .cdb."""

    def __init__(self, text: str):
        self.module = None
        self.spaces: dict[str, str] = {}      # symbol name -> address space
        self.addrs: dict[str, int] = {}       # symbol name -> address
        self.lines: dict[tuple[str, int], int] = {}   # (file, line) -> address

        for record in text.splitlines():
            if record.startswith("M:"):
                self.module = record[2:].strip()
            elif record.startswith("S:"):
                self._symbol(record)
            elif record.startswith("L:"):
                self._link(record)

    # S:Fmulti$bw_ms$0_0$0({2}SI:U),E,0,0
    def _symbol(self, record: str) -> None:
        m = re.match(r"^S:(?:F\w+|G|L\w+)\$([^$]+)\$[^(]*\([^)]*\),([A-Za-z]),", record)
        if m:
            self.spaces[m.group(1)] = m.group(2)

    # L:Fmulti$bw_ms$0_0$0:8        — a symbol's address, in hex
    # L:C$multi.c$45$0_0$2:105      — a source line's address, in hex
    def _link(self, record: str) -> None:
        m = re.match(r"^L:C\$([^$]+)\$(\d+)\$[^:]*:([0-9A-Fa-f]+)$", record)
        if m:
            key = (m.group(1), int(m.group(2)))
            # A line can appear more than once (loops, fallthrough). The first
            # address is the one execution reaches first, which is what a
            # breakpoint on that line should mean.
            self.lines.setdefault(key, int(m.group(3), 16))
            return

        m = re.match(r"^L:(?:F\w+|G|L\w+)\$([^$]+)\$[^:]*:([0-9A-Fa-f]+)$", record)
        if m:
            self.addrs.setdefault(m.group(1), int(m.group(2), 16))

    def location(self, name: str, size: int) -> dict:
        if name not in self.addrs:
            raise SymbolTableError(
                f"{name!r} has no address in the .cdb. Is this the linked .cdb "
                f"rather than the compile-only one?"
            )
        letter = self.spaces.get(name)
        if letter is None:
            raise SymbolTableError(f"{name!r} has an address but no symbol record")
        if letter not in CDB_SPACE:
            raise SymbolTableError(
                f"{name!r} is in SDCC address space {letter!r}, which this tool "
                f"does not map. Refusing to guess: iram and sfr share addresses, "
                f"so a wrong space is undetectable downstream."
            )
        return {"space": CDB_SPACE[letter], "addr": self.addrs[name], "size": size}


# ------------------------------------------------------------- C source scanning


def scan_tasks(source: str) -> dict[str, list[tuple[int, int, str]]]:
    """Find each bw_taskN and its `case` labels.

    Returns {task_name: [(state, line_number, label), ...]}.

    Scanning the generated C rather than reaching into the emitter is
    deliberate: it works on anything that emits this scheduler shape,
    including sb3-creator's generateC(), which is a separate implementation.
    """
    lines = source.splitlines()
    tasks: dict[str, list[tuple[int, int, str]]] = {}
    current: str | None = None
    depth = 0
    started = False

    for i, raw in enumerate(lines, start=1):
        m = TASK_RE.match(raw)
        if m and current is None:
            current, depth, started = m.group(1), 0, False
            tasks[current] = []
            continue

        if current is None:
            continue

        depth += raw.count("{") - raw.count("}")
        if raw.count("{"):
            started = True

        c = CASE_RE.match(raw)
        if c:
            state = int(c.group(1))
            label = _label_for(lines, i, current, state)
            tasks[current].append((state, i, label))

        if started and depth <= 0:
            current = None

    return tasks


def scan_yield_map(source: str) -> dict[str, dict[int, str]]:
    """The `(task, state) -> Scratch block id` map out of the `@bw` header.

    Only sb3-creator's `generateC(project, {debug: true})` writes this. Without
    it a Level 1 position is a number a front end cannot point at; with it the
    block editor can glow the block a halted program is sitting on.

    Returns {} for C that carries no header, which is the normal case for
    hand-written firmware and not an error.
    """
    header = re.search(r"@bw-begin(.*?)@bw-end", source, re.S)
    if not header:
        return {}
    out: dict[str, dict[int, str]] = {}
    for line in header.group(1).splitlines():
        m = YIELD_RE.search(line)
        if m:
            out.setdefault(m.group(1), {})[int(m.group(2))] = unquote(m.group(3))
    return out


def scan_variables(source: str) -> list[dict]:
    """The project's own variables, out of the `@bw` header.

    A debugger for this toolchain should be able to show `counter`, not
    `_counter` and certainly not "the int at IRAM 0x0A". The C name is mangled
    (`cName`) and not reversible, so the emitter states the pair and this
    carries it through to whoever renders it.

    Returns [] for C with no header, which is not an error.
    """
    header = re.search(r"@bw-begin(.*?)@bw-end", source, re.S)
    if not header:
        return []
    out = []
    for line in header.group(1).splitlines():
        m = VAR_RE.search(line)
        if m:
            entry = {"c": m.group(1), "name": m.group(2)}
            if m.group(3):
                entry["sprite"] = m.group(3)
            out.append(entry)
    return out


def _label_for(lines: list[str], lineno: int, task: str, state: int) -> str:
    """A human-readable name for a yield point. Advisory only.

    Nothing consumes this semantically — it exists so a person reading the
    symbol table or a breakpoint list can tell which block they are looking at.
    Derived from the statement the case label guards.
    """
    if state == 0:
        return "entry"
    nxt = ""
    for candidate in lines[lineno:lineno + 3]:
        if candidate.strip():
            nxt = candidate.strip()
            break
    if re.search(rf"bw_now\(\)\s*-\s*{re.escape(task)}_until", nxt):
        return "wait"
    if nxt.startswith("if (bw_i"):
        return "repeat_top"
    if nxt.startswith("if (!(") or nxt.startswith("if ("):
        return "wait_until"
    return "loop_top"


# ------------------------------------------------------------------- assembly


def build_symbol_table(cdb_text: str, c_source: str, *, fosc: int,
                       device: str, source_name: str | None = None) -> dict:
    cdb = Cdb(cdb_text)
    tasks = scan_tasks(c_source)
    yield_map = scan_yield_map(c_source)

    if not tasks:
        raise SymbolTableError(
            "no bw_taskN functions in this source. A single-WHEN program "
            "compiles to a plain loop with no scheduler and no per-task state, "
            "so there is no Level 1 position to describe and no symbol table "
            "to write. Only multi-WHEN programs need one."
        )

    if not cdb.lines and not cdb.addrs:
        raise SymbolTableError(
            "this .cdb has no L: records at all, so nothing in it has an "
            "address. That is the compile-only .cdb; the linked one is written "
            "beside the .ihx. Check the build used --debug."
        )

    # The line records are keyed by the file name the compiler saw.
    if source_name is None:
        files = {f for f, _ in cdb.lines}
        if len(files) == 1:
            source_name = files.pop()
        else:
            candidates = sorted(f for f in files if cdb.module and f.startswith(cdb.module))
            if not candidates:
                raise SymbolTableError(
                    f"cannot tell which of {sorted(files)} is the source; "
                    f"pass --source-name"
                )
            source_name = candidates[0]

    # If the C carries a yield map, it must describe exactly the `case` labels
    # that are actually in the file. A map that disagrees is worse than no map:
    # it would point a front end at a confidently wrong block, and nothing
    # downstream could detect it. Refuse rather than emit one.
    if yield_map:
        from_header = {(t, s) for t, states in yield_map.items() for s in states}
        from_source = {(t, s) for t, cases in tasks.items() for s, _, _ in cases}
        if from_header != from_source:
            only_header = sorted(from_header - from_source)
            only_source = sorted(from_source - from_header)
            raise SymbolTableError(
                "the @bw yield map disagrees with the case labels in the same "
                "file. It was written by a different build than this C, so "
                "every block id in it is suspect.\n"
                f"  in the header but not the source: {only_header}\n"
                f"  in the source but not the header: {only_source}"
            )

    out_tasks = []
    for name in sorted(tasks, key=lambda n: int(n[len("bw_task"):])):
        yields = []
        for state, lineno, label in sorted(tasks[name]):
            entry = {"state": state, "label": label}
            addr = cdb.lines.get((source_name, lineno))
            if addr is not None:
                entry["addr"] = addr
            block = yield_map.get(name, {}).get(state)
            if block is not None:
                entry["block"] = block
            yields.append(entry)

        missing = [y["state"] for y in yields if "addr" not in y]
        if missing:
            raise SymbolTableError(
                f"{name}: no code address for the case labels of states {missing}. "
                f"Was the image built with --debug?"
            )

        out_tasks.append({
            "name": name,
            "func_addr": cdb.addrs.get(name),
            "state": cdb.location(f"{name}_state", 2),
            "until": cdb.location(f"{name}_until", 2),
            "yields": yields,
        })

    # Variables, when the C says which are the user's. Every one is a 16-bit
    # int (generateC emits `static int`), so `size` is not guessed. A variable
    # the linker optimised away is REPORTED as unlocated rather than dropped:
    # a front end that simply omits it leaves the user wondering where their
    # variable went.
    variables = []
    for entry in scan_variables(c_source):
        try:
            located = cdb.location(entry["c"], 2)
        except SymbolTableError as exc:
            variables.append({**entry, "unlocated": str(exc)})
        else:
            variables.append({**entry, **located})

    table = {
        "fosc": fosc,
        "device": device,
        "scheduler": {
            "bw_ms": cdb.location("bw_ms", 2),
            "tasks": out_tasks,
        },
    }
    if variables:
        table["variables"] = variables
    return table


# ------------------------------------------------------------------------ CLI


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--cdb", required=True, help="the LINKED .cdb from sdcc --debug")
    ap.add_argument("--source", required=True, help="the generated C the .cdb describes")
    ap.add_argument("--fosc", type=int, default=11059200)
    ap.add_argument("--device", default="stc12c5a60s2")
    ap.add_argument("--source-name", help="file name as the compiler saw it")
    ap.add_argument("-o", "--output", help="write here instead of stdout")
    args = ap.parse_args(argv)

    try:
        table = build_symbol_table(
            open(args.cdb).read(),
            open(args.source).read(),
            fosc=args.fosc,
            device=args.device,
            source_name=args.source_name,
        )
    except SymbolTableError as exc:
        print(f"stc_symtab: {exc}", file=sys.stderr)
        return 1

    text = json.dumps(table, indent=2) + "\n"
    if args.output:
        with open(args.output, "w") as fh:
            fh.write(text)
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
