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

print("\ndownload endpoint")
def download(payload):
    req = urllib.request.Request(f"{BASE}/download", json.dumps(payload).encode(),
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as response:
        return response.read(), dict(response.headers)

for fmt, want_name, want_text in (("hex", "main.hex", True),
                                  ("ihx", "main.ihx", True),
                                  ("bin", "main.bin", False)):
    blob, headers = download({"code": SOURCE, "format": fmt})
    disposition = headers.get("Content-Disposition", "")
    check(f"{fmt}: filename in Content-Disposition", f'filename="{want_name}"' in disposition,
          disposition)
    check(f"{fmt}: body is the raw file",
          bool(blob) and (blob.startswith(b":") if want_text else not blob.startswith(b":")),
          f"{len(blob)} bytes")
    check(f"{fmt}: X-Image-Bytes matches body",
          headers.get("X-Image-Bytes") == str(len(blob)),
          f"header {headers.get('X-Image-Bytes')} vs {len(blob)}")

check("CORS exposes the filename header",
      "Content-Disposition" in download({"code": SOURCE})[1]
          .get("Access-Control-Expose-Headers", ""))

try:
    download({"code": "void main(void) { not C }"})
    check("failed download returns 4xx", False, "got 200")
except urllib.error.HTTPError as exc:
    body = exc.read().decode()
    check("failed download returns 4xx", exc.code == 400, f"http {exc.code}")
    check("failed download body is the compiler error", "syntax error" in body,
          body.splitlines()[0][:60])

print("\nbrowser UI")
with urllib.request.urlopen(f"{BASE}/", timeout=60) as response:
    page = response.read().decode()
check("serves HTML", page.lstrip().startswith("<!doctype html"))
check("has a Download control", "id=dl" in page and "URL.createObjectURL" in page)
check("has a format picker", "id=format" in page)
check("hex-dumps binary output", "function hexdump" in page)
check("example is HTML-escaped", "&amp;= ~0x80" in page)

print("\nabout dialog")
check("has an About control", 'id=aboutBtn' in page and '<dialog id=about>' in page)
check("service provider address", "Nikolausstr. 5" in page and "70190 Stuttgart" in page)
check("contact email and phone", "mailto:postmaster@crispstro.be" in page and 'href="tel:+49' in page)
check("bilingual EN/DE panes", 'data-lang="en"' in page and 'data-lang="de"' in page)
check("German section headings", "<h3>Anbieter</h3>" in page and "Haftungsausschluss" in page)
check("links to the GPL-2 text", "gnu.org/licenses/old-licenses/gpl-2.0.html" in page)
check("names the wrapper licence", "MIT" in page)
check("names SDCC's licence", "GPL-2.0-or-later" in page)
check("quotes the linking exception", "special exception" in page)
check("gives corresponding source", "apt-get source sdcc=" in page)
check("has a warranty disclaimer", "without warranty of any kind" in page)
check("warns about the 5V vs LE part", "STC12LE5A60S2 is not" in page)
check("states non-affiliation", "Not affiliated with" in page)
check("links to CrispStrobe", "github.com/CrispStrobe" in page)
check("links to NOTICE.md", "NOTICE.md" in page)
check("describes data handling", "deleted as soon as the response is sent" in page)

print("\npseudocode front end")
PSEUDO = """DEVICE STC12C5A60S2:
  CLOCK 11059200
  PIN led1 = P1.0 OUTPUT ACTIVE LOW
  PIN button = P3.2 INPUT

  WHEN started:
    set n to 0
    FOREVER:
      REPEAT 3:
        turn on led1
        wait 0.2 seconds
        turn off led1
        wait 0.2 seconds
      change n by 1
      IF n > 5 THEN:
        set n to 0
"""

req = urllib.request.Request(f"{BASE}/transpile", json.dumps({"code": PSEUDO}).encode(),
                             {"Content-Type": "application/json"})
with urllib.request.urlopen(req, timeout=120) as response:
    tr = json.load(response)
check("/transpile succeeds", tr.get("success") is True, tr.get("error", "")[:60])
check("reports the clock", tr.get("clock") == 11059200, str(tr.get("clock")))
check("maps pins to SFRs", tr.get("pins", {}).get("led1", {}).get("sfr") == "P1_0")
check("records active-low", tr.get("pins", {}).get("led1", {}).get("active_low") is True)
check("keeps INPUT direction", tr.get("pins", {}).get("button", {}).get("direction") == "input")
check("collects variables", tr.get("variables") == ["n"], str(tr.get("variables")))
c = tr.get("c", "")
check("emits push-pull setup", "P1M0 |=  0x01" in c)
check("active-low off state", "P1_0 = 1;" in c)
check("REPEAT becomes a for loop", "for (_i" in c)
check("IF becomes an if", "if (n > 5)" in c)

result, elapsed = post({"code": PSEUDO, "language": "pseudocode"})
check("pseudocode compiles to an image", result.get("success") is True,
      result.get("error", "")[:60])
check("returns the generated C too", bool(result.get("c")))
image, errors = code_bytes(base64.b64decode(result["base64"]).decode())
check("image checksums valid", not errors)
check("reload matches CLOCK", reload_value(image) == 65536 - 11059200 // 12 // 1000)

hi = post({"code": PSEUDO.replace("CLOCK 11059200", "CLOCK 22118400"),
           "language": "pseudocode", "fosc": 11059200})[0]
image, _ = code_bytes(base64.b64decode(hi["base64"]).decode())
check("CLOCK overrides the fosc field",
      reload_value(image) == 65536 - 22118400 // 12 // 1000)

print("\npseudocode errors point at a line")
for source, line_no, expect in [
    ("WHEN started:\n  wobble the thing", 2, "do not understand"),
    ("WHEN started:\n  turn on ghost", 2, "unknown pin"),
    ("PIN b = P3.2 INPUT\nWHEN started:\n  turn on b", 3, "cannot be driven"),
    ("PIN a = P1.0 OUTPUT\n", 1, "no 'WHEN started:'"),
]:
    result, _ = post({"code": source, "language": "pseudocode"})
    ok = (result.get("success") is False and result.get("line") == line_no
          and expect in result.get("error", ""))
    check(f"{expect[:24]!r}", ok,
          f"line {result.get('line')} (want {line_no}): {result.get('error','')[:44]}")

check("unknown language rejected",
      post({"code": "x", "language": "cobol"})[0].get("error", "").startswith("unknown language"))

print("\nUI exposes the front end")
check("language picker", "id=language" in page and 'value=pseudocode' in page)
check("C tab for generated source", 'data-pane=cgen' in page)

print("\nprocedures, analog and the other loops")
FULL = """DEVICE STC12C5A60S2:
  CLOCK 11059200
  PIN led = P1.7 OUTPUT ACTIVE LOW
  PIN pot = P1.2 ANALOG
  PIN btn = P3.2 INPUT ACTIVE LOW

  DEFINE flash (times) (ms):
    REPEAT times:
      turn on led
      wait ms ms
      turn off led
      wait ms ms

  WHEN started:
    wait until btn
    FOREVER:
      set level to pot
      IF level > 512 THEN:
        flash 3, 80
      ELSE:
        flash 1, 400
      REPEAT UNTIL btn:
        wait 50 ms
"""
req = urllib.request.Request(f"{BASE}/transpile", json.dumps({"code": FULL}).encode(),
                             {"Content-Type": "application/json"})
with urllib.request.urlopen(req, timeout=120) as response:
    tr = json.load(response)
check("full program transpiles", tr.get("success") is True, tr.get("error", "")[:60])
c = tr.get("c", "")
check("procedure forward-declared", "static void bw_flash(int times, int ms);" in c)
check("procedure defined", "static void bw_flash(int times, int ms)\n{" in c)
check("call emitted", "bw_flash(3, 80);" in c)
check("parameters stay local", tr.get("variables") == ["level"], str(tr.get("variables")))
check("ADC helper emitted", "static unsigned int adc_read" in c)
check("analog pin -> channel", "adc_read(2)" in c)
check("P1ASF set for the channel", "P1ASF = 0x04" in c)
check("analog pin set high-impedance", "P1M1 |=  0x04" in c)
check("wait until -> spin", "while (!(!P3_2)) ;" in c)
check("REPEAT UNTIL -> negated while", "while (!(!P3_2)) {" in c)

result, _ = post({"code": FULL, "language": "pseudocode"})
check("full program compiles", result.get("success") is True, result.get("error", "")[:60])
image, errors = code_bytes(base64.b64decode(result["base64"]).decode())
check("image is valid", not errors and len(image) > 200, f"{len(image)} bytes")
check("no compiler warnings", not (result.get("log") or "").strip(),
      (result.get("log") or "").strip()[:60])

for source, expect in [
    ("PIN p = P2.0 ANALOG\nWHEN started:\n  wait 1 ms", "ANALOG is only available"),
    ("DEFINE f (a):\n  wait 1 ms\nDEFINE f (b):\n  wait 1 ms\nWHEN started:\n  f 1",
     "defined twice"),
    ("DEFINE f (a):\n  wait 1 ms\nWHEN started:\n  f 1, 2", "takes 1 argument"),
]:
    result, _ = post({"code": source, "language": "pseudocode"})
    check(expect[:22], result.get("success") is False and expect in result.get("error", ""),
          result.get("error", "")[:56])

check("procedures may be called before they are defined",
      post({"code": "WHEN started:\n  beep\nDEFINE beep:\n  wait 1 ms",
            "language": "pseudocode"})[0].get("success") is True)

print("\ndisassembly")
result, _ = post({"code": FULL, "language": "pseudocode", "format": "ihx",
                  "disassemble": True})
asm = result.get("disassembly") or ""
check("returned with the image", bool(asm))
check("looks like a listing", bool(re.match(r"^0000  ", asm)), asm.splitlines()[0][:40])

req = urllib.request.Request(f"{BASE}/disassemble",
                             json.dumps({"base64": result["base64"]}).encode(),
                             {"Content-Type": "application/json"})
with urllib.request.urlopen(req, timeout=120) as response:
    standalone = json.load(response)
check("/disassemble agrees with the inline one",
      standalone.get("disassembly") == asm)
check("reports instruction count", standalone.get("instructions", 0) > 50,
      str(standalone.get("instructions")))
check("rejects a corrupt image",
      json.load(urllib.request.urlopen(urllib.request.Request(
          f"{BASE}/disassemble", json.dumps({"hex": ":deadbeef"}).encode(),
          {"Content-Type": "application/json"}), timeout=60)).get("success") is False)

print("\nround-trip")
first = post({"code": FULL, "language": "pseudocode", "format": "ihx"})[0]["base64"]
second = post({"code": FULL, "language": "pseudocode", "format": "ihx"})[0]["base64"]
check("same source compiles to the same image", first == second)

blink = post({"code": PSEUDO, "language": "pseudocode", "format": "ihx",
              "disassemble": True})[0]["disassembly"]
for label, needle in [("timer reload low byte", "MOV   TL0,#0x67"),
                      ("timer reload high byte", "MOV   TH0,#0xFC"),
                      ("push-pull port mode", "ORL   P1M0,#0x01"),
                      ("LED driven low", "CLR   P1.0"),
                      ("LED driven high", "SETB  P1.0"),
                      ("delay polls the timer flag", "JNB   TF0,")]:
    check(f"survives to machine code: {label}", needle in blink)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
