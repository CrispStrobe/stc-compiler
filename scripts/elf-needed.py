#!/usr/bin/env python3
"""
elf-needed.py — list the shared libraries an ELF binary requires (DT_NEEDED).

Why this exists rather than `ldd`: the vendored AVR toolchain is Linux
x86_64 and gets built on whatever machine a developer is using, so the
question "what does cc1 dynamically link against" has to be answerable
without being able to run cc1 at all.

It is also the check that catches the failure mode CI cannot. GitHub's
Ubuntu runner has libmpc, libmpfr and libgmp installed system-wide, so a
bundle missing them passes every test there and then dies on Vercel's Amazon
Linux 2023 with

    cc1: error while loading shared libraries: libmpc.so.3

The driver itself does not need them, so even /health looks healthy. Only a
static check over the binaries finds this before production does.

    ./scripts/elf-needed.py avr/lib/gcc/avr/*/cc1 ...   # list
    ./scripts/elf-needed.py --check avr                 # audit a whole tree
"""

import pathlib
import struct
import sys

# Provided by glibc itself on any Linux host, so they are never vendored.
# Anything outside this set has to travel with the bundle.
BASE = {
    "linux-vdso.so.1", "ld-linux-x86-64.so.2",
    "libc.so.6", "libm.so.6", "libdl.so.2", "libpthread.so.0",
    "librt.so.1", "libutil.so.1", "libresolv.so.2",
}

DT_NEEDED, DT_STRTAB, DT_STRSZ, DT_NULL = 1, 5, 10, 0
PT_DYNAMIC, PT_LOAD = 2, 1


def needed(path: pathlib.Path) -> list[str]:
    """DT_NEEDED entries of an ELF64 little-endian binary; [] if not one."""
    data = path.read_bytes()
    if len(data) < 64 or data[:4] != b"\x7fELF" or data[4] != 2 or data[5] != 1:
        return []                                   # not ELF64 LE

    e_phoff, = struct.unpack_from("<Q", data, 32)
    e_phentsize, e_phnum = struct.unpack_from("<HH", data, 54)

    # Virtual address -> file offset, via the PT_LOAD segments.
    loads, dynamic = [], None
    for i in range(e_phnum):
        off = e_phoff + i * e_phentsize
        p_type, = struct.unpack_from("<I", data, off)
        p_offset, p_vaddr = struct.unpack_from("<QQ", data, off + 8)
        p_filesz, = struct.unpack_from("<Q", data, off + 32)
        if p_type == PT_LOAD:
            loads.append((p_vaddr, p_offset, p_filesz))
        elif p_type == PT_DYNAMIC:
            dynamic = (p_offset, p_filesz)
    if dynamic is None:
        return []

    def to_offset(vaddr: int) -> int | None:
        for base, off, size in loads:
            if base <= vaddr < base + size:
                return off + (vaddr - base)
        return None

    entries, strtab_va, strsz = [], None, 0
    off, size = dynamic
    for i in range(size // 16):
        tag, val = struct.unpack_from("<Qq", data, off + i * 16)
        if tag == DT_NULL:
            break
        if tag == DT_NEEDED:
            entries.append(val)                     # offset into the strtab
        elif tag == DT_STRTAB:
            strtab_va = val
        elif tag == DT_STRSZ:
            strsz = val
    if strtab_va is None:
        return []
    base = to_offset(strtab_va)
    if base is None:
        return []

    out = []
    for entry in entries:
        end = data.index(b"\0", base + entry)
        out.append(data[base + entry:end].decode("utf-8", "replace"))
    return out


args = sys.argv[1:]
if not args:
    print(__doc__.strip())
    raise SystemExit(2)

if args[0] == "--check":
    root = pathlib.Path(args[1])
    vendored = {p.name for p in root.rglob("*.so*")}
    missing: dict[str, set[str]] = {}
    scanned = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        libs = needed(path)
        if not libs:
            continue
        scanned += 1
        for lib in libs:
            if lib not in BASE and lib not in vendored:
                missing.setdefault(lib, set()).add(str(path.relative_to(root)))
    print(f"scanned {scanned} ELF binaries under {root}/")
    if missing:
        print("\nNOT vendored and not provided by glibc:")
        for lib, users in sorted(missing.items()):
            print(f"  {lib}")
            for user in sorted(users):
                print(f"      needed by {user}")
        print("\nThese resolve on a developer machine and on GitHub's runners,")
        print("and will NOT resolve on Vercel. Vendor them.")
        raise SystemExit(1)
    print("every non-glibc dependency travels with the bundle.")
    raise SystemExit(0)

for name in args:
    path = pathlib.Path(name)
    libs = needed(path)
    extra = [lib for lib in libs if lib not in BASE]
    print(f"{path}:")
    for lib in libs:
        print(f"    {lib}{'' if lib in BASE else '   <- must be vendored'}")
    if not libs:
        print("    (static, or not an ELF64 binary)")
