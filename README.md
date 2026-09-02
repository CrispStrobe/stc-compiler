# stc-compiler

A serverless REST service that turns source into a flashable image for small
machines — and a browser page that does most of it with no server at all.

It began as one thing: compile **C to Intel HEX for the STC12C5A60S2** with
[SDCC](https://sdcc.sourceforge.net/), because STC's own toolchain is a
Windows-only `.exe` and nearly every STC12 project in the wild is a Keil
µVision project SDCC rejects outright. That is still the centre. Around it
have grown four more hosted toolchains, a block-shaped pseudocode front end
with nine code generators behind it, an assembler lane, a disassembler, a
debug-symbol extractor, and eight in-browser flashing protocols.

Deliberately shaped like
[`CrispStrobe/legacy-lego-compiler`](https://github.com/CrispStrobe/legacy-lego-compiler),
which does the same job for LEGO NXT and EV3 bytecode, so client code is nearly
identical between them.

- **Live:** <https://stc-compiler.vercel.app> · **Page:** <https://crispstrobe.github.io/stc-compiler/>
- Licensing: wrapper MIT; the vendored compilers keep their own licences. See
  [`NOTICE.md`](NOTICE.md) — short version: your compiled image is yours.

---

## Who calls this

The compile side of the STC12 work in
[`CrispStrobe/stc12c5a60s2-lab`](https://github.com/CrispStrobe/stc12c5a60s2-lab),
and the BrickWright block-to-silicon back end: the browser generates source,
POSTs it here, gets back an image, and flashes it over Web Serial or WebUSB.

It is a separate deployment from `legacy-lego-compiler` on purpose. SDCC is
GPL-2.0-or-later; that repository's story is MIT plus MPL/BSD, and there is no
reason to entangle the two.

---

## The shape of it

Three lanes, which is worth holding in mind because the endpoints follow it:

```
                     ┌─ emit_c ────────▶ C ──▶ sdcc / avr-gcc / arm-gcc / cc65 ──▶ image
pseudocode ─parse─▶ AST ─ emit (MicroPython) ─▶ .py   (interpreted on the device)
                     ├─ emit (Arduino C++) ──▶ .ino  (built by the IDE)
                     ├─ emit (TypeScript) ───▶ .ts   (built by PXT / MakeCode)
                     └─ emit_pseudocode ─────▶ text  (the round trip)

Keil C51 ──translate──▶ SDCC-dialect C ──▶ sdcc ──▶ image
assembly ──────────────────────────────▶ sdas8051 / ca65 / avr-gcc / arm-gcc / sdasz80 ──▶ image
```

One AST, many back ends. That is the whole design: `stc_pseudocode.py` is the
reference implementation and the test oracle for a dialect that also has a
JavaScript implementation in `sb3-creator`, and the differences between them
are tracked in [`DIVERGENCES.md`](DIVERGENCES.md) rather than discovered on
silicon.

---

## API

### `POST /compile`

```jsonc
{
  "code":    "#include <stc12.h>\nvoid main(void) { ... }",  // required
  "language": "c",             // c | pseudocode | keil | arduino
  "target":  "stc12c5a60s2",   // see Targets below
  "fosc":    11059200,         // emitted as -DFOSC_HZ / -DF_CPU; null to omit
  "defines": { "DEBUG": "1" }, // extra -D flags; null value = bare define
  "options": ["--opt-code-size"], // raw compiler flags
  "format":  "hex",            // hex (packihx) | ihx (raw) | bin (makebin)
  "disassemble": false,        // also return an 8051 listing
  "symbols":     false         // also return a debug symbol table (see below)
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
  "memory":   "Internal RAM layout:\n...",  // SDCC's memory map
  "c":        null,          // the generated/translated C, when there was one
  "listing":  null,          // {asm, lineMap} artifact, for a debugger
  "symbols":  null           // addresses of bw_ms, task state, every yield point
}
```

Failure returns `{"success": false, "error": "<compiler output>"}`, with
`line` and `stage` when the front end caught it rather than the compiler.

Source is capped at 1 MB (4 MB across a whole project).

The `memory` field is worth surfacing to users — it is how you catch an image
quietly outgrowing 60 KB of flash or 248 bytes of stack.

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

For a target this service cannot build — a micro:bit, an Arduino sketch, a
MakeCode Arcade game — the **source** is the file: `main.py`, `main.ino` or
`main.ts`, returned 200 with `X-Source-Only` naming the toolchain it would
have needed. A genuine error (a bad pin, an unknown target) is still a 400
with the message. The difference is whether there is usable output, not
whether a compiler ran.

Response headers: `Content-Disposition: attachment; filename="main.hex"`,
`X-Image-Bytes`, and `Access-Control-Expose-Headers` so a browser `fetch()` can
read both.

### `POST /transpile`

Pseudocode in, source out, no compiler involved — for seeing exactly what the
front end produced. Returns `c` (whatever the device's generator emits, C or
Python or TypeScript), the `language` label for it, plus the resolved `part`,
`clock`, `pins` and `variables`. Errors carry a `line`.

### `POST /decompile`

Pseudocode in, canonical pseudocode out — `parse` followed by
`emit_pseudocode`. Normalised layout, comments dropped, and a fixed point:
feeding the result back in returns it unchanged.

### `POST /assemble`

Raw assembly in, image out. **Five toolchains**, picked from `target`:

| chain | tools | targets |
|---|---|---|
| 8051 | `sdas8051` + `sdld` | `stc12c5a60s2` `stc12c5a16s2` `stc89c52` `stc89c52rc` `stc15f2k60s2` `stc15w408as` `mcs51` |
| 6502 | `ca65` + `ld65` (`eater.cfg`) | `eater6502` `6502` `w65c02` |
| AVR | `avr-gcc` | `atmega328p` `atmega168p` `atmega2560` `attiny85` `attiny88` |
| ARM | `arm-none-eabi-gcc` (`nrf52833.ld`) | `nrf52833` |
| Z80 | `sdasz80` + `sdldz80` + `makebin` | `z80` |

With `"debug": true` the response also carries a `stages` payload —
`{tokens, passes, listing}` — the assembler's own internal view: the lexed
source, the symbol table after linking, and the native listing. Each toolchain
gets its own parser over its own output format ([`stages.py`](stages.py)), so
an assembler lane in a front end can show tokens and symbols without
reimplementing five assemblers.

### `POST /uf2`

A raw binary and an origin address in, a **UF2 container** out — the format an
RP2040 in BOOTSEL mode accepts as a drag-and-drop file. This is the last step
of the Pico path: nothing on the device speaks a bootloader protocol, it
presents a mass-storage device, so the browser's job is to hand the user a
file rather than to program a chip.

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

**The table is generated from the encoding's regularities** — the `Rn` runs,
the `@Ri` pairs, the `addr11` ladders — with `assert len(TABLE) == 256`. It is
never hand-written, and it is checked against SDCC's own assembler two ways:

- `scripts/test-disasm.py <build-dir>` parses SDCC's relocated listing (`.rst`),
  which pairs every emitted byte with the mnemonic `sdas8051` assembled it from,
  and fails if we decode the same bytes differently or desynchronise on a wrong
  instruction length.
- `scripts/test-reassemble.py <file.hex>` feeds the disassembly back through
  `sdas8051` + `sdld` and compares bytes. **Emit labels, not bare addresses:**
  handing the assembler a number lets it resolve in its own frame and every
  relative displacement comes out short by 2.

Linear sweep **with branch-target sync points**: a target landing
mid-instruction proves the sweep is out of phase, so it becomes an anchor —
decode restarts there, orphaned bytes go out as `.db`, iterate to a fixed
point. Only real branch mnemonics seed anchors; `MOV DPTR,#imm16` is as often
a table base and must not split live code. Corpus scale: **380/380 byte-exact
over 349 images.**

### `POST /translate` · `POST /translate-project`

Keil C51 in, SDCC-dialect C out. See [Keil C51](#keil-c51) below.

`/translate-project` takes a whole project — `{"files": {"path": "content"}}` —
and owns the project-wide fixes a single file cannot: ISR prototype injection,
case-unified externs, `_at_` addresses stamped onto their externs, and the
`.uvproj` source list and memory model. With `"link": true` the response also
carries one linked, flashable image.

### `GET /health`

Reports every toolchain's version (SDCC, avr-gcc, arm-none-eabi-gcc, ca65),
every compile target, every assemble target, and every pseudocode device. Also
the cheapest way to see whether a cold start staged the toolchains correctly.

### `GET /` · `GET /docs`

A small browser UI with the blink example preloaded — no build step, no CDN.
Pick a language, target, clock and output format, compile with the button or
<kbd>Cmd</kbd>/<kbd>Ctrl</kbd>+<kbd>Enter</kbd>, then **Download** the image or
**Copy** it to the clipboard. Output, the memory map and the build log are on
separate tabs; `.bin` is shown as a hex dump rather than mojibake. `/docs` is
FastAPI's generated OpenAPI documentation.

**About dialog** — an *About* link opens a modal listing every licence (this
wrapper MIT; SDCC GPL-2.0-or-later; its runtime headers with the linking
exception, quoted in full), the corresponding-source pointer, links to the
related CrispStrobe repositories, a warranty and hardware disclaimer, a
non-affiliation statement, what happens to submitted source, and the imprint.

#### Imprint / Impressum

The operator details in the About dialog come from environment variables, so a
real postal address is never committed to a public repository and a fork
deploying its own instance does not publish someone else's details:

```bash
vercel env add IMPRINT_NAME production      # e.g. Jane Doe
vercel env add IMPRINT_ADDRESS production   # multi-line is fine
vercel env add IMPRINT_EMAIL production
```

If none are set the section is omitted entirely rather than rendering a
placeholder. Note that a publicly reachable service operated from Germany
generally needs one (§ 5 DDG, formerly TMG).

---

## Targets

Three questions that are easy to conflate: which chips the **pseudocode front
end** knows, which the service can **compile**, and which a browser can
**flash**. They are different lists.

### Compiled here

| `target` | Toolchain | Applied limits |
|---|---|---|
| `stc12c5a60s2` | sdcc `-mmcs51` | `--iram-size 256 --xram-size 1024 --code-size 61440` |
| `stc12c5a16s2` | sdcc | as above, `--code-size 16384` |
| `stc89c52rc` | sdcc | `--iram-size 256 --xram-size 256 --code-size 8192` (12T) |
| `stc15f2k60s2` | sdcc | `--iram-size 256 --xram-size 1792 --code-size 61440` (1T) |
| `mcs51` | sdcc | none — bring your own via `options` |
| `atmega328p` | avr-gcc | `-mmcu=atmega328p`, 32 KB |
| `atmega168p` | avr-gcc | 16 KB |
| `atmega2560` | avr-gcc | 256 KB |
| `attiny85` `attiny88` | avr-gcc | 8 KB |
| `rp2040` | arm-none-eabi-gcc | Cortex-M0+, SRAM image (`pico-sram.ld`) |
| `stm32f030` | arm-none-eabi-gcc | Cortex-M0, real flash image at `0x08000000` (`stm32f030-flash.ld`) |
| `eater6502` | cc65 | 65C02, 32 KB ROM at `$8000` (`eater.cfg`) |

The 8051 targets compile `-mmcs51 --std-c99`; adding a part is three lines in
`TARGETS` in [`app.py`](app.py). Keil translation is 8051-only by definition
and is refused for any other target rather than silently miscompiled.

`language: "arduino"` is a fifth route: an Arduino-API sketch compiled against
a vendored **ATTinyCore** subset, for `attiny85` and `attiny88` only. That is
the one place a real Arduino core is linked server-side; see the licensing
posture in [`NOTICE.md`](NOTICE.md).

The `stm32f030` image is a genuine flash image — vectors first, initial SP in
word 0 and the reset handler in word 1 — not a bare `.text` blob, which is
what makes it bootable and what `scripts/test-api.py` asserts about it.

### Known to the pseudocode front end

Nineteen `DEVICE` names across six architectures:

| family | `DEVICE` | emits | built here |
|---|---|---|---|
| STC 8051 | `stc12c5a60s2` `stc12c5a16s2` `stc15f2k60s2` `stc15w408as` `stc89c52` `stc89c52rc` | C | **yes** |
| bare AVR | `atmega328p` `atmega168p` | C | **yes** |
| bare AVR (tiny) | `attiny85` `attiny88` | C | **yes** (no `print` — no USART) |
| Arduino core | `arduino-uno` `arduino-nano` `arduino-mega` | C++ `.ino` | no — needs `arduino-cli` |
| MicroPython | `microbit` (`micro-bit`) | `.py` | nothing to compile |
| MicroPython | `pico` (`rp2040`) | `.py` | nothing to compile |
| 6502 | `eater6502` | — | no pseudocode generator — see *Known gaps* |
| game engine | `arcade` | TypeScript `.ts` | no — needs PXT |

`DEVICE` may be written with or without a trailing colon; `sb3-creator` writes
it without.

### Known gaps

Recorded here rather than left to be discovered, because a device that parses
and emits something is easy to mistake for a device that works:

- **`eater6502` has no pseudocode generator.** The DEVICE name is real and the
  6502 lane works — hand-written C or assembly through cc65, via
  `target: "eater6502"` on `/compile` or `/assemble`. What is missing is an
  emitter: until 2026-09-02 the device was registered on the AVR generator and
  produced `<avr/io.h>` and `ISR(TIMER0_COMPA_vect)` for a 65C02. It now
  refuses at the `DEVICE` line and says where the working lanes are, because
  emitting nothing beats emitting confident code for the wrong architecture.
  Writing it is scoped rather than hidden: the pins are the 65C22 VIA's
  (`$6000` PORTB, `$6001` PORTA, `$6002`/`$6003` the DDRs), and the open
  question is the millisecond tick — VIA Timer 1 in free-run mode wants an IRQ
  handler, and `crt0.s` currently points IRQ at a bare `RTI`.
- **A `DEVICE` line does not set the SDCC size flags.** The DEVICE decides the
  toolchain, but `--code-size` still comes from the request's `target` field,
  so `DEVICE STC89C52RC` without an explicit `target` is compiled with the
  STC12's 60 KB limit rather than its own 8 KB. Pass `target` as well until
  this is wired.
- **`print` is refused on the ATtinys**, which is correct: neither the ATtiny85
  nor the ATtiny88 has a USART.
- No `LIST`/arrays in the dialect; PWM via the PCA modules and UART output as
  pseudocode statements are still unwritten.

Closed on 2026-09-02, and now held by `test_device_matrix.py`:
`attiny85`/`attiny88` (the generator emitted ATmega Timer-0 spellings — the
ATtiny85 names the mask register `TIMSK`, and the ATtiny48/88 has no `TCCR0B`
or `WGM01` at all), and the arcade verbs plus `randint`/`controller` returning
a 500 on a chip instead of a parse error naming the board.

---

## The pseudocode front end

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

### Declarations

| Form | Notes |
|---|---|
| `DEVICE <part>[:]` | optional wrapper, like `SPRITE Name:` |
| `NAME <word>` | what the generated file is called: `blink.ino`, `blink.hex` |
| `CLOCK <hz>` / `CLOCK <n> MHz` | overrides the `fosc` request field |
| `PIN <n> = P1.0 OUTPUT [ACTIVE LOW]` | `ACTIVE LOW` makes `turn on` drive 0 |
| `PIN <n> = P3.2 INPUT [ACTIVE LOW]` | readable in expressions |
| `PIN <n> = P1.2 ANALOG` | 10-bit ADC; channel *n* is on `P1.n`, so P1 only |
| `PIN <n> = D9 PWM` / `TONE` | hardware timer channels, not bit-banged |
| `PORT <n> = P2 OUTPUT [ACTIVE LOW]` | whole-byte I/O, one store |
| `TABLE <n> = 1, 2, 4, 8` | constants in flash, read with `name[i]` |

The location on the right of the `=` is whatever the `DEVICE` calls a pin, and
the device checks it — down to the package:

```
P5.0 on an STC12C5A60S2   -> port 5 is an STC15 feature
P5.0 on an STC15F2K60S2   -> not bonded on the DIP-40; only P5.4 and P5.5 reach pins
PD0 on an ATtiny85        -> known ports: A, B
D54 on an Arduino Mega    -> has D0-D53, not D54
P21 on a micro:bit        -> has P0-P20, not P21
GP29 on a Pico            -> has GP0-GP28, not GP29
```

Asking for something the board cannot do is a parse error naming the board,
not a miscompile.

### PARTs — peripherals as first-class declarations

| Form | What it is |
|---|---|
| `PART <n> = 74HC595 DATA <p> CLOCK <p> LATCH <p> [ACTIVE LOW]` | shift register |
| `PART <n> = SEVENSEG8 SEGMENTS <port> SELECT <p> <p> <p> [COMMON CATHODE\|ANODE]` | ISR-scanned 7-segment display via a 74HC138 |
| `PART <n> = LEDBANK8 ON <port> [ACTIVE LOW]` | eight LEDs on one port |
| `PART <n> = MATRIX8X8 ROWS 74HC595 DATA <p> CLOCK <p> LATCH <p> COLUMNS <port>` | 8×8 dot matrix, self-scanning |
| `PART <n> = KEYPAD4X4 ROWS <4 pins> COLS <4 pins>` | sixteen keys on eight pins |

These are 8051-family-first: `MATRIX8X8` and `KEYPAD4X4` need a whole port and
the Timer-0 ISR, and push-pull targets need row tri-stating before they may
opt in.

The matrix refreshes itself in the Timer-0 ISR — one row per tick, 125 Hz — so
its drawing verbs are plain frame-buffer writes and the `WHEN` block keeps
running:

```
show image heart on screen      light pixel x y          clear pixel x y
clear screen                    set pixel 2 3 to on      set pixel 4 5 brightness 2
scroll screen left              draw row 0 = 0b11111111  set screen brightness 1
IF pixel 2 3 is on THEN:        # the reporter
```

The frame buffer is bit-plane packed: two planes, four grey levels, rendered
by binary code modulation. Wiring facts are baked into the scan — 595 rows
active-HIGH with Q7 at the top, port columns active-LOW with bit 7 at the
left — so an image byte reads top-down and MSB-left, which is how a person
draws one.

`SEVENSEG8` and `LEDBANK8` bring their own verbs: `show number … on <part>`,
`show digit … = value … on <part>`, `set digit … to segments … on <part>`,
`turn on/off led … on <part>`, `set leds to … on <part>`, `light only led …
on <part>`. When a 7-segment display and an LED bank share a port, the
generator warns that the 74HC138 address is visibly driven rather than
shadow-masked.

`KEYPAD4X4` is read-only: the scanned key, `0`–`15`, or `-1` for none. Its
scanner is the one verified on Prechin A2 silicon.

### Display verbs

`lcd` (HD44780), `tft` (ILI9341), `oled` (SSD1306/SH1106) and RGB LEDs each
have a verb family that lowers to a stable C call surface:

```
lcd clear screen1                        ->  bw_lcd_clear(screen1);
lcd set cursor 0 0 on screen1            ->  bw_lcd_cursor(screen1, 0, 0);
lcd print "Hello" on screen1             ->  bw_lcd_print_s(screen1, "Hello");
tft pixel 10 20 R 255 G 0 B 128 on disp  ->  bw_tft_pixel(disp, 10, 20, 255, 0, 128);
oled print value on oled1                ->  bw_oled_print_n(oled1, value);
set led1 colour to R 255 G 128 B 0       ->  bw_rgb_set(led1, 255, 128, 0);
```

The drivers themselves live in `sb3-creator`'s `generateC`; this side owns the
grammar, the AST, the round trip and the call signatures, so the two stay
callable across the boundary.

### Control flow and statements

| Form | Notes |
|---|---|
| `WHEN started:` | also `WHEN flag clicked:` / `WHEN powered on:` |
| `WHEN <pin> pressed:` / `released:` | debounced edge hat |
| `WHEN key <n> pressed:` / `released:` | a `KEYPAD4X4` key, debounced |
| `DEFINE <name> (a) (b):` | procedure; callable as `name 3, 80` or `name(3, 80)` |
| `FOREVER:` · `REPEAT n:` · `IF c THEN:` / `ELSE:` | indentation-scoped |
| `WHILE c:` · `REPEAT UNTIL c:` · `wait until c` | |
| `turn on/off <pin>` · `set <pin> high/low` · `toggle <pin>` | |
| `wait <n> seconds` / `<n> ms` | Timer 0, not a spin loop |
| `set <v> to <e>` · `change <v> by <e>` | variables are 16-bit `int` |
| `stop` | ends this script; the others keep running |
| `print <e>` | UART, 9600 baud |
| `randint(a, b)` · `controller dx/dy` | MakeCode Arcade only; refused elsewhere |
| `+ - * / %` (`mod`), `= != < > <= >=`, `and or not` | `=` compares, as in Scratch |

Procedures may be called before they are defined — the order people actually
write in — because headers are registered in a first pass. Parameters are
locals, so they never leak into the global variable list. `DEFINE FAST` is
accepted for `sb3-creator` compatibility.

**`not` binds looser than comparisons**, at Python's level. This was changed
after `IF not k = shown` mis-parsed as `(not k) = shown` and shipped a
flashed-clean, silently-dead program to real silicon.

`ANALOG` pins read through the ADC wherever they appear in an expression, so
`IF pot > 512 THEN:` just works: the emitter adds the polled `adc_read()`
helper, sets `P1ASF`, puts the pin in high-impedance mode and powers the
converter. Channel *n* lives on `P1.n`, which is why `ANALOG` is rejected on
any other port.

`ACTIVE LOW` is the point of the whole thing: a quasi-bidirectional 8051 pin
sinks 20 mA but sources ~230 µA, so LEDs get wired active-low and `turn on`
has to emit a `0`. The front end knows that, so the pseudocode never has to.

### The scheduler contract

Several `WHEN` blocks compile to a **cooperative scheduler**: a Timer-0
millisecond tick, one state machine per script, a yield at every wait and
every loop back-edge — Scratch's own contract. Deadlines are wraparound-safe
comparisons behind an atomic read of the tick. A single-script program keeps
straight-line emission, unless a `MATRIX8X8` forces the ISR anyway.

On the 8051 and AVR that state machine is a Duff's device: a `switch` whose
`case` labels sit inside the loops, so a task resumes by jumping into the
middle of its own control flow. On the Arduino core it is the same shape with
`millis()` supplying the tick that the 8051 back end has to build by hand — a
32-bit tick, so the deadlines and the wraparound compare widen with it, and
that type comes from the target rather than from the AST walker.

Limit: with several scripts, procedures must not wait — a parse error, exactly
like Scratch's run-to-completion custom blocks.

**Never emit a cycle-counted delay.** Everything hangs off Timer 0 at FOSC/12,
the one mode 12T and 1T cores count identically. That is what makes the same
program timing-correct on an STC12, an STC89 and an STC15 alike. A wait too
short for the target to represent is refused, naming the floor, rather than
silently rounded to zero.

### The round trip

**Parsing builds an AST**, and every back end walks it:

```
text ──parse──▶ Program (AST) ──emit────────────▶ C / Python / TypeScript
                      │
                      └────────emit_pseudocode──▶ text
```

That is the shape `sb3-creator` uses, where blocks are the IR and
`decompile(project)` walks it back — and it is what makes the round trip
testable, because `parse` and `emit_pseudocode` have to be inverses.
`scripts/test-roundtrip.py` checks it the way `transparency.test.mjs` does:
every hop is compared against the **original**, not merely against the previous
hop, because a degraded output is a stable fixed point too. **1,014 checks**,
including multi-script schedulers, event hats, every PART, the display verbs,
three chip families, and one program built specifically around operator
precedence — if the right operand of a binary node is not re-emitted one level
tighter, `a - (b - c)` silently comes back as `(a - b) - c`.

---

## Beyond the 8051

### Arduino core

`DEVICE ARDUINO-UNO:` / `-NANO:` / `-MEGA:` emit Arduino core C++ —
`pinMode`/`digitalWrite`/`analogRead`, the script in `setup()`, cooperative
tasks dispatched from `loop()`. The scheduler contract survives the move
unchanged; the Arduino target ships no runtime of its own at all.

The Uno and Nano differ in exactly one place, and it is a trap rather than a
detail: **the Nano has `A6` and `A7`, and they are analog inputs only.** The
Nano carries the ATmega328P in a TQFP package, which brings those two ADC
channels out to pads with no digital I/O buffer behind them — so
`digitalWrite(A6, HIGH)` compiles, uploads, runs, and does nothing at all,
silently. Declaring one as `OUTPUT` or `INPUT` is refused here, naming the
package; `A6 ANALOG` is accepted, because that is the one thing the pin can do.
On the Uno the same two names are refused for an unrelated reason — the DIP
package does not bring them out at all — and the two messages are deliberately
different, because one sends you to the package and the other to the schematic.

**Core C++ transpiles here; it does not compile here.** SDCC cannot build it
and `arduino-cli` plus the AVR core is ~250 MB against Vercel's 250 MB
function limit. `POST /compile` with an Arduino `DEVICE` is refused, naming
the toolchain it would need, and returns the generated source anyway.

### Bare AVR

`DEVICE ATMEGA328P:` is the same board **without** the core, and that one does
compile here. An ATmega328P *is* an Uno, and pins keep the board's labels
(`D13`, `A0`; port names like `PB5` are accepted and canonicalised), so a
program moves between the two devices unchanged and only the generated C
differs. What changes is what the C does:

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

The hardware constraints are refusals, not footnotes. PWM is offered on
D9/D10 (Timer 1) and D11/D3 (Timer 2) — **not** D5/D6, which are Timer 0 and
therefore the millisecond tick every `wait` is measured against. The tone is
Timer 1 toggling OC1A, so it is D9 and takes the timer outright, and PWM on D9
or D10 in the same program is refused pointing at D11 or D3.

### micro:bit and Pico

`DEVICE MICROBIT:` and `DEVICE PICO:` emit **MicroPython**, not C — and that is
the case the target interface exists for. Several `WHEN` blocks compile to
cooperative tasks, which on the 8051 and AVR is a Duff's device. MicroPython
has no `goto`, so that shape cannot be expressed at all. It becomes generators
instead:

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

micro:bit pins are `P0`–`P20`, `BUTTON_A`, `BUTTON_B`; analog is P0–P4 and P10
only, and asking for it elsewhere is a parse error. Declaring P3, P4, P6, P7,
P9 or P10 emits `display.off()` first, because those are wired to the 5×5 LED
matrix and the display driver would fight anything else driving them. A
`KEYPAD4X4` works on both boards through a shared scanner.

Nothing to compile: MicroPython is interpreted on the device, so `POST
/compile` refuses and points at `/transpile`. Flash the result with `uflash`,
paste it into [python.microbit.org](https://python.microbit.org), or use the
page's *Flash* button, which writes `main.py` over the raw REPL.

Because there is no compiler to catch mistakes, CI **runs** the generated
program against a stub `microbit` module and checks that the generators
actually interleave, that a 100 ms wait lasts about 100 ms, and that
`ACTIVE LOW` reaches the pin inverted.

### MakeCode Arcade

`DEVICE ARCADE:` is the furthest the target interface has been pushed: no GPIO
at all, a game engine instead. It emits **TypeScript** for the PXT compiler,
which builds it either to JS for the Arcade simulator or to ARM Thumb for
physical hardware.

```
DEVICE ARCADE

WHEN started:
  arcade create player kind Player
  arcade place player x 80 y 100
  arcade set player stay in screen
  FOREVER:
    arcade move player vx (controller dx) vy 0
    arcade score add 1
    wait 1 seconds

WHEN started:
  ARCADE ON OVERLAP Player Enemy:
    arcade game over lose
```

Sprites, tilemaps (`arcade tilemap`, `arcade set tile`, `arcade set wall`),
sprite-sheet frames, the controller as a reporter, score and game-over. Two
worked examples live in `docs/`: `arcade-example-dodge.bw` and
`arcade-example-dungeon.bw`. Declaring a `PIN` on this device is a parse error
that says so.

Provenance for the API surface is MIT throughout — see `docs/ARCADE-SCOPING.md`.

### The 6502

`DEVICE EATER6502:` names the composable 6502 machine from the Ben Eater
build — 65C02, 32 KB ROM at `$8000`, a 65C22 VIA at `$6000`, RAM below
`$4000` (`eater.cfg`, `crt0.s`). The **assembly and hand-written-C lanes
work** through cc65 (`ca65`, `ld65`, `cc65`). The pseudocode lane has no
generator and refuses at the `DEVICE` line saying so — see
[Known gaps](#known-gaps).

### What works where

`scripts/test-parity.py` prints this and asserts it, in both directions: a
feature a target claims must survive parse *and* leave recognisable evidence
in the output, and one it does not claim must be refused at parse time naming
the board.

| device | pwm | tone | print | table | port | part |
|---|---|---|---|---|---|---|
| STC12C5A60S2 / STC15 | yes | yes | yes | yes | yes | yes |
| STC89C52RC | n/a¹ | yes | yes | yes | yes | yes |
| ATmega328P / 168P | yes | yes | yes | yes | yes | yes |
| Arduino Uno / Nano / Mega | yes | yes | yes | yes | no² | yes |
| micro:bit | yes | yes | yes | yes | no³ | yes |
| Raspberry Pi Pico | yes | yes | yes | yes | no³ | yes |

¹ no PCA, so there is no PWM pin to declare.
² the Arduino core hides ports behind `digitalWrite`; reaching past it would
give up the portability that is the reason to emit core C++.
³ MicroPython has no whole-port write, and eight `write_digital` calls would
not land as the single store a `PORT` promises.

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
only place its translation unit gets that register from. A name reused at a
conflicting address — `sbit P2_0 = P3^7;` is real corpus code — is renamed,
uses and all.

**Register families.** `reg51.h`/`reg52.h`/`REGX52.H` map onto SDCC's
8051.h/8052.h — reg52 code expects Timer 2, which the STC12 does not have —
plus generated STC89 extras. `stc15*.h` maps onto its own family header with
Timer 2 at the STC15's 0xD6/0xD7. The chip-ambiguous registers (`WDT_CONTR`
is 0xC1 on an STC12 and 0xE1 on an STC89; `P4` is 0xC0 vs 0xE8) are left to
the file's own declarations on purpose. All compat headers are **generated**
(`keil-shim/generate-compat.py`) from datasheet facts, never copied from a
vendor header — so an SDCC release adding a register cannot silently break
the shim. Never hand-edit `keil-compat.h`.

**Beyond syntax, the translator also knows the traps:**

- *ISR vectors*: SDCC only emits one if the handler is visible in `main()`'s
  file; Keil links vectors separately. 24 of 37 ISR files in the corpus would
  compile and never fire. Single files get a warning; `POST /translate-project`
  injects the prototypes and fixes it.
- *Case*: Keil's BL51 links symbols case-insensitively, uVision resolves
  includes case-insensitively; project mode unifies both.
- *Timing*: a 1T part (STC12/STC15) dropped into a 12T socket (STC89) runs
  software delay loops ~6–12× too fast and breaks bit-banged I2C/1-wire.
  The translator flags busy-wait loops and `_nop_()` runs so the migration
  does not fail silently.
- *Bare `data`/`code`* are rewritten only when followed by whitespace plus a
  type, an identifier or `*`. As identifiers they are followed by an operator
  (`g(data)`, `code == 3`). That asymmetry is what makes it safe without
  parsing C.
- The shims include `<mcs51/stc12.h>`, never `<stc12.h>`: a project file named
  `STC12.h` shadows SDCC's header via `-I` ordering on a case-insensitive
  filesystem.

Also handled, each anchored to a corpus file: multiple `sbit` per line,
`data bit`, `(code *fp)()`, implicit int after a storage class, K&R params
across a newline-brace, flat/partial aggregate initialisers, tentative `[]`
arrays → extern, `bdata` → `__data __at` 0x20–0x2F with computed sbit
addresses, reentrant locals with explicit space → static, putchar/getchar
signatures, intrinsic redeclarations, `const` on `__code` pointer parameters,
and C51's tolerance of unknown preprocessing directives (`#defind` include
guards ship in real code).

**Measured, not asserted: 546 of 597 Keil-dialect files (91%)** across 86
public 8051 projects (GitHub + Gitee) compile after translation — and on the
one project of the corpus whose SDCC conversion also exists hand-written,
the translator converts **31 of 31** programs. The sbit oracle covers 4,448
uses, 2,924 vendor-confirmed, 0 mismatches. Whole projects link to a flashable
image via `/translate-project`. Remaining failures are broken sources and
constructs that need a typed front end.

---

## Debug artifacts

Three separate things a front end can ask for, all of which exist because a
browser cannot run `sdcc` or Python:

**Symbol tables** ([`stc_symtab.py`](stc_symtab.py), `"symbols": true`). The
scheduler keeps its position in named statics — `bw_ms`, `<task>_state`,
`<task>_until` — so "where is the program right now" is three variable reads
rather than anything that needs instrumenting. This pulls their addresses, and
the code address of every `case` label (each one a yield point), out of SDCC's
`.cdb`. Three debug targets consume it: a ucsim fork, an emu8051 fork, and an
on-chip UART monitor on real silicon.

`--debug` **changes the image**: measured on a two-task fixture, 8 of 39 hex
records differ and the image grows by two bytes, because SDCC stops
tail-merging returns so line records map cleanly. A symbol table is therefore
only ever valid for the image it was built *with*. Ask for both together, or
neither.

When the C came from a BrickWright project — `sb3-creator`'s
`generateC(project, {debug: true})` — it also carries an `@bw yield` header
naming the **Scratch block** behind each `case` label, and those ids are merged
into `yields[].block`. That is what lets a front end highlight the block a
halted program is sitting on instead of showing a state number. A map that
disagrees with the `case` labels in the same file is refused rather than
merged: pointing at a confidently wrong block is worse than pointing at
nothing.

`scripts/test-symtab.py` builds a two-script fixture end to end and checks
every address against the linker's `.map`, an artefact this tool never reads:
agreeing with the `.cdb` alone would only prove the parser is self-consistent.

**Listings** ([`listing.py`](listing.py), returned as `listing`). The
`{asm, lineMap, format, v}` artifact three consumers share: an assembler-lane
asm view, a debugger stepping map, and the AVR/ARM disassembly panes. Read
from SDCC's `.rst` for the 8051, and from `objdump -dS` plus
`--dwarf=decodedline` for the gcc toolchains.

**Assembler stages** ([`stages.py`](stages.py), `/assemble` with
`"debug": true`). Tokens, symbol passes and the raw listing — see
[`POST /assemble`](#post-assemble).

---

## The browser page

<https://crispstrobe.github.io/stc-compiler/>

Everything except turning source into an image runs in the page. It loads
CPython compiled to WebAssembly and imports **this repository's own
`stc_pseudocode.py`, `bw_micropython.py` and `bw_arcade.py`** — not a
JavaScript reimplementation, so there is no second implementation that can
drift from what the API and the test suites use. `docs/` keeps its own copy of
the three modules because Pages serves that directory and nothing above it;
`scripts/test-pages.py` fails the moment a copy and its original disagree.

That covers more than it sounds like: a micro:bit needs no compiler
(MicroPython is interpreted on the device) and an Arduino sketch is built by
the IDE, so for those targets the page is the whole toolchain. **Compile to
.hex** posts to the hosted API for the parts that genuinely need SDCC,
avr-gcc, arm-none-eabi-gcc or cc65, and says so.

CI starts a browser and transpiles every example in it, because "the page
loads" and "CPython starts in it and emits MicroPython" are different claims.

### Getting it onto a board

`docs/flash.js` implements **eight** programming paths, clean-room from
published protocol documents, over Web Serial and WebUSB:

| path | transport | boards |
|---|---|---|
| STK500v1 | Web Serial | ATmega328P — Uno, Nano, Pro Mini (optiboot) |
| STK500v2 | Web Serial | ATmega2560 — Arduino Mega (AVR068) |
| USBasp ISP | WebUSB | any AVR with an ISP header — closes the ATtiny gap |
| MicroPython raw REPL | Web Serial | micro:bit, Pico — writes `main.py` |
| STC ISP | Web Serial | STC12C5A60S2 / 5A16S2, after a cold power-on |
| STM32 AN3155 | Web Serial | STM32 via the ROM serial bootloader |
| SWD / CMSIS-DAP | WebUSB | STM32 via a debug probe — the bootloader-free path |
| Ben Eater programmer | Web Serial | parallel EEPROM (28C256 and kin) |

**Three are wired into the page today** — STK500v1, MicroPython and STC ISP.
The other five are library code with simulated-device tests but no button yet.

A micro:bit has no serial bootloader, but once MicroPython is on it the raw
REPL is a perfectly good file channel — which is what `microfs` and the
official editors use, and far less machinery than splicing a script into a
1.8 MB runtime hex. The trade is explicit: this writes `main.py` to a board
that **already has MicroPython**; flash that once from
[python.microbit.org](https://python.microbit.org) and it works from then on.

*Flash* pulses DTR to reset an AVR into its bootloader, then programs and
verifies it page by page. It probes 115200 and then 57600, because an Uno and
a modern Nano run optiboot at the first and an older Nano at the second — and
a wrong rate fails exactly like an absent board. Web Serial and WebUSB are
Chromium-only and need a secure context, which Pages provides; other browsers
are told why rather than given a dead button.

The STC parts are a different *interaction*, not just different bytes: their
ISP bootloader answers **only after a cold power-on** — a reset pulse will not
do — so the page starts pulsing `0x7F` and asks you to pull the power and
reapply it. That is the one thing only you can do.

Its wire format was not deduced. `scripts/fixtures/stc12-session.json` is a
transcript captured from **real stcgal** driving a simulated STC12C5A60S2 over
a pseudo-terminal, and the test asserts this code reproduces it **packet for
packet**. That matters: a test written from my own reading of stcgal would
have agreed with an implementation written from the same reading. It caught
two things reading had not — stcgal pads the image to a 512-byte boundary
before erasing, and the erase command carries the *part's* flash size (from a
magic-number table) rather than the image's.

Only the **stc12** ISP is implemented. The STC15 and STC89 families are
different protocols rather than dialects of it — stcgal classifies parts by
model name and hands them to separate handlers, and the STC15's handshake does
frequency calibration the STC12's has no notion of. Both the page and the
flasher refuse them by name: the page from the `DEVICE` line, before you have
power-cycled anything, and the flasher again from the magic the bootloader
announces.

Option bytes are deliberately **not** programmed, though stcgal rewrites them
on every run: an option byte is how you disable the ISP pin and lock yourself
out of the part, and nothing in the dialect asks for one to change.

Flashing is the one step whose output cannot be checked by compiling it, so
`scripts/test-flash.mjs` puts simulated devices on the far end of the wire and
asks whether their flash matches the image byte for byte. That is what catches
word-vs-byte addressing, an off-by-one page boundary, a checksum read as data,
or a REPL chunk swallowed in transit — mistakes that all look identical from
outside, as a board running the wrong thing. **39 checks, 0 failures.**

**Not verified against real hardware**, except the STC12 path. The protocols
are exercised end to end against simulators.

---

## Architecture

```
app.py                 FastAPI: every endpoint + the browser UI
stc_pseudocode.py      pseudocode <-> AST <-> C          (the front end, 5.2k lines)
bw_micropython.py      the MicroPython back end          (micro:bit, Pico)
bw_arcade.py           the MakeCode Arcade back end      (TypeScript)
keil2sdcc.py           Keil C51 -> SDCC dialect
stc_disasm.py          Intel HEX -> 8051 assembly
assemble.py            the five assembler chains
stages.py              assembler debug payload           (tokens, symbols, listing)
listing.py             {asm, lineMap} for a debugger
stc_symtab.py          .cdb -> debug symbol table
avr_symtab.py          the AVR equivalent
uf2.py                 binary -> UF2 container
keil-shim/             our replacements for Keil-only headers
  generate-compat.py   REGENERATES keil-compat.h — never hand-edit that file
bin/ share/            vendored SDCC        (~8 MB)
avr/                   vendored avr-gcc     (39 MB)
arm/                   vendored arm-none-eabi-gcc (82 MB)
cc65/                  vendored cc65        (3.7 MB)
arduino-core/          minimal ATTinyCore subset (LGPL, server-side only)
docs/                  the GitHub Pages app + mirrored modules
vendor/                upstream VERSION and copyright files
test_*.py              the pytest suite (see Tests)
scripts/               fetch/verify/deploy scripts and the standalone suites
```

Pre-built Linux binaries shipped next to a thin FastAPI wrapper, so each
invocation can `exec()` a compiler against a writable `/tmp` workspace.

```
POST /compile
   └─ stage toolchain into /tmp/{sdcc,avr,arm,cc65}   (once per container)
   └─ write source to /tmp/build-<uuid>/main.c
   └─ exec bin/sdcc ─┬─ bin/sdcpp      (preprocessor)
                     ├─ bin/sdas8051   (assembler)
                     └─ bin/sdld       (linker)      → main.ihx
   └─ bin/packihx or bin/makebin       → main.hex / main.bin
   └─ base64 → JSON, delete the workspace
```

Things that WILL bite you again:

**SDCC is four programs, not one.** It `fork`/`exec`s `sdcpp`, then `sdas8051`,
then `sdld` — confirm any time with `sdcc -V`. All four have to be present and
executable, which is what the `/tmp` staging and `chmod` are for.

**SDCC finds its headers relative to `argv[0]`**, as `<binary>/../share/sdcc`.
That is why staging copies `share/` alongside `bin/` instead of pointing at
the read-only originals — keeping the relative layout intact means no path
flags and no duplicate-library link errors.

**A partial stage used to stick forever**: the copy was skipped because the
directory existed. `stage_toolchain()` now checks for the binary, not the
directory, and heals by wiping and recopying.

**`SDCC_INCLUDE_DIR`** — `keil2sdcc` reads SFR addresses from SDCC's headers,
and on a deployment those live in `/tmp`, not Homebrew's path. Getting this
wrong silently turns every `sbit LED = P1^0;` into an unresolved one, so
`stage_toolchain()` must run before any Keil translation.

**`vercel.json` uses the legacy `builds` config on purpose.** It includes the
whole project directory in the function bundle. The newer config traces Python
imports only and would silently omit `bin/`, `share/`, `avr/`, `arm/`, `cc65/`.

---

## Building the vendored toolchains

```bash
./scripts/fetch-sdcc.sh       # ~8 MB: sdcc, sdcpp, sdas8051, sdld, packihx, makebin
./scripts/fetch-avr-gcc.sh    # ~39 MB: avr-gcc, binutils, avr-libc (avr5 only)
./scripts/fetch-arm-gcc.sh    # ~82 MB: arm-none-eabi-gcc, v6-m multilib only
./scripts/verify-avr.sh       # MUST run on Linux — see below
```

All of them lift binaries out of Debian **bullseye** `.deb`s rather than
building from source, and all of them do it for the same reason: **glibc
symbol versioning is forward-compatible only.** Bullseye's SDCC 4.0.0 caps at
`GLIBC_2.29`, which runs on Amazon Linux 2023 (glibc 2.34); bookworm's 4.5.0
needs `GLIBC_2.36` and would not start. Same for avr-gcc's `cc1`, which needs
only `GLIBC_2.14` from bullseye and 2.36 from bookworm. Check before bumping:

```bash
strings bin/sdas8051 | grep -oE 'GLIBC_2\.[0-9]+' | sort -V | tail -1
```

SourceForge is blocked from the development sandbox, which is why the vendored
SDCC comes from `deb.debian.org`.

The bundles are Linux x86_64, so **building one on macOS does not verify it** —
and that gap is not theoretical. Four separate failures got through a
successful-looking AVR build before CI caught them: the host `as` being handed
`-mmcu=avr5`, a missing `avr/io.h`, an absent LTO linker plugin, and `ld`'s
linker scripts. Each one produced a 45 MB bundle that looked complete.

So verification runs in CI, on Linux, on every push: `verify-avr.sh` compiles,
links and objcopies using only the vendored binaries, both goldens are built
for **every** part in `AVR_TARGETS` under `-Werror`, `scripts/elf-needed.py`
asserts every non-glibc dependency travels with the bundle, and the job checks
the GLIBC floor and uploads the verified bundle as an artifact.

The Arduino core is deliberately **not** vendored in full: it is LGPL-2.1, and
static linking it into an image this service hands back engages the relink
obligation. avr-libc is BSD-3-Clause and avr-gcc's runtime carries the GCC
Runtime Library Exception, so compiled output is unencumbered. The one
exception is the minimal ATTinyCore subset in `arduino-core/`, whose posture is
argued in full in [`NOTICE.md`](NOTICE.md).

### This service does not produce the same firmware as a local build

`stc12c5a60s2-lab` documents `brew install sdcc`, which is 4.5.0. This service
is 4.0.0. Measured on `01-blink`, the same C sent to both, with this service's
own flags:

| built by | size |
|---|---|
| local SDCC 4.5.0 | 996 bytes |
| this service, SDCC 4.0.0 | 888 bytes |

Both work. But do not diff a remote `.hex` against a local one and conclude
something is broken, and never pair a remote image with a locally produced
symbol table. The web page names the compiler beside the byte count for this
reason.

---

## Hosting and deploying

**Deploys are deliberate, not automatic.** The Vercel git integration is
disconnected on purpose: the free tier caps deployments at 100/day
account-wide, and auto-deploying every push burned through it mid-lane, with
a finished target sitting on `main` for hours while production served the old
one. Pushes are free; deploys are a decision.

```bash
scripts/deploy-vercel.sh            # preview
scripts/deploy-vercel.sh --prod     # promotes to stc-compiler.vercel.app
./scripts/test-api.py               # tests PRODUCTION — run it after --prod
```

Both deploy scripts refuse a dirty or unpushed tree: production must equal a
commit that exists on `origin/main`, or the deploy is untraceable.

A second host exists in the repo — `scripts/deploy-hf.sh` plus a `Dockerfile`
for a Hugging Face Space, same `app.py` and the same vendored toolchains on
port 7860 — so the service survives one platform's bad day. It is **currently
gated**: HF requires a PRO subscription to host Docker Spaces even on free
hardware. The script is ready for the day that changes.

Compiles take well under a second (~0.2 s for a small program), so the default
function duration is not a consideration.

---

## Tests

Everything below must pass before pushing.

| suite | what it holds to account | scale |
|---|---|---|
| `pytest test_*.py` | the dialect, PARTs, display verbs, arcade, assembler, UF2, listings, ARM builds | **335** |
| `scripts/test-roundtrip.py` | `parse` and `emit_pseudocode` are inverses | **1,014** |
| `scripts/test-api.py` | production: every endpoint, language, family | **172** |
| `scripts/test-parity.py` | every target × every feature, both directions | 82 |
| `scripts/test-peripherals.py` | PWM, tone, print | 62 |
| `scripts/test-golden.py` | what the emitter emits, byte for byte | 57 |
| `scripts/test-wait-floor.py` | a wait too short for the target is refused | 57 |
| `scripts/test-microbit.py` | the generated MicroPython actually *runs* | 54 |
| `scripts/test-symtab.py` | addresses match the linker's `.map` | 39 |
| `scripts/test-flash.mjs` | eight protocols against simulated devices | 39 |
| `scripts/test-pinmap.py` | what each package actually brings out | 38 |
| `scripts/test-tables-ports.py` | flash tables and whole-port I/O | 34 |
| `scripts/test-pages.py` | `docs/` still mirrors the transpiler | 25 |
| `scripts/test-wiring.py` | every test runs in CI, or says why not | 23 |
| `scripts/test-disasm.py` | decode agrees with `sdas8051`'s own listing | 380/380 |
| `scripts/test-reassemble.py` | disassembly reassembles byte-identically | 39/40 |

```bash
python3 -m pytest -q test_*.py
for s in roundtrip parity peripherals golden wait-floor microbit symtab \
         pinmap tables-ports pages wiring; do python3 scripts/test-$s.py; done
node scripts/test-flash.mjs
./scripts/test-api.py                       # deploy first — it tests production
./scripts/test-disasm.py ../stc/build/01-blink
./scripts/test-reassemble.py ../stc/build/01-blink/main.ihx
```

Three suites are excluded from CI with reasons that are themselves checked:
`test-api.py` needs a deployment, `test-disasm.py` and `test-reassemble.py`
need a built image from the lab repo. `test-wiring.py` enforces that anything
*not* on that list appears in the workflow — a test that is written,
committed, green and never executed reads as coverage and is not.

**Known gap in that guard:** `test-wiring.py` only scans `scripts/`, so the
17 root-level `test_*.py` files — 335 tests — are green locally and are not
run by CI.

---

## Where this is going: the toolchain in the browser

The page already runs the transpiler client-side, in Pyodide. Exactly one
thing still needs this service: **compiling**. Remove that and there is no
server — no deploy rate limit, no glibc pin, no skew between the page and the
compiler, and it works offline.

So: SDCC built for the mcs51 port as WebAssembly, served as static files.
Nothing runs server-side in that design. GitHub Pages serves bytes and the
compile happens on the visitor's CPU. It is not a "WASM server"; there is no
server.

### It is not speculative

`chrismaltby/gbdk-emscripten` ships this shape on npm today, for the z80 ports:

| artifact | size |
|---|---|
| `sdcc.wasm` | 0.88 MB |
| `sdcpp.wasm` | 0.22 MB |
| `as-gbz80.wasm` + `link-gbz80.wasm` | 0.14 MB |

About **1.3 MB of WASM** for a complete single-port toolchain, against a page
that already loads Pyodide — several times larger. What we need is the same
build with mcs51 instead of z80.

### Two constraints that decide the build

**Single-threaded, no exceptions.** GitHub Pages cannot set response headers.
`SharedArrayBuffer` and WASM threads require cross-origin isolation, which
requires `Cross-Origin-Opener-Policy` and `Cross-Origin-Embedder-Policy` on the
response. A threaded build would pass every test on the builder's machine and
fail on the only host it is for. (There is a service-worker shim that fakes the
headers; it reloads the page on first visit, which is a poor trade for
something whose appeal is being a static file.)

**Pin 4.5.0, not the 4.0.0 this service uses.** WASM has no glibc, so the
constraint that pins the hosted build does not exist in the browser. Building
4.5.0 does not merely replace this service — it ends the divergence documented
above. If the WASM build comes out matching 4.0.0, that is a failure.

The acceptance test is byte-identical `.hex` against native SDCC 4.5.0 for all
nine `stc12c5a60s2-lab` examples, run in CI rather than by hand, because every
measurement in that project was made with 4.5.0 and a subtly different compiler
would silently invalidate them.

### AVR is deliberately not part of this

The same page compiles Arduino/AVR targets with `avr-gcc`, and that is a far
bigger animal. Measured from the exact Debian packages `fetch-avr-gcc.sh`
vendors:

| binary | native x86-64 |
|---|---|
| `cc1plus` — Arduino sketches are C++ | 12.9 MB |
| `cc1` — C only | 11.7 MB |
| `lto1` | 10.9 MB |
| `avr-as` / `avr-ld` / `avr-objcopy` | 1.0 / 1.3 / 0.9 MB |
| full install | 99 MB |

The minimum viable set is smaller than the install suggests. `lto1` goes (no
LTO in a browser) and `cc1` goes too *if* the Arduino core is precompiled to
`core.a` at build time — which is what the Arduino IDE already does — leaving
only the user's C++ sketch to compile. That is **16.1 MB of native code**,
because `cc1plus` emits assembly text and something still has to assemble and
link it.

Estimating the WASM step: roughly **16–25 MB of `.wasm`, 7–11 MB compressed** —
about 8× the SDCC payload per visitor, plus GMP, MPFR and MPC to port, which
SDCC does not need. For scale on how wrong this can go, Wokwi compiled GDB with
Emscripten, got ~90 MB, and abandoned it for a Linux VM in the browser. That
was an unoptimised build of a bigger, threaded program, so it is not a
prediction — but no optimised `avr-gcc` WASM build exists publicly to measure.

Worth recording that one worry turned out to be unfounded: `cc1plus` links
`libc, libdl, libgmp, libm, libmpc, libmpfr, libz` and **no libpthread**. A
compiler is a single-threaded batch program; GDB needs threads for target
control, which is GDB's problem. So AVR-in-the-browser is not blocked by the
Pages header limitation — it is only expensive.

### The decision, taken 2026-08-10

Three options were on the table. **A** — drop browser AVR compiling, keep
transpile-and-download. **B** — keep a small hosted service for AVR only.
**C** — port `avr-gcc` to WASM as well.

**Chosen: A now, B then C later.**

- **A now.** This is an 8051 project; AVR arrived as a bonus target. Spending
  the single largest engineering item on the bonus, to serve the audience least
  in need of it — Arduino users already have a toolchain — is the wrong
  allocation. The page still emits the `.ino`; only in-browser compiling goes.
- **B before C.** If losing the capability turns out to bite, a small hosted
  service for one endpoint restores it immediately at almost no cost.
- **C last, and not never.** It is expensive, not impossible — the earlier claim
  that Pages could not host it was wrong, since `cc1plus` links no `libpthread`.

**What makes C cheaper than the estimate above.** `avr-gcc` needs GMP, MPFR and
MPC, which SDCC does not. `CrispStrobe/math-stack-ios-builder` carries
`build_wasm_deps.sh`, which builds GMP 6.3.0, MPFR 4.2.2 and MPC 1.3.1 to
WebAssembly with Emscripten, and its header records the two traps that cost the
time — GMP needs `--host=none --disable-assembly`, and `CC_FOR_BUILD` pointed
at a native compiler. Be precise about what that is worth: that repo's
`WASM_BUILD_PLAN.md` says "validated plan, not yet executed". It is a
substantial head start on the dependency layer, not a solved one — and it says
nothing about GCC itself, which remains the larger half.

Revisit C if someone actually asks for browser AVR compiling. Deferring it does
not block the 8051 path going fully static.

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

## Licensing

- **This wrapper** — MIT, see [`LICENSE`](LICENSE).
- **SDCC** — GPL-2.0-or-later. Its runtime libraries and headers carry a
  linking exception, so binaries you compile with it are unencumbered.
- **avr-gcc / arm-none-eabi-gcc** — GPL, with the GCC Runtime Library
  Exception on the runtime; **avr-libc** BSD-3-Clause; **cc65** zlib.
- **ATTinyCore** — LGPL-2.1, vendored as a minimal subset, compiled
  server-side only.
- **Keil C51 is proprietary** (Arm), Windows-only, redistribution prohibited.
  It can never be vendored. The translator was written from *published
  documentation* and third-party Keil *source*, never from its compiler source.
- STC's own headers and the corpus are never vendored: almost all of it carries
  no licence. Register addresses are facts; they are written from the datasheet.
- Full detail, including why serving it over HTTP is not distribution:
  [`NOTICE.md`](NOTICE.md).

---

## Reference

- Datasheet: <https://www.stcmicro.com/datasheet/STC12C5A60S2-en.pdf> (2011-07-15)
- `stcgal`: <https://github.com/grigorig/stcgal> (MIT) · `-P stc12`
- SDCC: <https://sdcc.sourceforge.net/> · headers in `mcs51/stc12.h`
- Prior-art Keil converters, both MIT, both covering only ground we already had:
  `CSY-tvgo/Keil-C51-C-to-SDCC-C-Converter`, `ywaby/keil2sdcc`
- Hackaday "Notes on converting Keil 8051 C to SDCC C" — project 170540, log
  177065. Fully mined; where it suggests lossy workarounds (bit→char), we do
  better.
- `cjacker/opensource-toolchain-8051` difference-between-c51-and-sdcc.md —
  six items, all subsumed.
