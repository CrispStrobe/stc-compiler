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

print("\nthe source cannot read files outside the build")
# A compiler quotes the offending line back in its diagnostics, and this
# service returns that output -- so an unrestricted #include turns a compile
# endpoint into a file-read primitive. Confirmed against a deployment with
# /etc/os-release before the check existed.
for probe, why in [('#include "/etc/os-release"\nvoid main(void){}', "an absolute path"),
                   ('#include "/proc/self/environ"\nvoid main(void){}', "the environment"),
                   ('#include "../../etc/passwd"\nvoid main(void){}', "a parent traversal")]:
    result, _ = post({"code": probe})
    check(f"refuses {why}",
          result.get("success") is False
          and "outside the build directory" in (result.get("error") or ""),
          (result.get("error") or "")[:56])
# And the way round the first fix: an include-path flag moves where a
# RELATIVE include resolves, so the source looks innocent.
for opts, why in [(["-I/etc"], "-I with a path"),
                  (["--include-dir=/etc"], "a long-form include dir"),
                  (["-L/usr/lib"], "a library path")]:
    result, _ = post({"code": '#include "os-release"\nvoid main(void){}', "options": opts})
    check(f"refuses {why} in options",
          result.get("success") is False
          and "cannot contain a path" in (result.get("error") or ""),
          (result.get("error") or "")[:56])
result, _ = post({"code": SOURCE, "options": ["--opt-code-size"]})
check("a flag without a path still works", result.get("success") is True,
      (result.get("error") or "")[:56])

for allowed, why in [('#include <stc12.h>\nvoid main(void){ P1 = 0; for(;;); }', "a system header"),
                     ('#include "nope.h"\nvoid main(void){}', "a relative header (fails to compile, but is not refused)")]:
    result, _ = post({"code": allowed})
    check(f"still allows {why}",
          "outside the build directory" not in (result.get("error") or ""),
          (result.get("error") or "")[:56])

print("\nevery endpoint bounds its input")
# /compile and /translate-project always did. The parse-only endpoints did
# not, on the reasoning that parsing is cheap -- which is true per byte and
# not true per request, in a metered function with a memory ceiling.
oversize = "x" * (1_000_001)
for path, body in [("/disassemble", {"hex": oversize}),
                   ("/decompile", {"code": oversize}),
                   ("/transpile", {"code": oversize}),
                   ("/translate", {"code": oversize})]:
    request = urllib.request.Request(f"{BASE}{path}", json.dumps(body).encode(),
                                     {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            data = json.load(response)
        check(f"{path} refuses an oversize body",
              data.get("success") is False and "too large" in (data.get("error") or ""),
              (data.get("error") or "")[:48])
    except urllib.error.HTTPError as exc:
        check(f"{path} refuses an oversize body", exc.code in (413, 400), f"HTTP {exc.code}")

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
# `{ }` and not a bare `;`: an empty statement after a while clause is what
# GCC's -Wmisleading-indentation fires on, because the next generated line is
# indented as though the loop guarded it. SDCC never warned, so this only
# surfaced once the AVR target compiled the same lowering.
check("wait until -> spin", "while (!(!P3_2)) { }" in c)
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

print("\nkeil")
KEIL = """#include <STC12C5A60S2.H>
sbit LED = P1^0;
sbit K1  = P3^2;sbit K2 = P3^3;
data bit flag;
unsigned char code pattern[2][2] = {1,2,3,4};
void main(void)
{
    LED = 0;
    while (1) { if (!K1) flag = 1; }
}
"""
req = urllib.request.Request(f"{BASE}/translate",
                             json.dumps({"code": KEIL, "language": "keil"}).encode(),
                             {"Content-Type": "application/json"})
with urllib.request.urlopen(req, timeout=120) as response:
    translated = json.load(response)
c = translated.get("c") or ""
check("sbit resolved via SDCC's table", "__sbit __at (0x90) LED;" in c)
check("second sbit on one line resolved", "__at (0xB3) K2;" in c)
check("data bit collapses to __bit", "__bit flag;" in c)
check("flat 2D initialiser re-braced", "{{1, 2}, {3, 4}}" in c)
check("vendor include mapped", "keil-stc12.h" in c)

result, _ = post({"code": KEIL, "language": "keil"})
check("keil source compiles to an image", result.get("success") is True,
      result.get("error", "")[:70])

ISR_MAIN = "#include <REG52.H>\nvoid main(void) { EA = 1; while (1) ; }\n"
ISR_FILE = ("#include <REG52.H>\nunsigned char ticks;\n"
            "void tick(void) interrupt 1 using 2 { ticks++; }\n")
req = urllib.request.Request(
    f"{BASE}/translate-project",
    json.dumps({"files": {"main.c": ISR_MAIN, "timer.c": ISR_FILE},
                "link": True, "format": "ihx"}).encode(),
    {"Content-Type": "application/json"})
with urllib.request.urlopen(req, timeout=120) as response:
    project = json.load(response)
check("project translates and links", project.get("success") is True,
      str(project.get("error", ""))[:70])
check("ISR prototype injected into main()'s file",
      any("tick" in note for note in project.get("isr_injected") or []),
      str(project.get("isr_injected")))
if project.get("success"):
    image, errors = code_bytes(base64.b64decode(project["base64"]).decode())
    check("linked image is valid hex", not errors and len(image) > 20,
          f"{len(image)} bytes")
    # Vector 1 (Timer 0) lives at 0x000B; SDCC only emits it when the
    # injected prototype made the handler visible -- the whole point.
    check("timer-0 vector emitted at 0x000B", image.get(0x0B) is not None
          and image.get(0x0B) != 0xFF, hex(image.get(0x0B, -1)))

print("\nscheduler and families")
TWO_SCRIPTS = """DEVICE STC12C5A60S2:
CLOCK 11059200
PIN led1 = P1.0 OUTPUT ACTIVE LOW
PIN led2 = P1.1 OUTPUT ACTIVE LOW
WHEN started:
  FOREVER:
    toggle led1
    wait 300 ms
WHEN started:
  FOREVER:
    toggle led2
    wait 700 ms
"""
result, _ = post({"code": TWO_SCRIPTS, "language": "pseudocode", "format": "ihx"})
check("two WHEN scripts compile", result.get("success") is True,
      result.get("error", "")[:70])
if result.get("success"):
    image, errors = code_bytes(base64.b64decode(result["base64"]).decode())
    check("scheduler tick vector at 0x000B", image.get(0x0B) is not None)
    check("generated C has two task machines",
          (result.get("c") or "").count("bw_task") >= 2)

STC89 = """DEVICE STC89C52RC:
CLOCK 11059200
PIN led = P1.0 OUTPUT ACTIVE LOW
WHEN started:
  FOREVER:
    toggle led
    wait 500 ms
"""
result, _ = post({"code": STC89, "language": "pseudocode",
                  "target": "stc89c52rc", "format": "ihx"})
check("STC89 target + device compiles", result.get("success") is True,
      result.get("error", "")[:70])
if result.get("success"):
    c = result.get("c") or ""
    check("STC89 code avoids STC12-only registers",
          "P1M0" not in c and "AUXR" not in c and "8052.h" in c)

result, _ = post({"code": "#include <STC15F2K60S2.h>\nvoid main(void)"
                          "{ T2L = 0x8F; T2H = 0xFD; while (1); }",
                  "language": "keil", "target": "stc15f2k60s2"})
check("STC15 Keil family (T2 at 0xD6/0xD7) compiles",
      result.get("success") is True, result.get("error", "")[:70])

result, _ = post({"code": "#include <reg52.h>\nvoid d(unsigned int t)"
                          "{ unsigned char j; while(t--) { j=200; while(--j); } }\n"
                          "void main(void){ d(100); while(1); }",
                  "language": "keil"})
check("timing lint flags busy-wait loops",
      any("busy-wait" in w for w in result.get("warnings") or []),
      str(result.get("warnings"))[:70])

print("\narchitectures beyond the 8051")

MICROBIT = """DEVICE MICROBIT:
  PIN led = P0 OUTPUT
  PIN btn = BUTTON_A INPUT
  WHEN started:
    FOREVER:
      toggle led
      wait 200 ms
  WHEN started:
    FOREVER:
      wait until btn
      wait 50 ms
"""
AVR = """DEVICE ATMEGA328P:
  CLOCK 16000000
  PIN led = D13 OUTPUT
  WHEN started:
    FOREVER:
      toggle led
      wait 500 ms
"""

result, _ = post({"code": AVR, "language": "pseudocode"})
check("AVR: pseudocode compiles to an image", result.get("success") is True,
      result.get("error", "")[:70])
check("AVR: reports its toolchain and part",
      result.get("toolchain") == "avr-gcc" and result.get("mcu") == "atmega328p")
if result.get("success"):
    image = base64.b64decode(result["base64"]).decode()
    check("AVR: Intel HEX with an EOF record",
          image.startswith(":") and image.strip().endswith(":00000001FF"))

result, _ = post({"code": MICROBIT, "language": "pseudocode"})
check("micro:bit: /compile refuses and says why",
      result.get("success") is False and "uflash" in (result.get("error") or ""),
      (result.get("error") or "")[:60])
check("micro:bit: the source comes back anyway",
      "from microbit import *" in (result.get("c") or ""))
check("micro:bit: cooperative tasks are generators",
      (result.get("c") or "").count("yield") >= 2)

request = urllib.request.Request(
    f"{BASE}/transpile", json.dumps({"code": MICROBIT}).encode(),
    {"Content-Type": "application/json"})
with urllib.request.urlopen(request, timeout=120) as response:
    tr = json.load(response)
check("micro:bit: /transpile labels the language", tr.get("language") == "python",
      str(tr.get("language")))
check("micro:bit: pins report a neutral location",
      tr.get("pins", {}).get("btn", {}).get("where") == "BUTTON_A",
      str(tr.get("pins", {}).get("btn")))

# /download hands back the SOURCE for a target that cannot be built here --
# a .py or a .ino is the deliverable, not a 400.
for source, want_name, want_tool in ((MICROBIT, "main.py", "uflash"),
                                     (EXAMPLE_ARDUINO := """DEVICE ARDUINO-UNO:
  PIN led = D13 OUTPUT
  WHEN started:
    turn on led
""", "main.ino", "arduino-cli")):
    request = urllib.request.Request(
        f"{BASE}/download", json.dumps({"code": source, "language": "pseudocode"}).encode(),
        {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            disposition = response.headers.get("Content-Disposition", "")
            tool = response.headers.get("X-Source-Only", "")
            body = response.read()
        check(f"/download returns {want_name}",
              want_name in disposition and tool == want_tool and len(body) > 50,
              f"{disposition} {tool} {len(body)}B")
    except urllib.error.HTTPError as exc:
        check(f"/download returns {want_name}", False, f"HTTP {exc.code}")

check("the UI offers the AVR parts",
      "atmega328p" in page and "optgroup" in page)
# Without this branch the browser shows "failed" for a micro:bit or an Arduino
# and leaves Download disabled, so the generated source is unreachable from
# the page the service is served on.
# NAME decides the filename, and the strict character set matters because the
# string is echoed into a Content-Disposition header.
NAMED = """DEVICE ARDUINO-UNO:
  NAME blink
  PIN led = D13 OUTPUT
  WHEN started:
    turn on led
"""
request = urllib.request.Request(
    f"{BASE}/download", json.dumps({"code": NAMED, "language": "pseudocode"}).encode(),
    {"Content-Type": "application/json"})
try:
    with urllib.request.urlopen(request, timeout=120) as response:
        check("NAME renames the download", 'filename="blink.ino"'
              in response.headers.get("Content-Disposition", ""),
              response.headers.get("Content-Disposition", ""))
except urllib.error.HTTPError as exc:
    # A failing check must stay a check. Letting the HTTPError out aborts the
    # whole suite and hides every test after it, which is how one unsupported
    # feature on an older deployment looks like total collapse.
    check("NAME renames the download", False, f"HTTP {exc.code}")

result, _ = post({"code": NAMED.replace("ARDUINO-UNO", "ATMEGA328P")
                             .replace("turn on led", "turn on led"),
                  "language": "pseudocode"})
check("NAME renames a compiled image too",
      result.get("success") is True and result.get("filename") == "blink.hex",
      str(result.get("filename")))

result, _ = post({"code": NAMED.replace("NAME blink", "NAME ../../etc/passwd"),
                  "language": "pseudocode"})
check("a path traversal in NAME is refused",
      result.get("success") is False and "do not understand" in (result.get("error") or ""),
      (result.get("error") or "")[:60])

check("the UI treats source-only as a result, not a failure",
      "source only, needs" in page and "data.c && data.toolchain" in page)

# ---- Arduino (ATTinyCore) language route -------------------------------------
print("\n--- Arduino (ATTinyCore) language route ---")

ARDUINO_BLINK = """\
void setup() {
  pinMode(LED_BUILTIN, OUTPUT);
}
void loop() {
  digitalWrite(LED_BUILTIN, HIGH);
  delay(500);
  digitalWrite(LED_BUILTIN, LOW);
  delay(500);
}
"""

# ATtiny88 blink
result, _ = post({"code": ARDUINO_BLINK, "language": "arduino",
                   "target": "attiny88", "fosc": 8000000})
check("arduino attiny88 blink compiles",
      result.get("success") is True,
      str(result.get("error", ""))[:100] if not result.get("success") else "")
if result.get("success"):
    check("arduino attiny88 toolchain is avr-gcc+ATTinyCore",
          result.get("toolchain") == "avr-gcc+ATTinyCore",
          result.get("toolchain"))
    check("arduino attiny88 echoes mcu",
          result.get("mcu") == "attiny88", result.get("mcu"))
    check("arduino attiny88 echoes f_cpu",
          result.get("f_cpu") == 8000000, str(result.get("f_cpu")))
    hex_text = base64.b64decode(result["base64"]).decode()
    check("arduino attiny88 hex is valid Intel HEX",
          hex_text.startswith(":") and hex_text.strip().endswith(":00000001FF"))
    image, errs = code_bytes(hex_text)
    check("arduino attiny88 hex checksums pass",
          len(errs) == 0, f"{len(errs)} errors" if errs else "")
    check("arduino attiny88 image is reasonably sized",
          200 < len(image) < 4096,
          f"{len(image)} bytes")
    check("arduino attiny88 memory report present",
          "attiny88" in (result.get("memory") or "").lower(),
          (result.get("memory") or "")[:80])

# ATtiny85 blink
result, _ = post({"code": ARDUINO_BLINK, "language": "arduino",
                   "target": "attiny85", "fosc": 8000000})
check("arduino attiny85 blink compiles",
      result.get("success") is True,
      str(result.get("error", ""))[:100] if not result.get("success") else "")
if result.get("success"):
    check("arduino attiny85 echoes mcu",
          result.get("mcu") == "attiny85", result.get("mcu"))
    check("arduino attiny85 echoes f_cpu",
          result.get("f_cpu") == 8000000, str(result.get("f_cpu")))
    hex85 = base64.b64decode(result["base64"]).decode()
    img85, errs85 = code_bytes(hex85)
    check("arduino attiny85 hex checksums pass",
          len(errs85) == 0, f"{len(errs85)} errors" if errs85 else "")

# Wrong target: arduino + atmega328p should fail with a clear message
result, _ = post({"code": ARDUINO_BLINK, "language": "arduino",
                   "target": "atmega328p"})
check("arduino rejects non-ATtiny target",
      result.get("success") is False and "ATtiny" in (result.get("error") or ""),
      (result.get("error") or "")[:80])

# Arduino with explicit #include
ARDUINO_EXPLICIT = '#include <Arduino.h>\n' + ARDUINO_BLINK
result, _ = post({"code": ARDUINO_EXPLICIT, "language": "arduino",
                   "target": "attiny88"})
check("arduino with explicit #include compiles",
      result.get("success") is True,
      str(result.get("error", ""))[:100] if not result.get("success") else "")

# Custom F_CPU via fosc parameter (1 MHz internal — valid for all ATtinys)
result, _ = post({"code": ARDUINO_BLINK, "language": "arduino",
                   "target": "attiny85", "fosc": 1000000})
check("arduino custom f_cpu echoed",
      result.get("success") is True and result.get("f_cpu") == 1000000,
      str(result.get("f_cpu")))

# Health check reports arduino_targets
try:
    with urllib.request.urlopen(f"{BASE}/health", timeout=30) as response:
        health = json.load(response)
    check("health reports arduino_targets",
          "attiny85" in health.get("arduino_targets", {}),
          str(list(health.get("arduino_targets", {}).keys())))
except Exception as exc:
    check("health reports arduino_targets", False, str(exc))

# UI offers Arduino language option
check("the UI offers Arduino language",
      "arduino" in page.lower() and "ATtiny" in page)

print(f"\n{passed} passed, {failed} failed")
sys.exit(1 if failed else 0)
