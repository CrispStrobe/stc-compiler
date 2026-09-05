#!/usr/bin/env bash
#
# fetch-sdcc.sh — build the vendored SDCC bundle in bin/ + share/.
#
# Vercel functions run x86_64 Linux, so we need Linux binaries regardless of
# what you develop on. Rather than cross-compiling, we lift them out of
# Debian's .deb packages, which are already built and already stripped.
#
# Why Debian bullseye (sdcc 4.0.0) and not the newest release: glibc symbol
# versioning is forward-compatible only. Bullseye's toolchain caps the
# requirement at GLIBC_2.29, which runs on Vercel's Amazon Linux 2023
# (glibc 2.34). Bookworm's sdcc 4.5.0 needs GLIBC_2.36 and would not start.
#
# We keep what the mcs51 (8051) AND z80 targets need. The full SDCC install
# is ~150 MB, almost all of it pic16 libraries and docs we will never touch;
# the z80 half adds 280 KB (crt0.rel + z80.lib).
#
# Run this from the repo root:  ./scripts/fetch-sdcc.sh
#
set -euo pipefail

SDCC_VERSION="4.0.0+dfsg-2"
POOL="https://deb.debian.org/debian/pool/main/s/sdcc"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }

say "Downloading sdcc ${SDCC_VERSION} ..."
for pkg in "sdcc_${SDCC_VERSION}_amd64.deb" "sdcc-libraries_${SDCC_VERSION}_all.deb"; do
  curl -fsSL --max-time 300 -o "$WORK/$pkg" "$POOL/$pkg"
done

say "Unpacking ..."
mkdir -p "$WORK/x"
for deb in "$WORK"/*.deb; do
  # .deb payloads are data.tar.xz on bullseye; newer ones use zstd.
  ar p "$deb" data.tar.xz 2>/dev/null | tar -xJ -C "$WORK/x" \
    || ar p "$deb" data.tar.zst | tar --zstd -x -C "$WORK/x"
done

say "Assembling the mcs51 + z80 bundle ..."
rm -rf "$ROOT/bin" "$ROOT/share"
mkdir -p "$ROOT/bin" "$ROOT/share/sdcc/include" "$ROOT/share/sdcc/lib"

# sdcc fork/execs exactly three helpers PER PORT: sdcpp (preprocessor), an
# assembler and a linker. Verify with `sdcc -V`. mcs51 uses sdas8051+sdld,
# z80 uses sdasz80+sdldz80 -- one sdcc binary drives both, because the
# Debian build has every port compiled in (`sdcc --version` lists them).
# packihx and makebin are optional output converters we expose as response
# formats; the z80 lane uses makebin for its raw 32 KB ROM.
for b in sdcc sdcpp sdas8051 sdld sdasz80 sdldz80 packihx makebin; do
  cp "$WORK/x/usr/bin/$b" "$ROOT/bin/$b"
  chmod +x "$ROOT/bin/$b"
done

S="$WORK/x/usr/share/sdcc"
cp -R "$S/include/mcs51" "$ROOT/share/sdcc/include/"
cp "$S"/include/*.h "$ROOT/share/sdcc/include/"
# The four mcs51 memory models. --model-small is the default; the others are
# cheap to carry and let callers pass --model-large etc.
for m in small medium large huge; do
  cp -R "$S/lib/$m" "$ROOT/share/sdcc/lib/"
done
# The z80 runtime: crt0.rel (reset vector at $0000, SP, gsinit, call _main)
# and z80.lib (the integer/long/float helpers the code generator calls).
# 280 KB. Without it `sdcc -mz80` compiles and then links against whatever
# /usr/share/sdcc a DEVELOPER happens to have -- which is not a thing that
# exists on Vercel, so the hosted z80 C target would 404 at link time while
# passing every local test. Measured 2026-09-05.
cp -R "$S/lib/z80" "$ROOT/share/sdcc/lib/"

# GPLv2 section 1 requires the licence to travel with the binaries.
mkdir -p "$ROOT/vendor/sdcc"
cp "$WORK/x/usr/share/doc/sdcc/copyright" "$ROOT/vendor/sdcc/copyright" 2>/dev/null || true

cat > "$ROOT/vendor/sdcc/VERSION" <<EOF
sdcc ${SDCC_VERSION} (amd64), from Debian bullseye
source: ${POOL}
corresponding source: apt-get source sdcc=${SDCC_VERSION}
upstream: https://sdcc.sourceforge.net/
EOF

say "Done."
du -sh "$ROOT/bin" "$ROOT/share" | sed 's/^/    /'
printf '    %s total\n' "$(du -sh "$ROOT/bin" "$ROOT/share" | awk '{s+=$1} END {print s"M (approx)"}')"
echo
echo "stc12.h present: $(test -f "$ROOT/share/sdcc/include/mcs51/stc12.h" && echo yes || echo NO)"
echo "z80 crt0 present: $(test -f "$ROOT/share/sdcc/lib/z80/crt0.rel" && echo yes || echo NO)"
echo "z80.lib present:  $(test -f "$ROOT/share/sdcc/lib/z80/z80.lib" && echo yes || echo NO)"
