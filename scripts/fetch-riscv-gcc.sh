#!/usr/bin/env bash
#
# fetch-riscv-gcc.sh — build the vendored RISC-V bundle in riscv-gcc/.
#
# Same trick as fetch-arm-gcc.sh / fetch-avr-gcc.sh: lift already-built
# binaries from Debian's .deb packages. Unlike the ARM bundle (which is
# freestanding, no libc), this one carries a REAL C library — picolibc — so
# the riscv32-gcc target compiles ARBITRARY C: printf with floats, malloc,
# qsort, string.h, math.h. It is the heavyweight companion to the browser's
# shecc-wasm subset (riscv/riscv-cc.wasm), for programs shecc cannot handle.
#
# The output is a freestanding rv32imac/ilp32 ELF32 an emulated RV32IM machine
# boots over its ECALL console (a7=64 write, a7=93 exit) — there is no hardware
# to flash. The tiny startup + console live in riscv-gcc/runtime/ (a flat crt0,
# not picolibc's flash->ram crt0, because the machine loads a flat image).
#
# What this deliberately does NOT keep:
#   - every multilib but rv32imac/ilp32 (the ~40 others are ~180 MB)
#   - cc1plus (C++), lto1, gfortran
#   - picolibc's own crt0/linker script (we ship our own flat runtime)
#
# Size: full install is ~440 MB (gcc all-multilibs + picolibc all-multilibs).
# Trimmed to one multilib and DWARF-stripped it lands near ~40 MB.
#
# Run from the repo root:  ./scripts/fetch-riscv-gcc.sh
#
set -euo pipefail

# Bullseye for GLIBC compatibility with Vercel's Amazon Linux 2023 (glibc 2.34).
# Same constraint as the ARM/AVR bundles: newer suites' builds need GLIBC_2.36.
SUITE="bullseye"
POOL="https://deb.debian.org/debian/pool/main"

GCC_RV="gcc-riscv64-unknown-elf_8.3.0.2019.08+dfsg-1_amd64.deb"
BINUTILS_RV="binutils-riscv64-unknown-elf_2.32.2020.04+dfsg-2_amd64.deb"
PICOLIBC_RV="picolibc-riscv64-unknown-elf_1.5.1-2_all.deb"

# gcc 8.3 links the same math libraries as the ARM 8.3.1 bundle (including
# libisl — cc1 dies without libisl.so.23) PLUS libstdc++/libgcc_s: this Debian
# riscv cc1 links the C++ runtime DYNAMICALLY (the ARM cc1 statics it), so
# elf-needed flags them as missing unless they travel here too.
RUNTIME_LIBS="
  g/gmp/libgmp10_6.2.1+dfsg-1+deb11u1_amd64.deb
  m/mpclib3/libmpc3_1.2.0-1_amd64.deb
  m/mpfr4/libmpfr6_4.1.0-3_amd64.deb
  z/zlib/zlib1g_1.2.11.dfsg-2+deb11u2_amd64.deb
  i/isl/libisl23_0.23-1_amd64.deb
  g/gcc-10/libstdc++6_10.2.1-6_amd64.deb
  g/gcc-10/libgcc-s1_10.2.1-6_amd64.deb
"

# The emulated machine is RV32IMA; picolibc + gcc ship an rv32imac/ilp32
# multilib (imac is a superset of ima the machine runs). ilp32 = soft-float ABI.
MULTILIB="rv32imac/ilp32"
GCC_MARCH="rv32imac"
GCC_MABI="ilp32"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# Large scratch off the small root fs.
WORK="$(mktemp -d "${TMPDIR:-/tmp}/riscv-gcc.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }

say "Downloading the RISC-V toolchain from Debian ${SUITE} ..."
declare -A DIRS=(
  [gcc-riscv64-unknown-elf]="g/gcc-riscv64-unknown-elf"
  [binutils-riscv64-unknown-elf]="b/binutils-riscv64-unknown-elf"
  [picolibc-riscv64-unknown-elf]="p/picolibc"
)
for pkg in "$GCC_RV" "$BINUTILS_RV" "$PICOLIBC_RV"; do
  base="${pkg%%_*}"; dir="${DIRS[$base]}"
  say "  $pkg"
  curl -fsSL --max-time 600 -o "$WORK/$pkg" "$POOL/$dir/$pkg"
done
for path in $RUNTIME_LIBS; do
  say "  $(basename "$path")"
  curl -fsSL --max-time 600 -o "$WORK/$(basename "$path")" "$POOL/$path"
done

say "Unpacking ..."
mkdir -p "$WORK/x"
for deb in "$WORK"/*.deb; do
  ar p "$deb" data.tar.xz 2>/dev/null | tar -xJ -C "$WORK/x" \
    || ar p "$deb" data.tar.zst | tar --zstd -x -C "$WORK/x"
done

GCCLIB="$WORK/x/usr/lib/gcc/riscv64-unknown-elf"
test -d "$GCCLIB" || { echo "gcc package has no lib/gcc/riscv64-unknown-elf" >&2; exit 1; }
VERSION="$(ls "$GCCLIB" | head -1)"
SRC="$GCCLIB/$VERSION"

say "Assembling the ${MULTILIB}-only bundle (gcc ${VERSION}) ..."
rm -rf "$ROOT/riscv-gcc"
mkdir -p "$ROOT/riscv-gcc/bin" "$ROOT/riscv-gcc/lib"

# Prefixed tools app.py / the verifier invoke by name.
for b in riscv64-unknown-elf-gcc riscv64-unknown-elf-objcopy riscv64-unknown-elf-objdump riscv64-unknown-elf-size; do
  if [ -f "$WORK/x/usr/bin/$b" ]; then
    cp "$WORK/x/usr/bin/$b" "$ROOT/riscv-gcc/bin/$b"; chmod +x "$ROOT/riscv-gcc/bin/$b"
  else echo "  WARNING: $b not found" >&2; fi
done

# Debian configures gcc with --prefix=/usr/lib, so the driver's tooldir is
# riscv-gcc/lib/riscv64-unknown-elf/bin (NOT riscv-gcc/riscv64-unknown-elf/bin).
# Put the unprefixed binutils there; symlink the standard layout too. Same
# gotcha the ARM bundle documents at length.
mkdir -p "$ROOT/riscv-gcc/lib/riscv64-unknown-elf/bin"
for b in as ld ar ranlib objcopy objdump nm strip; do
  src="$WORK/x/usr/lib/riscv64-unknown-elf/bin/$b"
  if [ -f "$src" ]; then
    cp "$src" "$ROOT/riscv-gcc/lib/riscv64-unknown-elf/bin/$b"
    chmod +x "$ROOT/riscv-gcc/lib/riscv64-unknown-elf/bin/$b"
  elif [ -f "$WORK/x/usr/bin/riscv64-unknown-elf-$b" ]; then
    cp "$WORK/x/usr/bin/riscv64-unknown-elf-$b" "$ROOT/riscv-gcc/lib/riscv64-unknown-elf/bin/$b"
    chmod +x "$ROOT/riscv-gcc/lib/riscv64-unknown-elf/bin/$b"
  fi
done
ln -sfn lib/riscv64-unknown-elf "$ROOT/riscv-gcc/riscv64-unknown-elf"

DST="$ROOT/riscv-gcc/lib/gcc/riscv64-unknown-elf/$VERSION"
mkdir -p "$DST"
for f in cc1 collect2; do
  test -f "$SRC/$f" || { echo "missing $f in gcc package" >&2; exit 1; }
  cp "$SRC/$f" "$DST/$f"; chmod +x "$DST/$f"
done
cp -a "$SRC"/liblto_plugin.so* "$DST/" 2>/dev/null || true
test -f "$SRC/lto-wrapper" && cp "$SRC/lto-wrapper" "$DST/lto-wrapper" && chmod +x "$DST/lto-wrapper"
cp -R "$SRC/include" "$DST/include"
test -d "$SRC/include-fixed" && cp -R "$SRC/include-fixed" "$DST/include-fixed"
# The one multilib: libgcc.a + crt objects for rv32imac/ilp32.
if [ -d "$SRC/$MULTILIB" ]; then
  mkdir -p "$DST/$MULTILIB"; cp -R "$SRC/$MULTILIB"/* "$DST/$MULTILIB/"
else echo "  ERROR: multilib $MULTILIB not in gcc package" >&2; exit 1; fi

# picolibc: the C library. Keep the rv32imac/ilp32 libc.a + libm.a and the
# headers. NOT picolibc's crt0 or linker script — riscv-gcc/runtime/ ships a
# flat crt0 + linker script the emulated machine boots directly.
PICO="$WORK/x/usr/lib/picolibc/riscv64-unknown-elf"
test -d "$PICO" || { echo "picolibc package layout unexpected" >&2; exit 1; }
mkdir -p "$ROOT/riscv-gcc/picolibc/lib" "$ROOT/riscv-gcc/picolibc/include"
# picolibc lays libs under lib/[release/]<multilib>; find the rv32imac/ilp32 one.
PICOLIB="$(dirname "$(find "$PICO/lib" -path "*$MULTILIB/libc.a" | head -1)")"
test -n "$PICOLIB" || { echo "no picolibc libc.a for $MULTILIB" >&2; exit 1; }
cp "$PICOLIB"/libc.a "$PICOLIB"/libm.a "$ROOT/riscv-gcc/picolibc/lib/" 2>/dev/null
cp -R "$PICO/include/"* "$ROOT/riscv-gcc/picolibc/include/"

say "Stripping DWARF from the static libraries ..."
STRIP="$ROOT/riscv-gcc/lib/riscv64-unknown-elf/bin/strip"
[ -x "$STRIP" ] || STRIP="$(command -v strip || true)"
if [ -n "$STRIP" ]; then
  before=$(du -sk "$ROOT/riscv-gcc" | cut -f1)
  find "$ROOT/riscv-gcc" -name '*.a' -exec "$STRIP" --strip-debug {} + 2>/dev/null || true
  # Strip the executables too (cc1/collect2/binutils carry debug info).
  find "$ROOT/riscv-gcc/bin" "$ROOT/riscv-gcc/lib/riscv64-unknown-elf/bin" \
       "$DST/cc1" "$DST/collect2" -type f -exec "$STRIP" {} + 2>/dev/null || true
  after=$(du -sk "$ROOT/riscv-gcc" | cut -f1)
  echo "  ${before} KB -> ${after} KB"
fi

# The shared libraries cc1 + binutils need at runtime.
mkdir -p "$ROOT/riscv-gcc/lib-deps"
for so in "$WORK"/x/usr/lib/x86_64-linux-gnu/lib*.so.* "$WORK"/x/lib/x86_64-linux-gnu/lib*.so.*; do
  test -e "$so" && cp -a "$so" "$ROOT/riscv-gcc/lib-deps/"
done

# GPL section 1: licences travel with the binaries.
mkdir -p "$ROOT/vendor/riscv-gcc"
for p in gcc-riscv64-unknown-elf binutils-riscv64-unknown-elf picolibc-riscv64-unknown-elf; do
  cp "$WORK/x/usr/share/doc/$p/copyright" "$ROOT/vendor/riscv-gcc/$p.copyright" 2>/dev/null || true
done
cat > "$ROOT/vendor/riscv-gcc/VERSION" <<EOF
gcc-riscv64-unknown-elf       ${GCC_RV}
binutils-riscv64-unknown-elf  ${BINUTILS_RV}
picolibc-riscv64-unknown-elf  ${PICOLIBC_RV}

from Debian ${SUITE}, ${POOL}
corresponding source: apt-get source gcc-riscv64-unknown-elf binutils-riscv64-unknown-elf picolibc

gcc and binutils are GPL-3.0-or-later; libgcc rides under the GCC Runtime
Library Exception. picolibc is BSD (2/3-clause + a few files under other
permissive terms) — see picolibc-riscv64-unknown-elf.copyright. The bundle is a
SUBSET (one multilib, DWARF stripped), not a modification: no binary is patched.
EOF

say "Done. Bundle at riscv-gcc/ ($(du -sh "$ROOT/riscv-gcc" | cut -f1))."
say "Build flags: -march=${GCC_MARCH} -mabi=${GCC_MABI}"
