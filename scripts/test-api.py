#!/usr/bin/env python3
"""
test-api.py — exercise a deployed stc-compiler.

    ./scripts/test-api.py                          # test production
    ./scripts/test-api.py http://localhost:3000    # test a local `vercel dev`

Validates more than "did it return 200": every Intel HEX record's checksum is
recomputed, and the Timer 0 reload constant is read back out of the machine
code to prove the -DFOSC_HZ define actually reached the compiler.
"""

import base64
import json
import re
import sys
import time
import urllib.error
import urllib.request

BASE = (sys.argv[1] if len(sys.argv) > 1 else "https://stc-compiler.vercel.app").rstrip("/")

SOURCE = """#include <stc12.h>

#define T0_RELOAD (65536UL - (FOSC_HZ / 12UL / 1000UL))

static void delay_ms(unsigned int ms)
{
    while (ms--) {
        TL0 = (unsigned char)(T0_RELOAD & 0xFF);
        TH0 = (unsigned char)(T0_RELOAD >> 8);
        TF0 = 0; TR0 = 1;
        while (!TF0) ;
        TR0 = 0; TF0 = 0;
    }
}

void main(void)
{
    P1M1 &= ~0x03;
    P1M0 |=  0x03;
    AUXR &= ~0x80;
    TMOD  = (TMOD & 0xF0) | 0x01;
    for (;;) {
        P1_0 = 0; P1_1 = 1; delay_ms(500);
        P1_0 = 1; P1_1 = 0; delay_ms(500);
    }
}
"""

passed = failed = 0


def check(name, ok, detail=""):
    global passed, failed
    if ok:
        passed += 1
        print(f"  \033[32mPASS\033[0m  {name}" + (f"  {detail}" if detail else ""))
    else:
        failed += 1
        print(f"  \033[31mFAIL\033[0m  {name}  {detail}")


def post(payload, timeout=120):
    req = urllib.request.Request(f"{BASE}/compile", json.dumps(payload).encode(),
                                 {"Content-Type": "application/json"})
    start = time.time()
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response), time.time() - start


def code_bytes(hex_text):
    """Parse Intel HEX into {address: byte}, verifying every checksum."""
    out, errors = {}, []
    for lineno, line in enumerate(hex_text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        if not line.startswith(":"):
            errors.append(f"line {lineno}: missing start code")
            continue
        raw = bytes.fromhex(line[1:])
        if (sum(raw) & 0xFF) != 0:
            errors.append(f"line {lineno}: checksum mismatch")
        count, addr, rectype = raw[0], (raw[1] << 8) | raw[2], raw[3]
        if rectype == 0:
            for offset, byte in enumerate(raw[4:4 + count]):
                out[addr + offset] = byte
    return out, errors


def reload_value(image):
    """Pull the Timer 0 reload back out of `mov TL0,#lo` / `mov TH0,#hi`."""
    blob = bytes(image.get(a, 0) for a in range(max(image) + 1))
    match = re.search(rb"\x75\x8a(.)\x75\x8c(.)", blob, re.DOTALL)
    return None if not match else (match.group(2)[0] << 8) | match.group(1)[0]


print(f"testing {BASE}\n")

print("health")
try:
    with urllib.request.urlopen(f"{BASE}/health", timeout=60) as response:
        health = json.load(response)
    check("reachable and toolchain staged", health.get("ok") is True)
    check("reports an SDCC version", "SDCC" in health.get("sdcc", ""),
          health.get("sdcc", "")[:60])
    check("advertises stc12c5a60s2", "stc12c5a60s2" in health.get("targets", {}))
except (urllib.error.URLError, OSError) as exc:
    check("reachable", False, str(exc))
    sys.exit(1)

print("\ncompile")
result, elapsed = post({"code": SOURCE, "target": "stc12c5a60s2", "fosc": 11059200})
check("succeeds", result.get("success") is True, result.get("error", "")[:70])
if not result.get("success"):
    sys.exit(1)
check("round trip under 5s", elapsed < 5, f"{elapsed:.2f}s")

image, errors = code_bytes(base64.b64decode(result["base64"]).decode())
check("every Intel HEX checksum valid", not errors, "; ".join(errors[:2]))
check("code starts at 0x0000", min(image) == 0, f"0x{min(image):04X}")
check("image is non-trivial", len(image) > 100, f"{len(image)} bytes")
check("memory map returned", "ROM/EPROM/FLASH" in result.get("memory", ""))

print("\nFOSC reaches the compiler")
for fosc in (11059200, 12000000, 22118400):
    result, _ = post({"code": SOURCE, "fosc": fosc})
    image, _ = code_bytes(base64.b64decode(result["base64"]).decode())
    got = reload_value(image)
    want = 65536 - fosc // 12 // 1000
    check(f"FOSC={fosc}", got == want,
          f"reload 0x{got:04X}, expected 0x{want:04X}" if got else "not found")

print("\noutput formats")
for fmt, head in (("hex", b":"), ("ihx", b":"), ("bin", None)):
    result, _ = post({"code": SOURCE, "format": fmt})
    blob = base64.b64decode(result["base64"])
    ok = result["success"] and (head is None or blob.startswith(head))
    check(f"format={fmt}", ok, f"{result.get('filename')} {len(blob)} bytes")

print("\nerrors are rejected cleanly")
cases = [
    ("syntax error", {"code": "void main(void) { not C }"}, "syntax error"),
    ("unknown target", {"code": SOURCE, "target": "attiny85"}, "unknown target"),
    ("image too large", {"code": SOURCE, "target": "stc12c5a16s2",
                         "options": ["--code-size", "64"]}, "Insufficient"),
    ("bad define name", {"code": SOURCE, "defines": {"BAD;NAME": "1"}}, "bad define"),
    ("path as option", {"code": SOURCE, "options": ["-I", "/etc"]}, "rejected"),
    ("leading bare value", {"code": SOURCE, "options": ["passwd"]}, "rejected"),
]
for name, payload, expect in cases:
    result, _ = post(payload)
    error = result.get("error", "")
    check(name, result.get("success") is False and expect in error, error[:60])

print("\nno internal paths leak")
result, _ = post({"code": "void main(void) { not C }"})
check("workspace path stripped from errors", "/tmp/build-" not in result.get("error", ""),
      result.get("error", "").splitlines()[0][:60])

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
