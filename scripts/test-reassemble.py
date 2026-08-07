#!/usr/bin/env python3
"""
test-reassemble.py — prove the disassembly is correct by assembling it back.

The strongest check available: take an image, disassemble it, feed that text to
`sdas8051` (an independent implementation, shipped with SDCC), and compare the
bytes it produces against the bytes we started with. If they match, the
disassembly is not merely plausible — it is exactly what those bytes mean.

    ./scripts/test-reassemble.py <file.hex> [more.hex ...]

Reports per file: identical / differing byte count / assembler errors.
"""

import pathlib
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import stc_disasm  # noqa: E402

SDAS = "sdas8051"
SDLD = "sdld"


def to_asm(entries, memory) -> str:
    """ASxxxx source for a run of instructions.

    Branch targets are emitted as *labels*, not bare addresses. Handing the
    assembler a number invites it to interpret the value in its own frame of
    reference; a label it resolves itself, which is both unambiguous and what
    a disassembly should look like anyway.
    """
    starts = {address for address, _, _ in entries}
    targets = set()
    for _, _, text in entries:
        for value in re.findall(r"0x([0-9A-F]{4})\b", text):
            targets.add(int(value, 16))
    targets &= starts                      # only label real instruction starts

    lines = [".area CODE (ABS)"]
    expected = None
    for address, raw, text in entries:
        if expected is None or address != expected:
            lines.append(f"\t.org 0x{address:04X}")
        if address in targets:
            lines.append(f"L{address:04X}:")
        text = re.sub(r"0x([0-9A-F]{4})\b",
                      lambda m: (f"L{int(m.group(1), 16):04X}"
                                 if int(m.group(1), 16) in targets else m.group(0)),
                      text)
        if text.startswith(".db"):
            lines.append("\t" + text.lower())
        else:
            mnemonic, _, operands = text.partition(" ")
            operands = operands.strip()
            lines.append(f"\t{mnemonic.lower()}\t{operands}" if operands
                         else f"\t{mnemonic.lower()}")
        expected = address + len(raw)
    return "\n".join(lines) + "\n"


def assemble(asm_text: str, work: pathlib.Path):
    """Assemble AND link, returning {address: byte} or (None, error).

    Linking is not optional: sdas8051 leaves relative branch displacements
    unresolved in the listing (they show up flagged, e.g. `30 8Dp05`), so the
    only place the final bytes exist is the linked image.
    """
    source = work / "r.asm"
    source.write_text(asm_text)
    result = subprocess.run([SDAS, "-losgff", str(source)], capture_output=True,
                            text=True, cwd=work, timeout=180)
    if result.returncode != 0:
        return None, (result.stdout + result.stderr)[:400]

    linked = subprocess.run([SDLD, "-n", "-i", "out", "r.rel"], capture_output=True,
                            text=True, cwd=work, timeout=180)
    image = work / "out.ihx"
    if linked.returncode != 0 or not image.exists():
        return None, (linked.stdout + linked.stderr)[:400] or "link produced nothing"
    return stc_disasm.parse_hex(image.read_text()), None


def main() -> int:
    files = [pathlib.Path(a) for a in sys.argv[1:]]
    if not files:
        print(__doc__)
        return 2

    stc_disasm.SYMBOLIC = False        # the assembler wants numbers, not names
    identical = differing = errored = 0

    for path in files:
        name = path.name.split("__", 1)[-1][:52]
        try:
            memory = stc_disasm.parse_hex(path.read_text(errors="replace"))
        except ValueError as exc:
            print(f"  \033[33mSKIP\033[0m {name}: {exc}")
            continue
        entries = stc_disasm.disassemble(memory)
        if not entries:
            continue

        with tempfile.TemporaryDirectory() as tmp:
            work = pathlib.Path(tmp)
            rebuilt, error = assemble(to_asm(entries, memory), work)

        if rebuilt is None:
            errored += 1
            first = error.strip().splitlines()[0] if error.strip() else "?"
            print(f"  \033[31mASM \033[0m {name}: {first[:70]}")
            continue

        mismatches = [a for a, v in memory.items() if rebuilt.get(a) != v]
        if mismatches:
            differing += 1
            spot = min(mismatches)
            print(f"  \033[31mDIFF\033[0m {name}: {len(mismatches)} byte(s), "
                  f"first at 0x{spot:04X} "
                  f"(was 0x{memory[spot]:02X}, rebuilt "
                  f"{'0x%02X' % rebuilt[spot] if spot in rebuilt else 'absent'})")
        else:
            identical += 1
            print(f"  \033[32mOK  \033[0m {name}: {len(memory)} bytes reproduced exactly")

    print(f"\n{identical} identical, {differing} differing, {errored} assembler errors")
    return 1 if (differing or errored) else 0


if __name__ == "__main__":
    sys.exit(main())
