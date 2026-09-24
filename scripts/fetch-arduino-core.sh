#!/usr/bin/env bash
#
# fetch-arduino-core.sh — build the vendored Arduino cores in arduino-core/.
#
# The `arduino` language route compiles a sketch as real C++ against the SAME
# core source the Arduino IDE would use, so a sketch that builds there builds
# here. Two upstreams, each pinned to a full commit SHA and checked against a
# tarball hash, so the tree is reproducible from this script alone:
#
#   ArduinoCore-avr  (arduino/ArduinoCore-avr, release 1.8.8)
#       cores/arduino                    Uno, Nano, Mega (ATmega328P/168P/2560)
#       variants/standard, eightanaloginputs, mega
#       libraries/EEPROM, SPI, Wire, SoftwareSerial   (the platform bundle;
#                                         HID is left out — it needs native USB)
#
#   ATTinyCore       (SpenceKonde/ATTinyCore, master — the 2.0.0 line)
#       cores/tiny                       ATtiny85, ATtiny88
#       variants/tinyx5, tinyx8
#       libraries/EEPROM, SPI, Wire, SoftwareSerial
#
# Both are LGPL-2.1-or-later. They are compiled on the server from this
# source, per request, alongside the sketch; the posture is argued in
# NOTICE.md. Only the files a build reads are kept: no examples, no
# bootloaders, no boards.txt.
#
# Run this from the repo root:  ./scripts/fetch-arduino-core.sh
#
set -euo pipefail

AVR_CORE_REPO="arduino/ArduinoCore-avr"
AVR_CORE_SHA="86df345b3cf46754a5db38fb983ec2808ce31303"   # tag 1.8.8
AVR_CORE_SHA256="757d54cca2c9d309fe865eabbdd558cd0b14f1742d369a801ac352da1a91a5af"

TINY_CORE_REPO="SpenceKonde/ATTinyCore"
TINY_CORE_SHA="9e7024945b3a873fbb2f482d88625c4d2524ba92"  # master, 2024-10-18
TINY_CORE_SHA256="f4313483fbe5b658557198e4256b4d5d65614d79269fbe26d0ca50a74ccfc5fd"

AVR_VARIANTS="standard eightanaloginputs mega"
AVR_LIBRARIES="EEPROM SPI Wire SoftwareSerial"
TINY_VARIANTS="tinyx5 tinyx8"
TINY_LIBRARIES="EEPROM SPI Wire SoftwareSerial"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

say() { printf '\033[1;34m==>\033[0m %s\n' "$*"; }

fetch() {  # repo sha sha256 dest
  say "  $1 @ $2"
  curl -fsSL --max-time 600 -o "$WORK/$2.tar.gz" \
    "https://codeload.github.com/$1/tar.gz/$2"
  echo "$3  $WORK/$2.tar.gz" | sha256sum -c --quiet - \
    || { echo "checksum mismatch for $1 @ $2" >&2; exit 1; }
  mkdir -p "$4"
  tar -xzf "$WORK/$2.tar.gz" -C "$4" --strip-components=1
}

# One library into arduino-core/libraries/<core>/<name>/src. Arduino has two
# library layouts: 1.5 (sources under src/) and 1.0 (sources at the top, with
# an optional utility/). ATTinyCore ships both, so both are normalised to src/
# and the builder only ever has one shape to read.
copy_lib() {  # from-dir to-dir
  mkdir -p "$2/src"
  if [ -d "$1/src" ]; then
    cp -R "$1/src/." "$2/src/"
  else
    find "$1" -maxdepth 1 -type f \( -name '*.h' -o -name '*.c' -o -name '*.cpp' -o -name '*.S' \) \
      -exec cp {} "$2/src/" \;
    test -d "$1/utility" && cp -R "$1/utility" "$2/src/utility"
  fi
  test -f "$1/library.properties" && cp "$1/library.properties" "$2/"
  return 0
}

say "Downloading the Arduino cores ..."
fetch "$AVR_CORE_REPO" "$AVR_CORE_SHA" "$AVR_CORE_SHA256" "$WORK/avr"
fetch "$TINY_CORE_REPO" "$TINY_CORE_SHA" "$TINY_CORE_SHA256" "$WORK/tiny"

say "Assembling arduino-core/ ..."
# LICENSE.md is ATTinyCore's (the full LGPL-2.1 text); ArduinoCore-avr ships
# no licence file of its own and states LGPL-2.1 in every source header.
rm -rf "$ROOT/arduino-core"
mkdir -p "$ROOT/arduino-core/cores" "$ROOT/arduino-core/variants" \
         "$ROOT/arduino-core/libraries/arduino" "$ROOT/arduino-core/libraries/tiny"
cp "$WORK/tiny/LICENSE.md" "$ROOT/arduino-core/LICENSE.md"

cp -R "$WORK/avr/cores/arduino" "$ROOT/arduino-core/cores/arduino"
for v in $AVR_VARIANTS; do
  cp -R "$WORK/avr/variants/$v" "$ROOT/arduino-core/variants/$v"
done
for l in $AVR_LIBRARIES; do
  copy_lib "$WORK/avr/libraries/$l" "$ROOT/arduino-core/libraries/arduino/$l"
done

cp -R "$WORK/tiny/avr/cores/tiny" "$ROOT/arduino-core/cores/tiny"
for v in $TINY_VARIANTS; do
  cp -R "$WORK/tiny/avr/variants/$v" "$ROOT/arduino-core/variants/$v"
done
for l in $TINY_LIBRARIES; do
  copy_lib "$WORK/tiny/avr/libraries/$l" "$ROOT/arduino-core/libraries/tiny/$l"
done

cat > "$ROOT/arduino-core/VERSION" <<EOF
ArduinoCore-avr  https://github.com/${AVR_CORE_REPO}  ${AVR_CORE_SHA}  (1.8.8)
ATTinyCore       https://github.com/${TINY_CORE_REPO}  ${TINY_CORE_SHA}  (master)

Both LGPL-2.1-or-later. Regenerate with scripts/fetch-arduino-core.sh; do not
hand-edit files under this directory.
EOF

say "Done."
du -sh "$ROOT/arduino-core" | sed 's/^/    /'
