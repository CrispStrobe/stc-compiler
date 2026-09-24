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

# Libraries that are not part of either core, each pinned and checksummed,
# each LGPL like the cores (NOTICE.md). Servo is the ATmega implementation;
# the ATtinys get ATTinyCore's own Servo (TINY_LIBRARIES). LiquidCrystal and
# Adafruit_NeoPixel are architecture-independent on AVR and go to
# libraries/common, which both cores search.
SERVO_REPO="arduino-libraries/Servo"
SERVO_SHA="2862ad9864d0dda5669b264857deafbba624940d"          # 1.3.0
SERVO_SHA256="e75f7357d024934a3460db993f8f5d0481775bc08d8faf8accdd94cccfb12823"
LCD_REPO="arduino-libraries/LiquidCrystal"
LCD_SHA="a89b4d46b28878557670440f2c35385c19307b69"            # 1.0.7
LCD_SHA256="c0f0792e6048c6b268c82b62ff6a6fd5c812ad311ecbdc213944fa245a39fd0d"
NEOPIXEL_REPO="adafruit/Adafruit_NeoPixel"
NEOPIXEL_SHA="d514fc3beae85dd4c2b19781b93faa47bd6e996f"       # 1.15.5
NEOPIXEL_SHA256="a149a7a81cbb32d3917bebe4d63d8e50043ea11dd7e94682905b1720de314487"

AVR_VARIANTS="standard eightanaloginputs mega"
AVR_LIBRARIES="EEPROM SPI Wire SoftwareSerial"
TINY_VARIANTS="tinyx5 tinyx8"
TINY_LIBRARIES="EEPROM SPI Wire SoftwareSerial Servo_ATTinyCore tinyNeoPixel"

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
fetch "$SERVO_REPO" "$SERVO_SHA" "$SERVO_SHA256" "$WORK/servo"
fetch "$LCD_REPO" "$LCD_SHA" "$LCD_SHA256" "$WORK/lcd"
fetch "$NEOPIXEL_REPO" "$NEOPIXEL_SHA" "$NEOPIXEL_SHA256" "$WORK/neopixel"

say "Assembling arduino-core/ ..."
# LICENSE.md is ATTinyCore's (the full LGPL-2.1 text); ArduinoCore-avr ships
# no licence file of its own and states LGPL-2.1 in every source header.
rm -rf "$ROOT/arduino-core"
mkdir -p "$ROOT/arduino-core/cores" "$ROOT/arduino-core/variants" \
         "$ROOT/arduino-core/libraries/arduino" "$ROOT/arduino-core/libraries/tiny" \
         "$ROOT/arduino-core/libraries/common"
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

# Servo keeps only its AVR implementation (src/avr) and the shared header;
# the samd/sam/stm32/... directories are guarded out on AVR anyway.
mkdir -p "$ROOT/arduino-core/libraries/arduino/Servo/src"
cp "$WORK/servo/src/Servo.h" "$ROOT/arduino-core/libraries/arduino/Servo/src/"
cp -R "$WORK/servo/src/avr" "$ROOT/arduino-core/libraries/arduino/Servo/src/avr"
cp "$WORK/servo/library.properties" "$ROOT/arduino-core/libraries/arduino/Servo/"
copy_lib "$WORK/lcd" "$ROOT/arduino-core/libraries/common/LiquidCrystal"
# NeoPixel: the library proper plus its AVR-relevant sources; the esp/rp2040/
# k210 back ends are separate files guarded to their own architectures.
mkdir -p "$ROOT/arduino-core/libraries/common/Adafruit_NeoPixel/src"
cp "$WORK/neopixel/Adafruit_NeoPixel.h" "$WORK/neopixel/Adafruit_NeoPixel.cpp" \
   "$ROOT/arduino-core/libraries/common/Adafruit_NeoPixel/src/"
cp "$WORK/neopixel/library.properties" "$WORK/neopixel/COPYING" \
   "$ROOT/arduino-core/libraries/common/Adafruit_NeoPixel/" 2>/dev/null || true

cat > "$ROOT/arduino-core/VERSION" <<EOF
ArduinoCore-avr  https://github.com/${AVR_CORE_REPO}  ${AVR_CORE_SHA}  (1.8.8)
ATTinyCore       https://github.com/${TINY_CORE_REPO}  ${TINY_CORE_SHA}  (master)
Servo            https://github.com/${SERVO_REPO}  ${SERVO_SHA}  (1.3.0)
LiquidCrystal    https://github.com/${LCD_REPO}  ${LCD_SHA}  (1.0.7)
Adafruit_NeoPixel https://github.com/${NEOPIXEL_REPO}  ${NEOPIXEL_SHA}  (1.15.5)

LGPL (2.1-or-later; Adafruit_NeoPixel LGPL-3.0). Regenerate with scripts/fetch-arduino-core.sh; do not
hand-edit files under this directory.
EOF

say "Done."
du -sh "$ROOT/arduino-core" | sed 's/^/    /'
