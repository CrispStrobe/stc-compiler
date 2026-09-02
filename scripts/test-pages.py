#!/usr/bin/env python3
"""
test-pages.py — the GitHub Pages copy is the same transpiler, still.

docs/ has to contain its own copy of the Python modules: Pages serves that
directory and nothing above it, and Jekyll does not follow symlinks. A copy is
a fork waiting to happen, so this fails the moment the two diverge.

The point of the page is that it runs stc_pseudocode.py itself rather than a
JavaScript reimplementation. A stale copy would quietly undo that.
"""

import hashlib
import pathlib
import re
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
MIRRORED = ["stc_pseudocode.py", "bw_micropython.py", "bw_arcade.py"]

passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  \033[32mok \033[0m {name} {detail}")
    else:
        failed += 1
        print(f"  \033[31mFAIL\033[0m {name} {detail}")
    return bool(ok)


print("github pages\n")

for name in MIRRORED:
    original, copy = ROOT / name, DOCS / name
    if check(f"{name} is present in docs/", copy.exists()):
        a = hashlib.sha256(original.read_bytes()).hexdigest()
        b = hashlib.sha256(copy.read_bytes()).hexdigest()
        check(f"{name} matches the original", a == b,
              "" if a == b else "run: cp %s docs/" % name)

index = DOCS / "index.html"
check("index.html exists", index.exists())
if index.exists():
    page = index.read_text()
    for name in MIRRORED:
        check(f"the page fetches {name}", f"'{name}'" in page or f'"{name}"' in page)
    check("Pyodide is pinned to a version, not 'latest'",
          "cdn.jsdelivr.net/pyodide/v" in page and "/latest/" not in page)
    check("the transpiler runs in the page, not on a server",
          "bw_transpile" in page and "loadPyodide" in page)
    # Does the page's script actually parse?
    #
    # The Python that runs in Pyodide is embedded in a JavaScript template
    # literal, so a single backtick anywhere in it — in a comment, in a
    # docstring, quoting an identifier out of habit — ends the string and takes
    # the whole module with it. The page then loads, renders, and does nothing
    # at all, because no script ran.
    #
    # CI did catch that, via the browser job, as a 180-second waitForFunction
    # timeout whose message was "Timeout 180000ms exceeded" and named no cause.
    # `node --check` says "SyntaxError: Unexpected identifier 'device'" in about
    # forty milliseconds. Same defect, and one of the two messages tells you
    # what to fix.
    module = re.search(r"<script type=module>(.*?)</script>", page, re.S)
    check("the page has a module script at all", bool(module))
    if module:
        with tempfile.NamedTemporaryFile("w", suffix=".mjs", delete=False) as fh:
            fh.write(module.group(1))
            probe = fh.name
        result = subprocess.run([node_exe := "node", "--check", probe],
                                capture_output=True, text=True)
        detail = ""
        if result.returncode:
            detail = next((l for l in result.stderr.splitlines()
                           if "Error" in l), result.stderr.strip()[:120])
        check("the page's JavaScript parses", result.returncode == 0, detail)
        pathlib.Path(probe).unlink(missing_ok=True)

        # There was a second check here, scanning the embedded Python for stray
        # backticks. It went, because it could never fire: a "balanced" pair
        # inside the Python is not balanced at all — the first one ends the
        # template literal and everything after it is parsed as JavaScript, so
        # the syntax check above already fails, and with a better message.
        # Tested by injecting a pair rather than assumed. A check that cannot
        # fail still reads as coverage in the pass count.

    check("compiling is delegated, and says where",
          "/compile" in page and "stc-compiler.vercel.app" in page)
    # …and says WHICH compiler, because it is not the one the repo uses. The
    # hosted service pins SDCC 4.0.0 (its host is glibc 2.34; 4.5.0 needs 2.36)
    # while stc12c5a60s2-lab builds with 4.5.0. Measured on 01-blink, same C and
    # same flags: 996 bytes from 4.5.0, 888 from 4.0.0. A user comparing the
    # page's .hex against their own build deserves to know why they differ.
    check("the image says which SDCC built it",
          "apiSdcc" in page and "built by SDCC" in page)
    # Only the COMPILE is delegated. Transpiling happens here, once, and the
    # image must be built from the C the page is showing — otherwise the server
    # transpiles a second time with its own copy of the front end, and the two
    # agree only for as long as the two deployments are in step. They were fifty
    # commits apart for a day on 2026-08-09.
    check("the image is built from the C on screen, not from the pseudocode again",
          "language: 'c'" in page and "code: result.code" in page
          and "language: 'pseudocode'" not in page)
    # …which needs the canonical device token, not the display name: "Arduino
    # Uno" is not "arduino-uno" and would be rejected.
    check("the compile request carries the target key and the clock",
          '"target": target.key' in page and '"clock": program.clock' in page
          and "target: result.target" in page and "fosc: result.clock" in page)
    check("the flasher is wired in", "flash.js" in page and "id=flash" in page)
    check("all three flash paths reach the button",
          all(t in page for t in ("flashAvr", "flashMicroPython", "flashStc")))
    check("the STC cold power-on is told to the user, not assumed",
          "COLD power-on" in page)
    check("Web Serial absence is explained rather than left as a dead button",
          "'serial' in navigator" in page)
    check("the page notices when the hosted compiler is older than it is",
          "checkApiAge" in page and "/health" in page)
check("flash.js is present", (DOCS / "flash.js").exists())
check(".nojekyll is present (Jekyll would drop dotfiles and mangle paths)",
      (DOCS / ".nojekyll").exists())

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
