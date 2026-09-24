"""xz-compressed compiler binaries in a vendored toolchain bundle.

Vercel refuses a function bigger than 225 MB -- the whole deployment, Python
dependencies included -- and the AVR bundle's three compilers proper (cc1,
cc1plus for the Arduino route's C++, lto1 for its -flto link) are 37 MB of it.
Committed as .xz they are 13 MB. The first attempt to deploy the Arduino C++
route without this measured 235.56 MB and was refused.

Nothing runs from the deployment directory anyway: stage_avr() copies the
bundle to /tmp on a cold start, and `materialize` decompresses there (about
0.3 s per binary). The committed form is what `compress` leaves.

    python3 bundle_xz.py compress <dir> cc1 cc1plus lto1
    python3 bundle_xz.py materialize <tree>

`materialize` walks a whole tree, so CI can turn a fresh checkout into a
runnable bundle with one command before any check that executes or inspects
a binary.
"""

from __future__ import annotations

import lzma
import os
import stat
import sys

SUFFIX = ".xz"


def compress(directory: str, names: list[str]) -> list[str]:
    """Replace each named file in `directory` with <name>.xz. Returns the
    files written. A name already compressed is left as it is."""
    done = []
    for name in names:
        src = os.path.join(directory, name)
        if not os.path.exists(src):
            if os.path.exists(src + SUFFIX):
                continue
            raise FileNotFoundError(src)
        with open(src, "rb") as fh:
            data = fh.read()
        with open(src + SUFFIX, "wb") as fh:
            fh.write(lzma.compress(data, preset=9 | lzma.PRESET_EXTREME))
        os.remove(src)
        done.append(src + SUFFIX)
    return done


def materialize(tree: str) -> list[str]:
    """Decompress every *.xz under `tree` next to itself, executable, and
    remove the .xz. Returns the files written. Idempotent."""
    done = []
    for root, _dirs, files in os.walk(tree):
        for f in files:
            if not f.endswith(SUFFIX):
                continue
            packed = os.path.join(root, f)
            out = packed[:-len(SUFFIX)]
            with open(packed, "rb") as fh:
                data = lzma.decompress(fh.read())
            tmp = out + ".part"
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.chmod(tmp, os.stat(tmp).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
            os.replace(tmp, out)
            os.remove(packed)
            done.append(out)
    return done


def logical_names(names) -> set[str]:
    """File names as they will be once materialized (the .xz dropped)."""
    return {n[:-len(SUFFIX)] if n.endswith(SUFFIX) else n for n in names}


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "compress":
        for path in compress(sys.argv[2], sys.argv[3:]):
            print(f"compressed {path}")
    elif len(sys.argv) == 3 and sys.argv[1] == "materialize":
        for path in materialize(sys.argv[2]):
            print(f"materialized {path}")
    else:
        sys.exit(__doc__)
