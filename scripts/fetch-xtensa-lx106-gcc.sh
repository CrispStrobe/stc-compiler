#!/usr/bin/env bash
#
# fetch-xtensa-lx106-gcc.sh — carve the vendored ESP8266 bundle in xtensa-lx106-gcc/.
#
# Source: the prebuilt x86_64-linux-gnu xtensa-lx106-elf toolchain from
# earlephilhower/esp-quick-toolchain (GCC 10.3), the SAME archive the Arduino
# ESP8266 core 3.1.2 installs as its compiler (tool version
# 3.1.0-gcc10.3-e5f9fec). Only the compiler is taken; nothing of the Arduino
# core, the Espressif SDK, newlib or libstdc++ is kept.
#
# Kept (everything the `esp8266` target's freestanding C build runs):
#   bin/xtensa-lx106-elf-gcc                       the driver
#   libexec/gcc/xtensa-lx106-elf/10.3.0/cc1        C compiler proper
# (all binaries and libgcc.a are stored as .xz, see below)
#   libexec/gcc/xtensa-lx106-elf/10.3.0/collect2   link wrapper the driver runs
#   libexec/gcc/xtensa-lx106-elf/10.3.0/liblto_plugin.so  the driver insists on it
#   xtensa-lx106-elf/bin/{as,ld}                   binutils the driver runs
#   lib/gcc/xtensa-lx106-elf/10.3.0/libgcc.a       (GCC Runtime Library Exception)
#   lib/gcc/xtensa-lx106-elf/10.3.0/include{,-fixed}  freestanding headers
#                                                  (stdint.h, stddef.h, ...)
# The binaries are statically linked against gmp/mpfr/mpc, so only glibc is
# NEEDED and no lib-deps/ directory is required.
#
# Run from the repo root:  ./scripts/fetch-xtensa-lx106-gcc.sh
#
set -euo pipefail

URL="https://github.com/earlephilhower/esp-quick-toolchain/releases/download/3.1.0-gcc10.3/x86_64-linux-gnu.xtensa-lx106-elf-e5f9fec.220621.tar.gz"
SHA256="89d21f38a516e34e0bb44df7e30ef6b1bc2f7f125aae3b7986e5cae545a3ed83"
GCCV="10.3.0"
OUT="xtensa-lx106-gcc"

[ -f app.py ] || { echo "run from the repository root" >&2; exit 1; }

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
curl -fsSL -o "$tmp/tc.tgz" "$URL"
echo "$SHA256  $tmp/tc.tgz" | sha256sum -c -
tar -xzf "$tmp/tc.tgz" -C "$tmp"
src="$tmp/xtensa-lx106-elf"

keep_provenance=""
[ -f "$OUT/PROVENANCE.md" ] && keep_provenance="$(cat "$OUT/PROVENANCE.md")"
rm -rf "$OUT"
mkdir -p "$OUT/bin" "$OUT/libexec/gcc/xtensa-lx106-elf/$GCCV" \
         "$OUT/xtensa-lx106-elf/bin" "$OUT/lib/gcc/xtensa-lx106-elf/$GCCV"

cp -p "$src/bin/xtensa-lx106-elf-gcc" "$OUT/bin/"
for f in cc1 collect2; do
  cp -p "$src/libexec/gcc/xtensa-lx106-elf/$GCCV/$f" "$OUT/libexec/gcc/xtensa-lx106-elf/$GCCV/"
done
# The driver passes -fuse-linker-plugin by default and refuses to link
# without the plugin present (it is not used: no -flto). One regular file.
cp -pL "$src/libexec/gcc/xtensa-lx106-elf/$GCCV/liblto_plugin.so" \
       "$OUT/libexec/gcc/xtensa-lx106-elf/$GCCV/liblto_plugin.so"
for f in as ld; do
  cp -p "$src/xtensa-lx106-elf/bin/$f" "$OUT/xtensa-lx106-elf/bin/"
done
cp -p "$src/lib/gcc/xtensa-lx106-elf/$GCCV/libgcc.a" "$OUT/lib/gcc/xtensa-lx106-elf/$GCCV/"
cp -a "$src/lib/gcc/xtensa-lx106-elf/$GCCV/include" \
      "$src/lib/gcc/xtensa-lx106-elf/$GCCV/include-fixed" \
      "$OUT/lib/gcc/xtensa-lx106-elf/$GCCV/"
# Headers no freestanding C sketch here needs (C++/gcov/SSP/fixed-point).
rm -rf "$OUT/lib/gcc/xtensa-lx106-elf/$GCCV/include/ssp" \
       "$OUT/lib/gcc/xtensa-lx106-elf/$GCCV/include/"{gcov.h,unwind.h,stdfix.h}
# GPL texts the toolchain ships, carried with the binaries.
for lic in COPYING COPYING3 COPYING.RUNTIME COPYING.LIB COPYING3.LIB; do
  f="$(find "$src" -name "$lic" -type f | head -1 || true)"
  [ -n "$f" ] && cp "$f" "$OUT/$lic"
done

# Every binary (and libgcc.a) is committed xz-compressed and decompressed at
# staging time by bundle_xz.materialize: the deployment has ~7 MB of room
# under Vercel's limit and the uncompressed set is 26 MB. Executables use the
# x86 BCJ filter (cc1: 6.6 -> 6.1 MB); lzma.decompress reads either form.
while IFS= read -r f; do
  if [ "$(head -c4 "$f" | od -An -tx1 | tr -d ' ')" = "7f454c46" ]; then
    xz -T1 --x86 --lzma2=preset=9e -c "$f" > "$f.xz"
  else
    xz -T1 -9e -c "$f" > "$f.xz"
  fi
  rm "$f"
done < <(find "$OUT/bin" "$OUT/libexec" "$OUT/xtensa-lx106-elf/bin" -type f; \
         echo "$OUT/lib/gcc/xtensa-lx106-elf/$GCCV/libgcc.a")

[ -n "$keep_provenance" ] && printf '%s\n' "$keep_provenance" > "$OUT/PROVENANCE.md"
du -sh "$OUT"
