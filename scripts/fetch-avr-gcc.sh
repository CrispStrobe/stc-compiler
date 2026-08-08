#!/usr/bin/env bash
#
# fetch-avr-gcc.sh — build the vendored AVR bundle in avr/.
#
# Same trick as fetch-sdcc.sh, and for the same reason: Vercel functions run
# x86_64 Linux, so we lift already-built, already-stripped binaries out of
# Debian's .deb packages rather than cross-compiling anything.
#
# What this deliberately does NOT fetch is arduino-cli and the Arduino AVR
# core. That combination is ~250 MB against Vercel's 250 MB function limit,
# it downloads cores at runtime, and the core is LGPL-2.1 — statically linked
# into a .hex we hand back, that engages the relink obligation. The generator
# writes ports directly instead (stc_pseudocode.AvrTarget), so avr-libc is all
# the runtime we need, and avr-libc is BSD-3-Clause.
#
# Size: the full gcc-avr install is ~230 MB, and almost all of it is the 42
# multilib variants and the C++ compiler. We keep cc1 (not cc1plus — the
# generator emits C), the driver, the assembler, the linker, objcopy/objdump/
# size, and the ONE multilib the supported parts use. That lands near 25 MB.
#
# Run this from the repo root:  ./scripts/fetch-avr-gcc.sh
#
set -euo pipefail

# Bullseye for the same GLIBC reason as SDCC: symbol versioning is
# forward-compatible only, and Vercel's Amazon Linux 2023 has glibc 2.34.
# Verify after building:
#   strings avr/libexec/gcc/avr/*/cc1 | grep -oE 'GLIBC_2\.[0-9]+' | sort -V | tail -1
#
# Bookworm and trixie were both checked and both rejected: bookworm's build of
# the same gcc-avr 5.4.0 needs GLIBC_2.36, and trixie carries gcc-avr 14.2.0
# needing newer still. Neither starts on Amazon Linux 2023.
#
# Re-derive these filenames after a suite bump with:
#   curl -fsSL https://deb.debian.org/debian/dists/bullseye/main/binary-amd64/Packages.xz \
#     | xz -dc | awk -v RS='' '/(^|\n)Package: (gcc-avr|binutils-avr|avr-libc)(\n|$)/ \
#         {for(i=1;i<=split($0,L,"\n");i++) if(L[i]~/^(Package|Version|Filename): /) print L[i]; print ""}'
SUITE="bullseye"
POOL="https://deb.debian.org/debian/pool/main"

GCC_AVR="gcc-avr_5.4.0+Atmel3.6.2-1+b1_amd64.deb"
BINUTILS_AVR="binutils-avr_2.26.20160125+Atmel3.6.2-2+b1_amd64.deb"
AVR_LIBC="avr-libc_2.0.0+Atmel3.6.2-1.1_all.deb"

# The ATmega328P and ATmega168P are both avr5. If a part outside that family
# is ever added to AVR_TARGETS in app.py, its multilib has to be added here
# too, or the link fails with a missing libgcc.
MULTILIBS="avr5"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }

say "Downloading the AVR toolchain from Debian ${SUITE} ..."
for pkg in "$GCC_AVR" "$BINUTILS_AVR" "$AVR_LIBC"; do
  case "$pkg" in
    gcc-avr*)      dir="g/gcc-avr" ;;
    binutils-avr*) dir="b/binutils-avr" ;;
    avr-libc*)     dir="a/avr-libc" ;;
  esac
  say "  $pkg"
  curl -fsSL --max-time 600 -o "$WORK/$pkg" "$POOL/$dir/$pkg"
done

say "Unpacking ..."
mkdir -p "$WORK/x"
for deb in "$WORK"/*.deb; do
  ar p "$deb" data.tar.xz 2>/dev/null | tar -xJ -C "$WORK/x" \
    || ar p "$deb" data.tar.zst | tar --zstd -x -C "$WORK/x"
done

say "Assembling the avr5-only bundle ..."
#
# The layout mirrors Debian's under avr/ rather than inventing one, because
# the gcc driver locates cc1 and collect2 relative to its own argv[0]:
# bin/avr-gcc looks for ../lib/gcc/avr/<version>/. Flattening this the way
# fetch-sdcc.sh flattens SDCC's would break that lookup.
#
#   avr/bin/                    avr-gcc and the binutils it fork/execs
#   avr/lib/gcc/avr/<ver>/      cc1, collect2, device-specs, avr5 multilib
#   avr/lib/avr/                avr-libc: headers and the avr5 libraries
#
rm -rf "$ROOT/avr"
mkdir -p "$ROOT/avr/bin" "$ROOT/avr/lib"

# The driver plus the binutils it fork/execs. avr-gcc runs `avr-as` and
# `avr-ld` by name, and objcopy/objdump/size are what turn the .elf into a
# .hex and report its size.
for b in avr-gcc avr-as avr-ld avr-objcopy avr-objdump avr-size avr-ar avr-ranlib; do
  if [ -f "$WORK/x/usr/bin/$b" ]; then
    cp "$WORK/x/usr/bin/$b" "$ROOT/avr/bin/$b"
    chmod +x "$ROOT/avr/bin/$b"
  else
    echo "  WARNING: $b not found in the packages" >&2
  fi
done

GCCLIB="$WORK/x/usr/lib/gcc/avr"
test -d "$GCCLIB" || { echo "gcc-avr package has no lib/gcc/avr" >&2; exit 1; }
VERSION="$(ls "$GCCLIB" | head -1)"
SRC="$GCCLIB/$VERSION"
DST="$ROOT/avr/lib/gcc/avr/$VERSION"
mkdir -p "$DST"

# cc1 is the C compiler proper; collect2 is what the driver calls to link.
# cc1plus (13 MB) and lto1 are deliberately left behind: nothing here emits
# C++, and -flto is not offered.
for f in cc1 collect2 libgcc.a; do
  test -f "$SRC/$f" || { echo "missing $f in gcc-avr" >&2; exit 1; }
  cp "$SRC/$f" "$DST/$f"
done
chmod +x "$DST/cc1" "$DST/collect2"

# device-specs is what makes -mmcu=atmega328p mean anything: one spec file per
# part, naming its core, its startfile and its device library. Small, and
# dropping the unused ones would only make the error for an unsupported part
# worse.
cp -R "$SRC/device-specs" "$DST/device-specs"
# GCC's own headers (stddef.h, stdint-gcc.h, ...) -- not avr-libc's.
cp -R "$SRC/include" "$DST/include"
test -d "$SRC/include-fixed" && cp -R "$SRC/include-fixed" "$DST/include-fixed"

# The compiler support library for the multilibs we keep. The files sitting
# directly in the version directory are the default (avr2) variant.
for m in $MULTILIBS; do
  test -d "$SRC/$m" && cp -R "$SRC/$m" "$DST/$m"
done

# avr-libc. The headers are ~19 MB because there is one per part; they are
# kept whole, since avr/io.h dispatches to them by device macro and a trimmed
# set turns an unsupported part into a confusing #include error.
mkdir -p "$ROOT/avr/lib/avr/lib"
cp -R "$WORK/x/usr/lib/avr/include" "$ROOT/avr/lib/avr/include"
for m in $MULTILIBS; do
  test -d "$WORK/x/usr/lib/avr/lib/$m" \
    && cp -R "$WORK/x/usr/lib/avr/lib/$m" "$ROOT/avr/lib/avr/lib/$m"
done
find "$WORK/x/usr/lib/avr/lib" -maxdepth 1 -type f \
     -exec cp {} "$ROOT/avr/lib/avr/lib/" \;

# GPL section 1: the licences travel with the binaries. avr-gcc is GPL-3.0
# (with the GCC Runtime Library Exception, which is what leaves compiled
# output unencumbered); avr-libc is BSD-3-Clause.
mkdir -p "$ROOT/vendor/avr"
for p in gcc-avr binutils-avr avr-libc; do
  cp "$WORK/x/usr/share/doc/$p/copyright" "$ROOT/vendor/avr/$p.copyright" 2>/dev/null || true
done

cat > "$ROOT/vendor/avr/VERSION" <<EOF
gcc-avr      ${GCC_AVR}
binutils-avr ${BINUTILS_AVR}
avr-libc     ${AVR_LIBC}

from Debian ${SUITE}, ${POOL}
corresponding source: apt-get source gcc-avr binutils-avr avr-libc

gcc-avr and binutils-avr are GPL-3.0-or-later. The GCC Runtime Library
Exception covers libgcc, so images compiled with this bundle are unencumbered.
avr-libc is BSD-3-Clause. The Arduino core (LGPL-2.1) is NOT vendored and is
not linked into anything this service returns.
EOF

printf '%s\n' "$VERSION" > "$ROOT/avr/GCC_VERSION"

say "Done."
du -sh "$ROOT/avr" | sed 's/^/    /'
echo
echo "avr-gcc present: $(test -x "$ROOT/avr/bin/avr-gcc" && echo yes || echo NO)"
echo "cc1 present:     $(test -x "$DST/cc1" && echo yes || echo NO)"
echo "io.h present:    $(test -f "$ROOT/avr/lib/avr/include/avr/io.h" && echo yes || echo NO)"
echo "avr5 crt:        $(ls "$ROOT/avr/lib/avr/lib/avr5"/crtatmega328p.o >/dev/null 2>&1 \
                          && echo yes || echo NO)"
echo
echo "GLIBC requirement (must be <= 2.34 to start on Vercel):"
strings "$DST/cc1" 2>/dev/null | grep -oE 'GLIBC_2\.[0-9]+' | sort -V | tail -1 \
  | sed 's/^/    /'
echo
# These are Linux x86_64 binaries. They cannot be run on macOS, so the bundle
# is unverified until this is executed somewhere it can run -- a Linux box, or
#   docker run --rm -v "$PWD:/w" -w /w debian:bullseye-slim ./scripts/verify-avr.sh
echo "Smoke test (must run on Linux x86_64):"
echo "    ./scripts/verify-avr.sh"
