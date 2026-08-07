#!/usr/bin/env python3
"""
test-disasm.py — check the disassembler against SDCC's own assembler.

A disassembler you wrote yourself is only worth as much as its oracle. This
one uses SDCC's relocated listing (`.rst`), which pairs every emitted byte with
the mnemonic sdas8051 assembled it from. If our table decodes the same bytes to
a different instruction, or gets a length wrong and desynchronises, this fails.

    ./scripts/test-disasm.py <build-dir>

where <build-dir> holds main.ihx and main.rst, e.g. from stc12c5a60s2-lab:

    make && ./scripts/test-disasm.py ../stc/build/01-blink
"""

import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import stc_disasm  # noqa: E402

# `      0000B7 75 8A 67         [24]  512 \tmov\t_TL0,#0x67`
RST_RE = re.compile(
    r"^\s+([0-9A-F]{6})\s+((?:[0-9A-F]{2}\s)+)\s*(?:\[[^\]]*\])?\s*\d+\s+(\S+)(?:\s+(.*))?$")


def read_listing(path: pathlib.Path):
    """(address, bytes, mnemonic) for every real instruction in a .rst."""
    out = []
    for line in path.read_text(errors="replace").splitlines():
        match = RST_RE.match(line)
        if not match:
            continue
        mnemonic = match.group(3).lower()
        # Skip assembler directives and label-only lines.
        if mnemonic.startswith(".") or mnemonic.endswith(":"):
            continue
        raw = bytes.fromhex(match.group(2).replace(" ", ""))
        if not raw:
            continue
        out.append((int(match.group(1), 16), raw, mnemonic))
    return out


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    build = pathlib.Path(sys.argv[1])
    ihx, rst = build / "main.ihx", build / "main.rst"
    for path in (ihx, rst):
        if not path.exists():
            print(f"missing {path}")
            return 2

    expected = read_listing(rst)
    memory = stc_disasm.parse_hex(ihx.read_text())
    ours = {address: (raw, text) for address, raw, text in stc_disasm.disassemble(memory)}

    # The listing covers library code linked in from elsewhere too; only compare
    # instructions that actually made it into the image.
    checked = skipped = 0
    failures = []
    for address, raw, mnemonic in expected:
        if address not in memory:
            skipped += 1
            continue
        if address not in ours:
            failures.append(f"{address:04X}  we did not decode an instruction here "
                            f"(SDCC says {mnemonic}); a preceding length is wrong")
            continue
        our_raw, our_text = ours[address]
        our_mnemonic = our_text.split()[0].lower()
        if our_raw != raw:
            failures.append(f"{address:04X}  bytes differ: SDCC {raw.hex(' ')} "
                            f"vs ours {our_raw.hex(' ')} ({our_text})")
        elif our_mnemonic != mnemonic:
            failures.append(f"{address:04X}  {raw.hex(' '):<11} SDCC says {mnemonic!r}, "
                            f"we say {our_mnemonic!r}  ({our_text})")
        checked += 1

    print(f"{build}")
    print(f"  {len(memory)} bytes of image, {checked} instructions compared "
          f"({skipped} listing entries outside the image)")
    if failures:
        print(f"  \033[31m{len(failures)} mismatch(es)\033[0m")
        for failure in failures[:20]:
            print("    " + failure)
        return 1
    print("  \033[32mevery instruction agrees with SDCC's assembler\033[0m")

    # Coverage note: which opcodes did this image actually exercise?
    opcodes = {memory[a] for a, _, _ in stc_disasm.disassemble(memory) if a in memory}
    print(f"  {len(opcodes)} distinct opcodes exercised")
    return 0


if __name__ == "__main__":
    sys.exit(main())
