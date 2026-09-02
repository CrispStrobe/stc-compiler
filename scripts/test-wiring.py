#!/usr/bin/env python3
"""
test-wiring — every test runs in CI, or says here why it does not.

A test that is written, committed, green, and never executed is worse than no
test: it reads as coverage in the file listing and costs nothing to keep, so
nobody looks at it again. This file found exactly that on 2026-08-09 --
`test-wait-floor.py` had been added that morning, passed locally, and was
absent from the workflow.

Then on 2026-09-02 it missed a much larger instance of the same thing, for a
reason worth writing down: it only looked in `scripts/`. The seventeen
`test_*.py` files at the repository root -- 380 tests, the whole dialect, every
PART, the Arcade back end, the assembler chains -- had never run in CI at all.
A guard with a blind spot is not a weaker guard, it is a guard that certifies
the blind spot as covered. So it now checks BOTH shapes of test this project
has:

  scripts/test-*.py   standalone suites, each named in the workflow
  ./test_*.py         a pytest suite, collected as a group by one step

The two need different checks. A standalone suite is wired if its filename
appears in the workflow. A pytest file is wired if some step actually runs
pytest over a set of paths that includes it -- which is checked by expanding
the step's own arguments, not by trusting that `pytest` appearing anywhere
means this file is collected.

The rule is deliberately not "every test file must be in the workflow",
because three of them genuinely cannot be:

  - test-api.py         talks to the deployed service. CI has no deployment,
                        and pointing it at production would make every push a
                        live probe of someone else's uptime.
  - test-disasm.py      needs a built image from the LAB repo (../stc/build),
  - test-reassemble.py  which is a different repository and a real toolchain.

Those are exclusions with reasons, and the reasons are checked: an excluded
file must still exist, so the list cannot rot into a rule that excuses a test
someone deleted. Anything not on the list must appear in the workflow.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"

# Excluded on purpose -- name -> why. Adding a name here is a decision that
# has to be written down, which is the entire mechanism.
EXCLUDED = {
    "test-api.py":
        "talks to the deployed service; CI has no deployment to talk to",
    "test-disasm.py":
        "needs a built image from the lab repo (../stc/build) and SDCC",
    "test-reassemble.py":
        "needs a built image from the lab repo and a real assembler",
}

checks = failures = 0


def ok(cond, label, detail=""):
    global checks, failures
    checks += 1
    if not cond:
        failures += 1
    mark = "\x1b[32mok \x1b[0m" if cond else "\x1b[31mFAIL\x1b[0m"
    print(f"  {mark} {label}" + (f"   {detail}" if detail else ""))


workflows = list((ROOT / ".github" / "workflows").glob("*.yml"))
ok(bool(workflows), "there is a workflow to check",
   ", ".join(w.name for w in workflows))
text = "\n".join(w.read_text() for w in workflows)

tests = sorted(p.name for p in SCRIPTS.iterdir()
               if p.name.startswith(("test-", "check-"))
               and p.suffix in (".py", ".mjs", ".js")
               and p.name != Path(__file__).name)
ok(bool(tests), f"there are tests to check", f"{len(tests)} files")

print("\nevery test is run by CI, or excluded with a reason")
for name in tests:
    if name in EXCLUDED:
        print(f"  \x1b[33m--  \x1b[0m {name}   excluded: {EXCLUDED[name]}")
        continue
    ok(name in text, f"{name} is run by the workflow",
       "" if name in text else
       "written, committed, and never executed -- add it to .github/workflows/")

print("\nthe exclusion list has not rotted")
for name, why in EXCLUDED.items():
    ok((SCRIPTS / name).exists(), f"{name} still exists to be excluded")
    ok(bool(why.strip()), f"{name} says why it is excluded")
    # An excluded test that quietly got wired anyway means the reason is
    # stale -- either the obstacle went away, or CI is doing something it
    # was not meant to.
    ok(name not in text,
       f"{name} is not secretly wired as well",
       "" if name not in text else
       f"it runs in CI but is listed as excluded because it {why}")

# ---------------------------------------------------------------- pytest side
#
# The root suite is not run file-by-file, so "the name appears in the
# workflow" is the wrong question. The right one is whether any step's pytest
# invocation would COLLECT each file -- so the step's own arguments are
# expanded as globs from the repository root and compared against what is
# actually there.

import re

PYTEST_FILES = sorted(p.name for p in ROOT.glob("test_*.py"))

print("\nthe root pytest suite is collected by CI")
ok(bool(PYTEST_FILES), "there are root pytest files to check",
   f"{len(PYTEST_FILES)} files")

collected: set[str] = set()
steps = re.findall(r"^\s*run:\s*(.+?)$", text, re.M)
steps += re.findall(r"^\s*- run:\s*(.+?)$", text, re.M)
for command in steps:
    if "pytest" not in command:
        continue
    # Everything after the pytest invocation that is not a flag is a path.
    tail = command.split("pytest", 1)[1].split()
    args = [a for a in tail if not a.startswith("-")]
    if not args:
        args = ["."]            # bare `pytest` collects the whole tree
    for arg in args:
        for found in ROOT.glob(arg):
            if found.name.startswith("test_") and found.suffix == ".py":
                collected.add(found.name)

ok(bool(collected), "some CI step runs pytest over the root suite",
   "" if collected else
   "no `run:` step invokes pytest -- the root test_*.py files never execute")

for name in PYTEST_FILES:
    ok(name in collected, f"{name} is collected by that step",
       "" if name in collected else
       "written, committed, and never executed -- widen the pytest step's paths")

print(f"\n  {checks - failures}/{checks} checks passed")
sys.exit(1 if failures else 0)
