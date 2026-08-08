#!/usr/bin/env python3
"""
test-golden.py — the byte-level oracle for refactors of the emitter.

    ./scripts/test-golden.py            # compare against the committed goldens
    ./scripts/test-golden.py --update   # rewrite them (review the diff!)

test-roundtrip.py proves the front and back ends are inverses, but it compares
each program against *its own* first output, so a change that shifts every
emission equally slips straight through it. This file closes that hole: the
generated C and the canonical pseudocode for every fixture are committed as
files, so any change to what we emit shows up as a reviewable diff.

That makes it the tool for restructuring the emitter — splitting the STC12
specifics out behind a target interface has to leave the output identical byte
for byte, and this is what proves it.
"""

import importlib.util
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import stc_pseudocode as sp  # noqa: E402

# test-roundtrip.py owns the fixtures; a dash in the name keeps `import` out.
spec = importlib.util.spec_from_file_location("roundtrip", HERE / "test-roundtrip.py")
roundtrip = importlib.util.module_from_spec(spec)
sys.modules["roundtrip"] = roundtrip
_stdout = sys.stdout
try:                                    # importing it runs its own suite
    sys.stdout = open("/dev/null", "w")
    try:
        spec.loader.exec_module(roundtrip)
    except SystemExit:
        pass
finally:
    sys.stdout.close()
    sys.stdout = _stdout

GOLDEN = HERE / "golden"
UPDATE = "--update" in sys.argv

GOLDEN.mkdir(exist_ok=True)
passed = failed = 0
written = 0

for name, source in roundtrip.PROGRAMS.items():
    program = sp.parse(source)
    for suffix, text in (("c", sp.emit_c(program)),
                         ("bw", sp.emit_pseudocode(program))):
        path = GOLDEN / f"{name}.{suffix}"
        if UPDATE:
            if not path.exists() or path.read_text() != text:
                path.write_text(text)
                written += 1
            continue
        if not path.exists():
            failed += 1
            print(f"  \033[31mMISSING\033[0m  {path.name} — run with --update")
            continue
        want = path.read_text()
        if want == text:
            passed += 1
            continue
        failed += 1
        print(f"  \033[31mFAIL\033[0m  {path.name}")
        for number, (a, b) in enumerate(zip(want.splitlines(), text.splitlines()), 1):
            if a != b:
                print(f"        line {number}\n        want {a!r}\n         got {b!r}")
                break
        else:
            print(f"        length differs: {len(want.splitlines())} vs "
                  f"{len(text.splitlines())} lines")

if UPDATE:
    print(f"goldens updated: {written} file(s) written to {GOLDEN}")
    sys.exit(0)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
