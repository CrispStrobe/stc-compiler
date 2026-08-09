# stc-compiler

A serverless REST API that compiles **C to Intel HEX for the STC12C5A60S2** and
other 8051 parts, using [SDCC](https://sdcc.sourceforge.net/). POST source, get
back an image ready to hand to [`stcgal`](https://github.com/grigorig/stcgal) —
or to a browser that speaks the ISP protocol itself.

Deliberately shaped like
[`CrispStrobe/legacy-lego-compiler`](https://github.com/CrispStrobe/legacy-lego-compiler),
which does the same job for LEGO NXT and EV3 bytecode, so client code is nearly
identical between them.

---

## Who calls this

The compile side of the STC12 work in
[`CrispStrobe/stc12c5a60s2-lab`](https://github.com/CrispStrobe/stc12c5a60s2-lab),
and eventually the BrickWright block-to-8051 back-end: the browser generates C,
POSTs it here, gets back a `.hex`, and flashes it over Web Serial.

It is a separate deployment from `legacy-lego-compiler` on purpose. SDCC is
GPL-2.0-or-later; that repository's story is MIT plus MPL/BSD, and there is no
reason to entangle the two. See [`NOTICE.md`](NOTICE.md) for exactly what the
GPL does and does not reach here — short version: the wrapper stays MIT, and
your compiled `.hex` is yours, thanks to SDCC's linking exception.

---

## API

### `POST /compile`

```jsonc
{
  "code":    "#include <stc12.h>\nvoid main(void) { ... }",  // required
  "target":  "stc12c5a60s2",   // stc12c5a60s2 | stc12c5a16s2 | mcs51
  "fosc":    11059200,         // emitted as -DFOSC_HZ=11059200UL; null to omit
  "defines": { "DEBUG": "1" }, // extra -D flags; null value = bare define
  "options": ["--opt-code-size"], // raw SDCC flags
  "format":  "hex"             // hex (packihx) | ihx (raw) | bin (makebin)
}
```

Success:

```jsonc
{
  "success":  true,
  "base64":   "OjAzMDAwMDAwMDIw...",  // the image
  "filename": "main.hex",
  "bytes":    308,
  "log":      "",
  "memory":   "Internal RAM layout:\n..."  // SDCC's memory map
}
```

Failure returns `{"success": false, "error": "<compiler output>"}`.

The `memory` field is worth surfacing to users — it is how you catch an image
quietly outgrowing 60 KB of flash or 248 bytes of stack.

### `POST /disassemble`

Intel HEX in (as `hex` text or `base64`), 8051 assembly out. `POST /compile`
also takes `"disassemble": true` to return the listing alongside the image.

```
0000  02 00 06     LJMP  0x0006
0006  75 81 09     MOV   SP,#0x09
00B7  75 8A 67     MOV   TL0,#0x67
00BA  75 8C FC     MOV   TH0,#0xFC
00BD  C2 8D        CLR   TF0
00BF  D2 8C        SETB  TR0
00C1  30 8D FD     JNB   TF0,0x00C1
```

SFR and bit addresses are resolved to names, so `MOV TL0,#0x67` reads as itself
rather than `MOV 0x8A,#0x67`.

**The table is checked against SDCC's own assembler**, not hand-eyeballed:
`scripts/test-disasm.py <build-dir>` parses SDCC's relocated listing (`.rst`),
which pairs every emitted byte with the mnemonic `sdas8051` assembled it from,
and fails if we decode the same bytes differently or desynchronise on a wrong
instruction length. Verified across four programs, 90 distinct opcodes.

Linear sweep, not a control-flow trace — right for comparing against a compiler
listing, but data embedded in the code stream would decode as nonsense.

### `POST /translate-project`

A whole Keil project in one request: `{"files": {"path": "content", ...}}`.
Every `.c`/`.h` is translated with a shared header map; ISR prototypes are
injected into `main()`'s file (SDCC only emits a vector when the handler is
visible there — Keil links vectors separately, so per-file translation
compiles cleanly and the interrupt never fires); externs whose definitions
differ only in case are unified (BL51 links case-insensitively, sdld does
not); `_at_` addresses are stamped onto their extern declarations; and a
`.uvproj`, when present, selects the real source list and the memory model.
With `"link": true` the response also carries one linked, flashable image.
The browser UI's **Keil project…** button is this endpoint.

### `POST /decompile`

Pseudocode in, canonical pseudocode out — `parse` followed by
`emit_pseudocode`. Normalised layout, comments dropped, and a fixed point:
feeding the result back in returns it unchanged.

### `POST /transpile`

Pseudocode in, C out, no compiler involved — for seeing exactly what the front
end produced. Returns `c`, plus the resolved `part`, `clock`, `pins` and
`variables`. Errors carry a `line`.

### `POST /download`

Identical request body, but returns the **raw file** with a filename attached
instead of base64 inside JSON — so `curl` can just save it:

```bash
curl -X POST https://stc-compiler.vercel.app/download \
     -H 'Content-Type: application/json' \
     -d '{"code": "#include <stc12.h>\nvoid main(void) { P1 = 0; for(;;); }"}' \
     -OJ                       # -OJ takes the name from Content-Disposition
# -> main.hex
```

For a target this service cannot build — a micro:bit, an Arduino sketch — the
**source** is the file: `main.py` or `main.ino`, returned 200 with
`X-Source-Only` naming the toolchain it would have needed. A genuine error (a
bad pin, an unknown target) is still a 400 with the message. The difference is
whether there is usable output, not whether a compiler ran.

Response headers: `Content-Disposition: attachment; filename="main.hex"`,
`X-Image-Bytes`, and `Access-Control-Expose-Headers` so a browser `fetch()` can
read both. A compile failure returns **400** with the compiler output as plain
text.

### `GET /health`

Reports the SDCC version and the known targets. Also the cheapest way to see
whether a cold start staged the toolchain correctly.

### `GET /`

A small browser UI with the blink example preloaded — no build step, no CDN.
Pick a target, clock and output format, compile with the button or
<kbd>Cmd</kbd>/<kbd>Ctrl</kbd>+<kbd>Enter</kbd>, then **Download** the image or
**Copy** it to the clipboard. Output, SDCC's memory map and the build log are
on separate tabs; `.bin` is shown as a hex dump rather than mojibake.

**About dialog** — an *About* link in the header opens a modal listing every
licence (this wrapper MIT; SDCC GPL-2.0-or-later; its runtime headers with the
linking exception, quoted in full), the corresponding-source pointer, links to
the related CrispStrobe repositories, a warranty and hardware disclaimer, a
non-affiliation statement, what happens to submitted source, and the imprint.

### Imprint / Impressum

The operator details in the About dialog come from environment variables, so a
real postal address is never committed to a public repository and a fork
deploying its own instance does not publish someone else's details:

```bash
vercel env add IMPRINT_NAME production      # e.g. Jane Doe
vercel env add IMPRINT_ADDRESS production   # multi-line is fine
vercel env add IMPRINT_EMAIL production
vercel --prod                               # redeploy to pick them up
```

If none are set the section is omitted entirely rather than rendering a
placeholder. Note that a publicly reachable service operated from Germany
generally needs one (§ 5 DDG, formerly TMG).

### `GET /docs`

FastAPI's generated OpenAPI documentation.

---

## Keil C51

Set `"language": "keil"` and the body is translated from the Keil dialect into
SDCC's before compiling. `POST /translate` returns just the translated C.

Nearly every STC12 project in the wild is a Keil µVision project, and SDCC
rejects the dialect outright, so a large body of existing code is otherwise
unreachable from an open toolchain. The two disagree on syntax, not semantics.

| Keil | SDCC |
|---|---|
| `sbit LED = P1^0;` | `__sbit __at (0x90) LED;` |
| `sfr ADC_CONTR = 0xBC;` | `__sfr __at (0xBC) ADC_CONTR;` |
| `xdata` `idata` `pdata` `code` `data` | `__xdata` … (either side of the type) |
| `bit f(bit x)`, `(bit)y` | `__bit` |
| `void t(void) interrupt 1 using 2` | `__interrupt(1) __using(2)` |
| `int x _at_ 0x30;` | `__at (0x30) int x;` |
| `#include <STC12C5A60S2.H>`, `<reg52.h>` | our shim → SDCC's `<stc12.h>` |
| `_nop_`, `_crol_`, … | `keil-shim/intrins.h` |

`sbit` needs the SFR's address to turn `P1^0` into `0x90`. Those come from
SDCC's own headers plus any `sfr` the file declares itself — and the file's
own declarations are always kept: SDCC accepts an identical redeclaration
(only a differing address is an error), and in a project the file may be the
only place its translation unit gets that register from.

**Register families.** `reg51.h`/`reg52.h`/`REGX52.H` map onto SDCC's
8051.h/8052.h — reg52 code expects Timer 2, which the STC12 does not have —
plus generated STC89 extras. `stc15*.h` maps onto its own family header with
Timer 2 at the STC15's 0xD6/0xD7. The chip-ambiguous registers (`WDT_CONTR`
is 0xC1 on an STC12 and 0xE1 on an STC89; `P4` is 0xC0 vs 0xE8) are left to
the file's own declarations on purpose. All compat headers are **generated**
(`keil-shim/generate-compat.py`) from datasheet facts, never copied from a
vendor header.

**Beyond syntax, the translator also knows the traps:**

- *ISR vectors*: SDCC only emits one if the handler is visible in `main()`'s
  file; Keil links vectors separately. Single files get a warning;
  `POST /translate-project` injects the prototypes and fixes it.
- *Case*: Keil's BL51 links symbols case-insensitively, uVision resolves
  includes case-insensitively; project mode unifies both.
- *Timing*: a 1T part (STC12/STC15) dropped into a 12T socket (STC89) runs
  software delay loops ~6–12× too fast and breaks bit-banged I2C/1-wire.
  The translator flags busy-wait loops and `_nop_()` runs so the migration
  does not fail silently.

**Measured, not asserted: 546 of 597 Keil-dialect files (91%)** across 86
public 8051 projects (GitHub + Gitee) compile after translation — and on the
one project of the corpus whose SDCC conversion also exists hand-written,
the translator converts **31 of 31** programs. Whole projects link to a
flashable image via `/translate-project` (uVision `.uvproj` respected for
source list and memory model). Remaining failures are broken sources and
constructs that need a typed front end.

## Pseudocode

Set `"language": "pseudocode"` and the body goes through a BrickWright-style
front end first. The dialect follows the conventions already used by
[`sb3-creator`](https://github.com/CrispStrobe/sb3-creator)'s pseudocode —
UPPERCASE for structure and control flow, lowercase for statements,
indentation for nesting:

```
DEVICE STC12C5A60S2:
  CLOCK 11059200
  PIN led1 = P1.0 OUTPUT ACTIVE LOW
  PIN led2 = P1.1 OUTPUT ACTIVE LOW
  PIN button = P3.2 INPUT

  WHEN started:
    set counter to 0
    FOREVER:
      REPEAT 6:
        turn on led1
        turn off led2
        wait 0.15 seconds
        turn off led1
        turn on led2
        wait 0.15 seconds
      change counter by 1
      IF counter > 2 THEN:
        set counter to 0
```

| Form | Notes |
|---|---|
| `DEVICE <part>:` | optional wrapper, like `SPRITE Name:` |
| `NAME <word>` | what the generated file is called: `blink.ino`, `blink.hex` |
| `CLOCK <hz>` / `CLOCK <n> MHz` | overrides the `fosc` request field |
| `PIN <name> = P1.0 OUTPUT [ACTIVE LOW]` | `ACTIVE LOW` makes `turn on` drive 0 |
| `PIN <name> = P3.2 INPUT [ACTIVE LOW]` | readable in expressions |
| `PIN <name> = P1.2 ANALOG` | 10-bit ADC; channel *n* is on `P1.n`, so P1 only |

The location on the right of the `=` is whatever the `DEVICE` calls a pin, and
the device checks it: `P0.0`–`P4.7` on the 8051 parts, `D0`–`D13` and `A0`–`A5`
on an Arduino Uno. Asking for something the board cannot do is a parse error
naming the board, not a miscompile.

| `DEFINE <name> (a) (b):` | procedure; callable as `name 3, 80` or `name(3, 80)` |
| `WHEN started:` | also accepts `WHEN flag clicked:` |
| `FOREVER:` · `REPEAT n:` · `IF c THEN:` / `ELSE:` | indentation-scoped |
| `WHILE c:` · `REPEAT UNTIL c:` · `wait until c` | |
| `turn on/off <pin>` · `set <pin> high/low` · `toggle <pin>` | |
| `wait <n> seconds` / `<n> ms` | Timer 0, not a spin loop |
| `set <v> to <e>` · `change <v> by <e>` | variables are 16-bit `int` |
| `+ - * / %`, `= != < > <= >=`, `and or not` | `=` compares, as in Scratch |

Procedures may be called before they are defined — the order people actually
write in — because headers are registered in a first pass. Parameters are
locals, so they never leak into the global variable list.

`ANALOG` pins read through the ADC wherever they appear in an expression, so
`IF pot > 512 THEN:` just works: the emitter adds the polled `adc_read()`
helper, sets `P1ASF`, puts the pin in high-impedance mode and powers the
converter. Channel *n* lives on `P1.n`, which is why `ANALOG` is rejected on
any other port.

`ACTIVE LOW` is the point of the whole thing: a quasi-bidirectional 8051 pin
sinks 20 mA but sources ~230 µA, so LEDs get wired active-low and `turn on`
has to emit a `0`. The front end knows that, so the pseudocode never has to.

Response includes the generated `c` alongside the image, so nothing is hidden.

**Parsing builds an AST**, and both back ends walk it:

```
text ──parse──▶ Program (AST) ──emit_c─────────▶ C ──▶ SDCC ──▶ .hex
                      │
                      └────────emit_pseudocode─▶ text
```

That is the shape `sb3-creator` uses, where blocks are the IR and
`decompile(project)` walks it back — and it is what makes the round-trip
testable, because `parse` and `emit_pseudocode` have to be inverses.
`scripts/test-roundtrip.py` checks it the way `transparency.test.mjs` does:
every hop is compared against the **original**, not merely against the previous
hop, because a degraded output is a stable fixed point too. 429 checks over
twelve programs — including multi-script schedulers, three chip families, and one built specifically around operator precedence —
if the right operand of a binary node is not re-emitted one level tighter,
`a - (b - c)` silently comes back as `(a - b) - c`.

## Targets

| `target` | Applied limits |
|---|---|
| `stc12c5a60s2` | `--iram-size 256 --xram-size 1024 --code-size 61440` |
| `stc12c5a16s2` | as above, `--code-size 16384` |
| `stc89c52rc` | `--iram-size 256 --xram-size 256 --code-size 8192` (12T) |
| `stc15f2k60s2` | `--iram-size 256 --xram-size 1792 --code-size 61440` (1T) |
| `mcs51` | none — bring your own via `options` |
| `atmega328p` | avr-gcc, `-mmcu=atmega328p`, 32 KB flash |
| `atmega168p` | avr-gcc, `-mmcu=atmega168p`, 16 KB flash |

The 8051 targets compile `-mmcs51 --std-c99`; adding a part is three lines in
`TARGETS` in [`app.py`](app.py). The AVR targets route to a second toolchain
entirely (`AVR_TARGETS`), and `/compile` picks between them from the `DEVICE`
line for pseudocode, or the `target` field for hand-written C. `/health`
reports both, and reports the AVR side as absent rather than failing when the
bundle is not deployed.

The pseudocode front end is part-aware too: `DEVICE STC89C52RC:` emits code
without port-mode registers or the AUXR 1T bit, refuses `ANALOG` pins (no
ADC), and times everything off Timer 0 at FOSC/12 — which 12T and 1T cores
count identically, so the same program is timing-correct across families.
Several `WHEN started:` blocks compile to a cooperative scheduler (Timer-0
millisecond tick, one state machine per script, a yield at every wait and
every loop iteration — Scratch's own contract).

### Beyond the 8051

`DEVICE ARDUINO-UNO:` and `DEVICE ARDUINO-NANO:` emit Arduino core C++ —
`pinMode`/`digitalWrite`/`analogRead`, the script in `setup()`, cooperative
tasks dispatched from `loop()`. The scheduler contract survives the move
unchanged, because `millis()` *is* the millisecond tick the 8051 back end had
to build by hand; the Arduino target ships no runtime of its own at all. What
does change is the width of it: `millis()` is 32-bit, so the deadlines and the
wraparound compare widen with it, and that type comes from the target rather
than from the AST walker.

**Core C++ transpiles here; it does not compile here.** SDCC cannot build it
and `arduino-cli` plus the AVR core is ~250 MB against Vercel's 250 MB
function limit. `POST /compile` with an Arduino `DEVICE` is refused, naming
the toolchain it would need, and returns the generated source anyway.

`DEVICE ATMEGA328P:` is the same board **without** the core, and that one does
compile here — see below.

### micro:bit

`DEVICE MICROBIT:` emits **MicroPython**, not C — and that is the case the
target interface exists for. Several `WHEN` blocks compile to cooperative
tasks, which on the 8051 and AVR is a Duff's device: a `switch` whose `case`
labels sit inside the loops, so a task resumes by jumping into the middle of
its own control flow. MicroPython has no `goto`, so that shape cannot be
expressed at all. It becomes generators instead:

```python
def bw_task0():
    while True:
        _level['led'] = 1 - _level['led']
        pin0.write_digital(_level['led'])
        _deadline = running_time() + (500)
        while running_time() < _deadline:
            yield
        yield                      # loop back-edge
```

Same contract — yield at every wait and every loop back-edge, so no script
starves another — with nothing in common in the code that implements it.

Pins are `P0`–`P20`, `BUTTON_A`, `BUTTON_B`; analog is P0–P4 and P10 only, and
asking for it elsewhere is a parse error. Declaring P3, P4, P6, P7, P9 or P10
emits `display.off()` first, because those are wired to the 5×5 LED matrix and
the display driver would fight anything else driving them.

Nothing to compile: MicroPython is interpreted on the device, so `POST
/compile` refuses and points at `/transpile`. Flash the result with `uflash`,
or paste it into [python.microbit.org](https://python.microbit.org).

Because there is no compiler to catch mistakes, CI **runs** the generated
program against a stub `microbit` module and checks that the generators
actually interleave, that a 100 ms wait lasts about 100 ms, and that
`ACTIVE LOW` reaches the pin inverted.

#### Getting the `.ino` (or the `.py`)

**Browser:** choose `Pseudocode`, paste a program starting `DEVICE ARDUINO-UNO:`,
press Compile. The status line reads `main.ino — source only, needs
arduino-cli`, the source appears in the output pane, and **Download** gives you
the file. A micro:bit behaves the same way and yields `main.py`.

**curl:**

```bash
curl -X POST https://stc-compiler.vercel.app/download \
     -H 'Content-Type: application/json' \
     -d '{"language":"pseudocode","code":"DEVICE ARDUINO-UNO:\n  PIN led = D13 OUTPUT\n  WHEN started:\n    FOREVER:\n      toggle led\n      wait 500 ms\n"}' \
     -OJ
# -> main.ino
```

Two things about the file itself:

- **The Arduino IDE wants a sketch in a folder of the same name**, so `main.ino`
  belongs in `main/`. The IDE offers to move it there when you open it. Add a
  `NAME blink` line and you get `blink.ino` instead, which makes the folder
  read sensibly; the name is restricted to letters, digits, `_` and `-`,
  because it is echoed into a `Content-Disposition` header.
- `#include <Arduino.h>` is emitted even though the IDE would add it. That is
  deliberate: the same file then also builds with `arduino-cli` or a plain C++
  toolchain, instead of only inside the IDE.

### AVR: the same boards, compiled

| `DEVICE` / `target` | Emits | Compiles here |
|---|---|---|
| `arduino-uno`, `arduino-nano` | Arduino core C++ | no — paste into the IDE |
| `atmega328p`, `atmega168p` | bare AVR C | **yes**, via avr-gcc |
| `microbit` | MicroPython | nothing to compile — see above |

An ATmega328P *is* an Uno, and pins keep the board's labels (`D13`, `A0`; port
names like `PB5` are accepted and canonicalised), so a program moves between
the two devices unchanged and only the generated C differs. What changes is
what the C does: the generator knows the pin at emit time, so

```
turn on led     ->   PORTB |= _BV(PB5);        /* one instruction */
toggle led      ->   PINB = _BV(PB5);          /* hardware toggle, no RMW */
```

instead of the core's `digitalWrite`, which resolves the port through a
PROGMEM table and checks for a PWM channel to disable on every call. A
two-script scheduler with an ADC read comes out at **638 bytes**.

Timer 0 runs in CTC mode at exactly 1 kHz — the emitter picks the prescaler
and compare value that divide the declared `CLOCK` evenly, and refuses a clock
that cannot produce an exact millisecond rather than silently drifting.

Building the toolchain bundle:

```bash
./scripts/fetch-avr-gcc.sh    # ~33 MB: avr-gcc, binutils, avr-libc (avr5 only)
./scripts/verify-avr.sh       # MUST run on Linux — see below
```

`fetch-avr-gcc.sh` lifts Debian **bullseye** binaries out of `.deb`s, the same
trick and the same reason as `fetch-sdcc.sh`: bullseye's `cc1` needs only
`GLIBC_2.14`, where bookworm's needs 2.36 and would not start on Vercel's
Amazon Linux 2023 (2.34). `cc1plus` and the other 41 multilibs are dropped;
nothing here emits C++.

The bundle is Linux x86_64, so **building it on macOS does not verify it** —
and that gap is not theoretical. Four separate failures got through a
successful-looking build before CI caught them: the host `as` being handed
`-mmcu=avr5`, a missing `avr/io.h`, an absent LTO linker plugin, and `ld`'s
linker scripts. Each one produced a 45 MB bundle that looked complete.

So verification runs in CI (`.github/workflows/ci.yml`), on Linux, on every
push: `verify-avr.sh` compiles, links and objcopies using only the vendored
binaries, and both goldens are then built for **every** part in `AVR_TARGETS`
under `-Werror`. The job also asserts the GLIBC floor and uploads the verified
bundle as an artifact. To run the check by hand on any Linux box:

```bash
./scripts/fetch-avr-gcc.sh && ./scripts/verify-avr.sh
```

The Arduino core is deliberately **not** vendored: it is LGPL-2.1, and static
linking it into an image this service hands back engages the relink
obligation. avr-libc is BSD-3-Clause, and avr-gcc's runtime carries the GCC
Runtime Library Exception, so compiled output is unencumbered.

## Debug symbol tables

That scheduler is also what makes the generated code debuggable, and
[`stc_symtab.py`](stc_symtab.py) is what lets a debugger find its way around:

```bash
sdcc -mmcs51 --std-c99 --debug ... -o build/ prog.c
python3 stc_symtab.py --cdb build/prog.cdb --source prog.c -o symbols.json
```

The scheduler keeps its position in named statics — `bw_ms`, `<task>_state`,
`<task>_until` — so "where is the program right now" is three variable reads
rather than anything that needs instrumenting. This tool pulls their addresses,
and the code address of every `case` label (each one a yield point), out of
SDCC's `.cdb` and writes the JSON that three separate debug targets consume: a
ucsim fork, an emu8051 fork, and an on-chip UART monitor on real silicon. The
contract they all implement is `stc12c5a60s2-lab/docs/DEBUG-CONTROL-MODEL.md`.

Producing it needs both halves of the problem — what a task looks like, and
where the linker put it — which is why it lives here and not in either
emulator. `scripts/test-symtab.py` builds a two-script fixture end to end and
checks every address against the linker's `.map`, an artefact this tool never
reads: agreeing with the `.cdb` alone would only prove the parser is
self-consistent.

When the C came from a Brickwright project — `sb3-creator`'s
`generateC(project, {debug: true})` — it also carries an `@bw yield` header
naming the **Scratch block** behind each `case` label, and those ids are merged
into `yields[].block`. That is what lets a front end highlight the block a
halted program is sitting on instead of showing a state number. A map that
disagrees with the `case` labels in the same file is refused rather than
merged: pointing at a confidently wrong block is worse than pointing at
nothing. Hand-written firmware has no such header, no `block` keys, and no
error.

---

## Calling it

### JavaScript (browser / TurboWarp extension)

```javascript
async function compileSTC12(code) {
  const res = await fetch('https://stc-compiler.vercel.app/compile', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ code, target: 'stc12c5a60s2', fosc: 11059200 }),
  });
  const data = await res.json();
  if (!data.success) throw new Error(data.error);
  return data.base64;              // hand to the flasher
}
```

### Python

```python
import base64, requests

def compile_stc12(code, out="main.hex"):
    res = requests.post(
        "https://stc-compiler.vercel.app/compile",
        json={"code": code, "target": "stc12c5a60s2"},
    ).json()
    if not res.get("success"):
        raise RuntimeError(res.get("error", "compile failed"))
    with open(out, "wb") as f:
        f.write(base64.b64decode(res["base64"]))
```

Then flash it:

```bash
stcgal -P stc12 -p /dev/cu.usbserial-XXXX -l 2400 -b 115200 main.hex
```

---

## Architecture

Pre-built Linux binaries shipped next to a thin FastAPI wrapper, so each
invocation can `exec()` the compiler against a writable `/tmp` workspace.

```
POST /compile
   └─ stage toolchain into /tmp/sdcc   (once per container, ~8 MB)
   └─ write source to /tmp/build-<uuid>/main.c
   └─ exec bin/sdcc ─┬─ bin/sdcpp      (preprocessor)
                     ├─ bin/sdas8051   (assembler)
                     └─ bin/sdld       (linker)      → main.ihx
   └─ bin/packihx or bin/makebin       → main.hex / main.bin
   └─ base64 → JSON, delete the workspace
```

Two details that matter and are easy to get wrong:

**SDCC finds its headers relative to `argv[0]`**, as `<binary>/../share/sdcc`.
That is why the staging step copies `share/` alongside `bin/` instead of
pointing at the read-only originals — keeping the relative layout intact means
no path flags and no duplicate-library link errors.

**SDCC is four programs, not one.** It `fork`/`exec`s `sdcpp`, then `sdas8051`,
then `sdld` — confirm any time with `sdcc -V`. All four have to be present and
executable, which is what the `/tmp` staging and `chmod` are for.

---

## Building the vendored toolchain

```bash
./scripts/fetch-sdcc.sh
```

This lifts the binaries out of Debian's `.deb` packages and strips them to the
mcs51 target only — a full SDCC install is ~150 MB, almost all of it pic16
libraries and documentation. The result is **~8 MB**, against Vercel's 250 MB
uncompressed function limit.

**Why Debian bullseye (SDCC 4.0.0) rather than the newest release:** glibc
symbol versioning is forward-compatible only. Bullseye's toolchain caps the
requirement at `GLIBC_2.29`, which runs fine on Vercel's Amazon Linux 2023
(glibc 2.34). Bookworm's SDCC 4.5.0 needs `GLIBC_2.36` and would not start.
Check before bumping:

```bash
strings bin/sdas8051 | grep -oE 'GLIBC_2\.[0-9]+' | sort -V | tail -1
```

---

## Deploying

```bash
vercel --prod
```

`vercel.json` uses the legacy `builds` config, which includes the whole project
directory in the function bundle — `bin/` and `share/` included. The newer
config traces Python imports only and would silently omit them.

Compiles take well under a second (~0.2 s for a small program), so the default
function duration is not a consideration.

---

## Licensing

- **This wrapper** — MIT, see [`LICENSE`](LICENSE).
- **SDCC** — GPL-2.0-or-later. Its runtime libraries and headers carry a
  linking exception, so binaries you compile with it are unencumbered.
- Full detail, including why serving it over HTTP is not distribution:
  [`NOTICE.md`](NOTICE.md).
