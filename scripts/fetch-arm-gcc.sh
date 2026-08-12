#!/usr/bin/env bash
#
# fetch-arm-gcc.sh — build the vendored ARM bundle in arm/.
#
# Same trick as fetch-avr-gcc.sh: lift already-built binaries from Debian's
# .deb packages. The target is bare-metal Cortex-M0+ (RP2040), freestanding:
# no newlib, no C++, only libgcc's division/shift helpers.
#
# What this deliberately does NOT fetch:
#   - libnewlib-arm-none-eabi (~34 MB): the generated C is freestanding,
#     calls no libc functions, and uses only libgcc's integer helpers.
#   - The C++ compiler (cc1plus, ~13 MB): the generator emits C.
#   - The Pico SDK: ~30 MB, CMake-based, LGPL runtime. The codegen writes
#     registers directly, the same discipline as the 8051 and AVR targets.
#
# Size: the full gcc-arm-none-eabi install is ~120 MB (dozens of multilibs
# for v5te/v7/v7-m/v7e-m/v7+fp etc). We keep ONE: thumb/v6-m/nofp (the
# Cortex-M0/M0+ variant). That plus cc1 and the binutils lands near 30 MB.
#
# Run this from the repo root:  ./scripts/fetch-arm-gcc.sh
#
set -euo pipefail

# Bullseye for GLIBC compatibility with Vercel's Amazon Linux 2023 (glibc 2.34).
# Same constraint as the AVR bundle: bookworm's builds need GLIBC_2.36.
SUITE="bullseye"
POOL="https://deb.debian.org/debian/pool/main"

GCC_ARM="gcc-arm-none-eabi_8-2019-q3-1+b1_amd64.deb"
BINUTILS_ARM="binutils-arm-none-eabi_2.35.2-2+14+b2_amd64.deb"

# Same runtime libraries as the AVR bundle PLUS libisl23: the ARM gcc 8.3.1
# links against libisl which the AVR gcc 5.4.0 does not. Without it, cc1
# dies with "error while loading shared libraries: libisl.so.23".
RUNTIME_LIBS="
  g/gmp/libgmp10_6.2.1+dfsg-1+deb11u1_amd64.deb
  m/mpclib3/libmpc3_1.2.0-1_amd64.deb
  m/mpfr4/libmpfr6_4.1.0-3_amd64.deb
  z/zlib/zlib1g_1.2.11.dfsg-2+deb11u2_amd64.deb
  i/isl/libisl23_0.23-1_amd64.deb
"

# The RP2040 is Cortex-M0+ = ARMv6-M, Thumb only, no FPU.
MULTILIB="thumb/v6-m/nofp"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }

say "Downloading the ARM toolchain from Debian ${SUITE} ..."
for pkg in "$GCC_ARM" "$BINUTILS_ARM"; do
  case "$pkg" in
    gcc-arm*)      dir="g/gcc-arm-none-eabi" ;;
    binutils-arm*) dir="b/binutils-arm-none-eabi" ;;
  esac
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

say "Assembling the v6-m-only bundle ..."
#
# Layout mirrors the AVR bundle's convention:
#
#   arm/bin/                             arm-none-eabi-gcc, -objcopy, -objdump
#   arm/lib/gcc/arm-none-eabi/<ver>/     cc1, collect2, include, v6-m multilib
#   arm/arm-none-eabi/bin/               as, ld (unprefixed, driver finds them)
#   arm/lib-deps/                        shared libraries for cc1 + binutils
#
# The gcc driver resolves cc1 via ../lib/gcc/arm-none-eabi/<ver>/ relative
# to argv[0], and tools via $prefix/arm-none-eabi/bin/. Debian's
# gcc-arm-none-eabi configures --prefix=/usr/lib --libdir=/usr/lib, so after
# relocation the gcc driver resolves its prefix to arm/lib/ (not arm/).
# The tooldir it searches is therefore arm/lib/arm-none-eabi/bin/, NOT
# arm/arm-none-eabi/bin/. Getting this wrong makes the driver find the HOST
# assembler via PATH, which accepts none of the ARM-specific flags and fails
# with "invalid -march= option: armv6s-m". A symlink covers the standard
# cross-compiler layout too.
#
rm -rf "$ROOT/arm"
mkdir -p "$ROOT/arm/bin" "$ROOT/arm/lib"

# The tools app.py and the verifier invoke by name.
for b in arm-none-eabi-gcc arm-none-eabi-objcopy arm-none-eabi-objdump arm-none-eabi-size; do
  if [ -f "$WORK/x/usr/bin/$b" ]; then
    cp "$WORK/x/usr/bin/$b" "$ROOT/arm/bin/$b"
    chmod +x "$ROOT/arm/bin/$b"
  else
    echo "  WARNING: $b not found in the packages" >&2
  fi
done

# The tooldir: unprefixed tools where the driver looks for them.
# arm-none-eabi-gcc -print-search-dirs resolves to:
#   .../arm/lib/gcc/arm-none-eabi/8.3.1/../../../arm-none-eabi/bin/
# which is arm/lib/arm-none-eabi/bin/ (because --prefix=/usr/lib, the
# Debian packaging's oddest decision). A symlink arm/arm-none-eabi ->
# lib/arm-none-eabi covers the standard prefix layout.
mkdir -p "$ROOT/arm/lib/arm-none-eabi/bin"
for b in as ld ar ranlib objcopy objdump; do
  src="$WORK/x/usr/lib/arm-none-eabi/bin/$b"
  if [ -f "$src" ]; then
    cp "$src" "$ROOT/arm/lib/arm-none-eabi/bin/$b"
    chmod +x "$ROOT/arm/lib/arm-none-eabi/bin/$b"
  elif [ -f "$WORK/x/usr/bin/arm-none-eabi-$b" ]; then
    cp "$WORK/x/usr/bin/arm-none-eabi-$b" "$ROOT/arm/lib/arm-none-eabi/bin/$b"
    chmod +x "$ROOT/arm/lib/arm-none-eabi/bin/$b"
  fi
done
ln -sfn lib/arm-none-eabi "$ROOT/arm/arm-none-eabi"

GCCLIB="$WORK/x/usr/lib/gcc/arm-none-eabi"
test -d "$GCCLIB" || { echo "gcc-arm-none-eabi package has no lib/gcc/arm-none-eabi" >&2; exit 1; }
VERSION="$(ls "$GCCLIB" | head -1)"
SRC="$GCCLIB/$VERSION"
DST="$ROOT/arm/lib/gcc/arm-none-eabi/$VERSION"
mkdir -p "$DST"

# cc1 is the C compiler proper; collect2 is what the driver calls to link.
# cc1plus (C++) and lto1 (LTO) are left behind.
for f in cc1 collect2; do
  test -f "$SRC/$f" || { echo "missing $f in gcc-arm-none-eabi" >&2; exit 1; }
  cp "$SRC/$f" "$DST/$f"
  chmod +x "$DST/$f"
done

# The LTO linker plugin, needed even without -flto (same as AVR).
cp -a "$SRC"/liblto_plugin.so* "$DST/" 2>/dev/null || true
test -f "$SRC/lto-wrapper" && cp "$SRC/lto-wrapper" "$DST/lto-wrapper" \
  && chmod +x "$DST/lto-wrapper"

# GCC's own headers (stdint.h, stddef.h, stdarg.h, stdbool.h, etc.)
cp -R "$SRC/include" "$DST/include"
test -d "$SRC/include-fixed" && cp -R "$SRC/include-fixed" "$DST/include-fixed"

# The default multilib (root-level crt and libgcc — for the default ARM mode)
for f in "$SRC"/crt*.o "$SRC"/libgcc.a; do
  test -f "$f" && cp "$f" "$DST/"
done

# The v6-m multilib: libgcc.a and CRT files for Cortex-M0/M0+
if [ -d "$SRC/$MULTILIB" ]; then
  mkdir -p "$DST/$MULTILIB"
  cp -R "$SRC/$MULTILIB"/* "$DST/$MULTILIB/"
else
  echo "  ERROR: multilib $MULTILIB not found in gcc-arm-none-eabi" >&2
  exit 1
fi

# thumb/nofp (the generic Thumb fallback) is NOT carried: we always pass
# -mcpu=cortex-m0plus which selects thumb/v6-m/nofp, and the generic
# multilib is 22 MB of exact duplicates. If a build ever picks the wrong
# multilib, the symptom is a link failure (missing libgcc), and the fix is
# to add the right multilib here — not to carry them all.

# The shared libraries cc1 and the binutils need.
mkdir -p "$ROOT/arm/lib-deps"
for so in "$WORK"/x/usr/lib/x86_64-linux-gnu/lib*.so.*; do
  test -e "$so" && cp -a "$so" "$ROOT/arm/lib-deps/"
done
for so in "$WORK"/x/lib/x86_64-linux-gnu/lib*.so.*; do
  test -e "$so" && cp -a "$so" "$ROOT/arm/lib-deps/"
done

# GPL section 1: licences travel with the binaries.
mkdir -p "$ROOT/vendor/arm"
for p in gcc-arm-none-eabi binutils-arm-none-eabi; do
  cp "$WORK/x/usr/share/doc/$p/copyright" "$ROOT/vendor/arm/$p.copyright" 2>/dev/null || true
done

cat > "$ROOT/vendor/arm/VERSION" <<EOF
gcc-arm-none-eabi  ${GCC_ARM}
binutils-arm       ${BINUTILS_ARM}

from Debian ${SUITE}, ${POOL}
corresponding source: apt-get source gcc-arm-none-eabi binutils-arm-none-eabi

gcc-arm-none-eabi and binutils-arm-none-eabi are GPL-3.0-or-later. The GCC
Runtime Library Exception covers libgcc, so images compiled with this bundle
are unencumbered. No newlib (BSD/LGPL) is vendored or linked.
EOF

printf '%s\n' "$VERSION" > "$ROOT/arm/GCC_VERSION"

say "Done."
du -sh "$ROOT/arm" | sed 's/^/    /'
echo
echo "arm-none-eabi-gcc present:  $(test -x "$ROOT/arm/bin/arm-none-eabi-gcc" && echo yes || echo NO)"
echo "cc1 present:                $(test -x "$DST/cc1" && echo yes || echo NO)"
echo "tooldir as:                 $(test -x "$ROOT/arm/lib/arm-none-eabi/bin/as" && echo yes || echo NO)"
echo "tooldir ld:                 $(test -x "$ROOT/arm/lib/arm-none-eabi/bin/ld" && echo yes || echo NO)"
echo "v6-m libgcc:                $(test -f "$DST/$MULTILIB/libgcc.a" && echo yes || echo NO)"
echo "runtime libs:               $(ls "$ROOT/arm/lib-deps" 2>/dev/null | tr '\n' ' ')"
echo
echo "GLIBC requirement (must be <= 2.34 to start on Vercel):"
strings "$DST/cc1" 2>/dev/null | grep -oE 'GLIBC_2\.[0-9]+' | sort -V | tail -1 \
  | sed 's/^/    /'
echo
