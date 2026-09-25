#!/usr/bin/env python3
"""
test-arduino-run — Arduino C++ sketches built by this service RUN, and do
what they say.

test_arduino_cpp.py proves the `arduino` route compiles and links. That is
not the claim a user cares about: a HardwareSerial that links but never
transmits, a String that concatenates into garbage, or a core built for the
wrong F_CPU all link perfectly. So each sketch here is built through
`compile_source` -- the endpoint's own code path -- and executed in avr8js
(scripts/avr8js-run.mjs), and the assertions are on what came out of the
simulated chip: the bytes USART0 sent, and how often a pin changed.

Timing is asserted too, because it is the one thing a wrong clock changes and
nothing else does: the service once built every sketch that omitted `fosc`
for 11.0592 MHz, and on a 16 MHz board those images ran -- with every
delay() 31% short.

Needs node and avr8js (`npm install --no-save avr8js@0.21.1`). Only the
ATmegas run here: avr8js models their USART and Timer 0 at the addresses the
core uses. The ATtiny images are compile-checked in test_arduino_cpp.py.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import app  # noqa: E402
import arduino_build  # noqa: E402

RUNNER = os.path.join(ROOT, "scripts", "avr8js-run.mjs")

checks = failures = 0


def ok(cond, label, detail=""):
    global checks, failures
    checks += 1
    if not cond:
        failures += 1
    mark = "\x1b[32mok \x1b[0m" if cond else "\x1b[31mFAIL\x1b[0m"
    print(f"  {mark} {label}" + (f"   {detail}" if detail else ""))


def build(code, language="arduino", target="atmega328p"):
    return asyncio.run(app.compile_source(app.CompileReq(
        code=code, language=language, target=target)))


def run(result, target, mcu, ms, peek=""):
    """Simulate at the BOARD's crystal, never at the clock the response
    reports. Using the response's f_cpu made a mis-clocked image
    self-consistent -- built for 11 MHz, simulated at 11 MHz, every delay
    exactly right -- so the timing check could not fail. (Verified: that
    version stayed green with the 11.0592 MHz bug put back.)"""
    clock = arduino_build.BOARDS[target]["default_clock"]
    with tempfile.NamedTemporaryFile("w", suffix=".hex", delete=False) as fh:
        fh.write(base64.b64decode(result["base64"]).decode())
        path = fh.name
    try:
        out = subprocess.run(["node", RUNNER, path, mcu, str(ms), str(clock), peek],
                             capture_output=True, text=True, timeout=300, cwd=ROOT)
    finally:
        os.unlink(path)
    if out.returncode != 0:
        raise RuntimeError(out.stderr.strip() or "avr8js-run failed")
    return json.loads(out.stdout)


# --------------------------------------------------------------- C++ objects

CPP = """
class Counter {
 public:
  explicit Counter(int start) : n(start) {}
  int next() { return n++; }
 private:
  int n;
};

template <typename T> T twice(T x) { return x + x; }

Counter counter(20);
String label = "n";

void setup() {
  Serial.begin(115200);
  Serial.println(F("hello from C++"));
  pinMode(LED_BUILTIN, OUTPUT);
}

void loop() {
  report(twice(counter.next()));      // called before it is defined
  digitalWrite(LED_BUILTIN, !digitalRead(LED_BUILTIN));
  delay(10);
}

void report(int v) {
  String s = label + "=" + String(v) + " pi=" + String(3.14159, 3);
  Serial.println(s);
}
"""

for target, mcu, led in (("arduino-uno", "atmega328p", "PB5"),
                         ("atmega168p", "atmega168p", "PB5"),
                         ("arduino-mega", "atmega2560", "PB7")):
    print(f"\n--- {target}: Serial, String, a class, a template ---")
    r = build(CPP, target=target)
    ok(r.get("success"), f"{target}: builds", (r.get("error") or "")[:200])
    if not r.get("success"):
        continue
    sim = run(r, target, mcu, 200)
    lines = sim["serial"].split("\r\n")
    ok(lines[0] == "hello from C++", f"{target}: F() string over Serial", repr(lines[:1]))
    ok(lines[1:3] == ["n=40 pi=3.142", "n=42 pi=3.142"],
       f"{target}: String + float + class + template", repr(lines[1:3]))
    # 200 ms of delay(10) is about 20 passes; an image built for 11.0592 MHz
    # makes 24 (measured, with that bug put back).
    passes = sum(1 for line in lines if line.startswith("n="))
    ok(17 <= passes <= 21, f"{target}: timing is right on a {r['f_cpu']} Hz board",
       f"{passes} passes in 200 ms")
    ok(sim["toggles"].get(led, 0) >= 17, f"{target}: LED_BUILTIN ({led}) blinks",
       str(sim["toggles"].get(led)))

# ------------------------------------------------------------------ libraries

print("\n--- EEPROM library round-trip ---")
r = build("""
#include <EEPROM.h>
void setup() {
  Serial.begin(115200);
  EEPROM.write(3, 42);
  Serial.print("eeprom=");
  Serial.println(EEPROM.read(3));
}
void loop() {}
""")
ok(r.get("success") and r.get("libraries") == ["EEPROM"], "EEPROM sketch builds",
   (r.get("error") or str(r.get("libraries")))[:200])
if r.get("success"):
    sim = run(r, "atmega328p", "atmega328p", 30)
    ok("eeprom=42" in sim["serial"], "EEPROM reads back what it wrote", repr(sim["serial"]))

# ------------------------------------------------------------------ pseudocode

print("\n--- pseudocode on an Arduino board: emitted core C++, built, run ---")
r = build("""DEVICE ARDUINO-UNO:
  PIN led = D13 OUTPUT
  WHEN started:
    FOREVER:
      toggle led
      wait 50 ms
""", language="pseudocode")
ok(r.get("success"), "DEVICE ARDUINO-UNO builds (it was transpile-only)",
   (r.get("error") or "")[:200])
if r.get("success"):
    sim = run(r, "arduino-uno", "atmega328p", 500)
    n = sim["toggles"].get("PB5", 0)
    ok(9 <= n <= 11, "toggle every 50 ms: about 10 edges in 500 ms", str(n))

# ---------------------------------------------------------------- symbols

print("\n--- a sketch's symbol table points at the live variables ---")
SYM = """
unsigned int ticks = 1000;
String greeting = "hi";
void setup() { Serial.begin(115200); }
void loop() { ticks++; delay(10); }
"""
r = asyncio.run(app.compile_source(app.CompileReq(
    code=SYM, language="arduino", target="arduino-uno", symbols=True)))
table = r.get("symbols") or {}
by_name = {v["name"]: v for v in table.get("variables", [])}
ok(r.get("success") and "ticks" in by_name and "greeting" in by_name,
   "the table lists the sketch's globals", str(sorted(by_name)))
ok({"setup", "loop"} <= {f["name"] for f in table.get("functions", [])},
   "and its functions", str([f["name"] for f in table.get("functions", [])]))
if "ticks" in by_name:
    v = by_name["ticks"]
    sim = run(r, "arduino-uno", "atmega328p", 200, f"{v['addr']}:{v['size']}")
    # 200 ms of `ticks++; delay(10)` from 1000: about 1019. Read from the
    # RUNNING chip at the table's address -- a wrong address reads garbage.
    ok(1010 <= sim["peeked"][0] <= 1025, "the running chip holds `ticks` at that address",
       f"read {sim['peeked'][0]} at 0x{v['addr']:x}")

print(f"\n{checks - failures}/{checks} checks passed")
sys.exit(1 if failures else 0)
