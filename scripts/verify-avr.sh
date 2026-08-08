#!/usr/bin/env bash
#
# verify-avr.sh — prove the vendored AVR bundle actually works.
#
# fetch-avr-gcc.sh produces Linux x86_64 binaries. They cannot be executed on
# macOS, so building the bundle is NOT the same as verifying it: the layout
# can be right, the sizes can be right, and cc1 can still fail to start or
# fail to find avr-libc. This script is the check.
#
# Run it on Linux, or in a container from the repo root:
#
#   docker run --rm -v "$PWD:/w" -w /w debian:bullseye-slim ./scripts/verify-avr.sh
#
# It uses ONLY the vendored bundle -- never a system avr-gcc -- because a
# system toolchain passing tells you nothing about what Vercel will run.
#
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
AVR="$ROOT/avr"
GCC="$AVR/bin/avr-gcc"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

fail=0
ok()   { printf '  \033[32mok \033[0m %s\n' "$*"; }
bad()  { printf '  \033[31mFAIL\033[0m %s\n' "$*"; fail=1; }

test -x "$GCC" || { bad "no $GCC — run ./scripts/fetch-avr-gcc.sh first"; exit 1; }

VERSION="$(cat "$AVR/GCC_VERSION" 2>/dev/null || echo "")"
FLAGS="-B$AVR/lib/gcc/avr/$VERSION/ -B$AVR/lib/avr/lib/ -isystem$AVR/lib/avr/include"

echo "verifying the vendored AVR bundle"
echo

# 1. Does the driver start at all? This is where a GLIBC mismatch shows up.
if "$GCC" --version >/dev/null 2>&1; then
  ok "avr-gcc starts: $("$GCC" --version | head -1)"
else
  bad "avr-gcc will not start (GLIBC mismatch?): $("$GCC" --version 2>&1 | head -1)"
  exit 1
fi

# 2. A minimal program: exercises cc1, the assembler and the linker, and
#    proves avr-libc's crt and device library were found.
cat > "$WORK/t.c" <<'EOF'
#include <avr/io.h>
int main(void) { DDRB |= _BV(PB5); for (;;) PORTB ^= _BV(PB5); }
EOF
if out="$("$GCC" -mmcu=atmega328p -Os -DF_CPU=16000000UL $FLAGS \
          -o "$WORK/t.elf" "$WORK/t.c" 2>&1)"; then
  ok "compiles and links for atmega328p"
else
  bad "compile/link failed:"; printf '%s\n' "$out" | sed 's/^/        /'
fi

# 3. Intel HEX out, which is what the service returns.
if [ -f "$WORK/t.elf" ] && "$AVR/bin/avr-objcopy" -O ihex -R .eeprom \
     "$WORK/t.elf" "$WORK/t.hex" 2>/dev/null && [ -s "$WORK/t.hex" ]; then
  if head -1 "$WORK/t.hex" | grep -q '^:'; then
    ok "avr-objcopy emits Intel HEX ($(wc -l < "$WORK/t.hex" | tr -d ' ') records)"
  else
    bad "output is not Intel HEX"
  fi
else
  bad "avr-objcopy produced nothing"
fi

# 4. The generated-code path: the scheduler lowering is a Duff's device, and
#    an interrupt handler needs <avr/interrupt.h> and the vector table.
cat > "$WORK/s.c" <<'EOF'
#include <avr/io.h>
#include <avr/interrupt.h>
static volatile unsigned long ms;
ISR(TIMER0_COMPA_vect) { ms++; }
static unsigned int st; static unsigned long until;
static void task(void) {
    switch (st) {
    case 0:
    st = 1;
    case 1:
    PINB = _BV(PB5);
    until = ms + 500; st = 2;
    case 2:
    if ((long)(ms - until) < 0) return;
    st = 1; return;
    }
}
int main(void) { DDRB |= _BV(PB5); TCCR0A = _BV(WGM01); OCR0A = 249;
                 TIMSK0 = _BV(OCIE0A); TCCR0B = _BV(CS01)|_BV(CS00); sei();
                 for (;;) task(); }
EOF
if out="$("$GCC" -mmcu=atmega328p -Os -std=c99 -Wall -Wextra \
          -Wno-implicit-fallthrough -DF_CPU=16000000UL $FLAGS \
          -o "$WORK/s.elf" "$WORK/s.c" 2>&1)"; then
  ok "the scheduler lowering (Duff's device + ISR) builds"
  if [ -n "$out" ]; then printf '        warnings:\n%s\n' "$out" | sed 's/^/        /'; fi
else
  bad "scheduler lowering failed:"; printf '%s\n' "$out" | sed 's/^/        /'
fi

# 5. avr-size, which the service reports back as `memory`.
if "$AVR/bin/avr-size" --mcu=atmega328p --format=avr "$WORK/s.elf" >/dev/null 2>&1; then
  ok "avr-size reports: $("$AVR/bin/avr-size" --mcu=atmega328p --format=avr \
        "$WORK/s.elf" | awk '/Program:/ {print $2, $3}')"
else
  bad "avr-size failed"
fi

echo
if [ "$fail" -eq 0 ]; then
  echo "bundle verified."
else
  echo "bundle is NOT usable — do not deploy it."
fi
exit "$fail"
