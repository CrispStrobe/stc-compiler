#!/usr/bin/env python3
"""
test-symtab — check stc_symtab against a real SDCC build, end to end.

Pseudocode -> C -> sdcc --debug -> .cdb -> symbol table, then assert the
result against things that are true independently of the tool under test.

Two of the assertions are the point of the whole exercise:

  * every address is cross-checked against the .map, which the LINKER writes
    and stc_symtab never reads. Agreeing with the .cdb alone would only prove
    the parser is self-consistent.

  * the address space of every entry is checked, because `iram` and `sfr`
    overlap numerically at 0x80..0xFF. A wrong space produces a plausible
    number and silently reads the wrong memory, which is exactly the failure
    a downstream emulator could not detect.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import stc_pseudocode                      # noqa: E402
import stc_symtab                          # noqa: E402

def _find_sdcc() -> str:
    """The vendored bin/sdcc is a Linux build for the hosted deploy, so on a
    developer's Mac it exists but cannot run. Probe rather than assume."""
    for candidate in (str(ROOT / "bin" / "sdcc"), "sdcc"):
        try:
            subprocess.run([candidate, "--version"], check=True, capture_output=True)
            return candidate
        except (OSError, subprocess.CalledProcessError):
            continue
    sys.exit("test-symtab: no runnable sdcc found (brew install sdcc)")


SDCC = _find_sdcc()

# Two scripts, so the emitter produces the cooperative scheduler. One task
# has a REPEAT (a counted loop yield) and one does not, so both yield shapes
# are covered.
FIXTURE = """
DEVICE STC12C5A60S2:
  CLOCK 11059200

  PIN led1 = P1.0 OUTPUT ACTIVE LOW
  PIN led2 = P1.1 OUTPUT ACTIVE LOW

  WHEN started:
    FOREVER:
      turn on led1
      wait 500 ms
      turn off led1
      wait 500 ms

  WHEN started:
    REPEAT 3:
      toggle led2
      wait 150 ms
    FOREVER:
      wait 1000 ms
      toggle led2
"""

SINGLE_TASK = """
DEVICE STC12C5A60S2:
  CLOCK 11059200
  PIN led1 = P1.0 OUTPUT ACTIVE LOW
  WHEN started:
    FOREVER:
      toggle led1
      wait 500 ms
"""

checks = 0
failures = 0


def ok(cond, what):
    global checks, failures
    checks += 1
    if not cond:
        failures += 1
        print(f"  FAIL {what}")


def build(source: str, workdir: Path) -> tuple[str, str, str]:
    """pseudocode -> C -> compiled with --debug. Returns (c, cdb, map)."""
    c_text = stc_pseudocode.emit_c(stc_pseudocode.parse(source))
    c_path = workdir / "prog.c"
    c_path.write_text(c_text)

    subprocess.run(
        [SDCC, "-mmcs51", "--std-c99", "--debug",
         "--iram-size", "256", "--xram-size", "1024", "--code-size", "61440",
         "-DFOSC_HZ=11059200UL", "-o", str(workdir) + "/", str(c_path)],
        check=True, capture_output=True,
    )
    return (c_text,
            (workdir / "prog.cdb").read_text(),
            (workdir / "prog.map").read_text())


def map_addresses(map_text: str) -> dict[str, int]:
    """Symbol -> address, straight from the linker's own map file."""
    out = {}
    for line in map_text.splitlines():
        m = re.match(r"^(?:C:)?\s+([0-9A-F]{8})\s+(?:X?)F\w+\$([^$]+)\$", line)
        if m:
            out.setdefault(m.group(2), int(m.group(1), 16))
    return out


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        workdir = Path(tmp)
        c_text, cdb_text, map_text = build(FIXTURE, workdir)
        table = stc_symtab.build_symbol_table(
            cdb_text, c_text, fosc=11059200, device="stc12c5a60s2"
        )
        from_map = map_addresses(map_text)

        print("shape")
        sched = table["scheduler"]
        ok(table["fosc"] == 11059200 and table["device"] == "stc12c5a60s2", "header")
        ok(len(sched["tasks"]) == 2, "two tasks found")
        ok([t["name"] for t in sched["tasks"]] == ["bw_task0", "bw_task1"],
           "tasks are named and ordered as the emitter names them")

        print("addresses agree with the linker's map, not just with the .cdb")
        ok(sched["bw_ms"]["addr"] == from_map.get("bw_ms"),
           f"bw_ms {sched['bw_ms']['addr']} vs map {from_map.get('bw_ms')}")
        for t in sched["tasks"]:
            for field in ("state", "until"):
                name = f"{t['name']}_{field}"
                ok(t[field]["addr"] == from_map.get(name),
                   f"{name} {t[field]['addr']} vs map {from_map.get(name)}")
            ok(t["func_addr"] == from_map.get(t["name"]),
               f"{t['name']} entry {t['func_addr']} vs map {from_map.get(t['name'])}")

        print("address spaces")
        ok(sched["bw_ms"]["space"] == "iram", "bw_ms is directly addressable IRAM")
        ok(all(t[f]["space"] == "iram" for t in sched["tasks"] for f in ("state", "until")),
           "every task variable is IRAM, not xram and not sfr")
        ok(all(t[f]["size"] == 2 for t in sched["tasks"] for f in ("state", "until")),
           "task variables are 16-bit, matching the 0xFFFF done sentinel")

        print("yield points")
        starts = sorted(t["func_addr"] for t in sched["tasks"])
        for t in sched["tasks"]:
            states = [y["state"] for y in t["yields"]]
            ok(states == sorted(states) and states == list(range(len(states))),
               f"{t['name']} states are 0..n with no gaps: {states}")
            addrs = [y["addr"] for y in t["yields"]]
            ok(addrs == sorted(addrs), f"{t['name']} yield addresses increase")
            ok(all(a > t["func_addr"] for a in addrs),
               f"{t['name']} yields are inside the function body")
            later = [s for s in starts if s > t["func_addr"]]
            if later:
                ok(all(a < later[0] for a in addrs),
                   f"{t['name']} yields do not spill into the next function")
            ok(len(set(addrs)) == len(addrs), f"{t['name']} yields are distinct")

        ok(sched["tasks"][0]["yields"][0]["label"] == "entry", "state 0 is the entry")
        ok(any(y["label"] == "wait" for y in sched["tasks"][0]["yields"]),
           "a WAIT is labelled as one")
        ok(any(y["label"] == "repeat_top" for y in sched["tasks"][1]["yields"]),
           "a REPEAT head is labelled as one")

        print("the @bw yield map: block ids, and the refusal when it has drifted")
        # sb3-creator's generateC(project, {debug: true}) writes this header; the
        # emitter in THIS repo does not, because pseudocode has no blocks. So the
        # fixture is built from the C's own case labels — what is under test here
        # is the merge and the drift refusal, not the other repo's emitter.
        ok(all("block" not in y for t in sched["tasks"] for y in t["yields"]),
           "headerless C still produces a table, just without block ids")

        # An id containing `*/` is the case the percent-encoding exists for: it
        # would otherwise close the comment the header lives in.
        def synth_header(cases):
            lines = ["/* @bw-begin", " * @bw device stc12c5a60s2"]
            for task, state in cases:
                raw = f"a*/b-{task}-{state}" if state == 0 else f"blk({task}/{state})"
                enc = quote(raw, safe="").replace("*", "%2A")
                lines.append(f" * @bw yield {task} {state} {enc} yielded")
            lines.append(" * @bw-end */")
            return "\n".join(lines) + "\n"

        # Appended rather than prepended: the header's position is irrelevant to the
        # scanner, and appending leaves every line number — and so every .cdb line
        # record — exactly where it was. The "addresses are unchanged" check below
        # is then testing the merge rather than a line shift.
        real_cases = [(t["name"], y["state"]) for t in sched["tasks"] for y in t["yields"]]
        with_header = c_text + synth_header(real_cases)
        merged = stc_symtab.build_symbol_table(
            cdb_text, with_header, fosc=11059200, device="stc12c5a60s2"
        )
        got = [(t["name"], y["state"], y.get("block"))
               for t in merged["scheduler"]["tasks"] for y in t["yields"]]
        want = [(task, state,
                 f"a*/b-{task}-{state}" if state == 0 else f"blk({task}/{state})")
                for task, state in real_cases]
        ok(sorted(got) == sorted(want), "every yield carries its block id, decoded exactly")
        ok(any("*/" in (b or "") for _, _, b in got),
           "including one that would have closed the C comment")
        # The addresses must not have moved: adding a header is metadata only.
        ok([y["addr"] for t in merged["scheduler"]["tasks"] for y in t["yields"]]
           == [y["addr"] for t in sched["tasks"] for y in t["yields"]],
           "and the code addresses are unchanged")

        print("  a map that disagrees with the case labels is refused")
        for label, cases in (
            ("a state the source does not have", real_cases + [("bw_task0", 99)]),
            ("a state the header is missing", real_cases[:-1]),
            ("a task that is not in the source", real_cases + [("bw_task9", 0)]),
        ):
            try:
                stc_symtab.build_symbol_table(
                    cdb_text, c_text + synth_header(cases),
                    fosc=11059200, device="stc12c5a60s2",
                )
                ok(False, f"should have raised: {label}")
            except stc_symtab.SymbolTableError as exc:
                ok("disagrees with the case labels" in str(exc), f"refused: {label}")

        print("the single-task case refuses rather than emitting an empty table")
        c2, cdb2, _ = build(SINGLE_TASK, workdir / "single" if False else workdir)
        try:
            stc_symtab.build_symbol_table(cdb2, c2, fosc=11059200, device="x")
            ok(False, "should have raised for a single-WHEN program")
        except stc_symtab.SymbolTableError as exc:
            ok("single-WHEN" in str(exc), f"explains why: {exc}")

        print("an unlinked .cdb is diagnosed, not silently mis-parsed")
        stripped = "\n".join(l for l in cdb_text.splitlines() if not l.startswith("L:"))
        try:
            stc_symtab.build_symbol_table(stripped, c_text, fosc=1, device="x")
            ok(False, "should have raised without L: records")
        except stc_symtab.SymbolTableError as exc:
            ok("--debug" in str(exc) and "L:" in str(exc), f"explains why: {exc}")

    print(f"\n{checks} checks, {failures} failures")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
