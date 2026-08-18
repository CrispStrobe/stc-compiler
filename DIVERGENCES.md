# DIVERGENCES — the reference dialect vs the sb3-creator dialect

The reference implementation here is the test oracle for the shared
pseudocode dialect, but the sb3-creator (JS) dialect has grown VERB
FAMILIES this parser does not know yet. Recorded 2026-08-17, when the
owner's Pico calculator went from blocks to real silicon through
sb3-creator's `generateMicroPython` — a path this repo claims for the
Pico but cannot yet fully express.

Missing here, present in sb3-creator:

- **Display verbs**: `oled clear/print/set cursor` (SSD1306/SH1106 over
  bit-banged or hardware I2C), `lcd ...` (HD44780 via PCF8574),
  `tft ...` (ILI9341 SPI). sb3-creator lowers these on 8051, AVR, ARM
  and (oled) MicroPython-Pico; the C drivers live in its generateC.
- **Device verbs**: servo/motor (PCA + AVR/ARM PWM), neopixel, matrix,
  cube.
- **MicroPython Pico flavor extras**: internal pull selection from pin
  polarity (PULL_UP for ACTIVE LOW, PULL_DOWN otherwise), page-mode
  OLED driver (the GME12864-70 is an SH1106 — SSD1306 horizontal
  addressing shows noise), DEFINEs as generators with yield-from.

Closing this is real parser + emitter work per family, not a patch:
each verb needs grammar, AST, and lowering in stmts_c/stmts_task plus
`bw_micropython`. Until then, programs using those verbs must be
compiled by sb3-creator (`bw transpile` / the app); this service still
builds the C that sb3-creator EMITS (its /compile endpoints are
dialect-agnostic C/ASM toolchains).

## Divergence in the OTHER direction (2026-08-17, evening)

**`PART <name> = KEYPAD4X4 ROWS <4 pins> COLS <4 pins>`** — PARITY
CLOSED (sb3-creator a045ecc, 2026-08-18): the part exists at every
sb3-creator layer (dialect parser with matching refusals, stc12_keypad
palette reporter, the silicon-verified C scanner, C reader, decompiler)
and this oracle's own 14-a2-keyshow.bw round-trips
pseudocode→C→pseudocode→C byte-identically over there. Historical
entry follows. Existed HERE first: a read-only part (the scanned key 0..15, -1 for none) whose
emitted scanner is the one verified on Prechin A2 silicon the same
evening (stc12c5a60s2-lab 06-matrix89/09-keyshow89). 8051 family only —
push-pull targets need row tri-stating before they may opt in.
sb3-creator's generateC does NOT have it yet; until it does, keypad
programs compile through this service only (`compile-remote.sh -p`).

**`not` precedence (2026-08-17, same evening):** this service now parses
`not` at Python's level — looser than comparisons, tighter than and/or —
because `IF not k = shown` mis-parsing as `(not k) = shown` shipped a
flashed-clean, silently-dead program to real silicon before the bench
caught it. sb3-creator's reader/emitter must match or `not a = b`
round-trips with changed meaning; verify its parser when closing parity.

**`PART <name> = MATRIX8X8 ROWS 74HC595 DATA <p> CLOCK <p> LATCH <p>
COLUMNS <port>` — landed in reference, NOT yet mirrored to sb3-creator
(2026-08-18).** An 8×8 LED dot matrix that refreshes itself in the Timer-0
ISR (one row per tick → 125 Hz), so the drawing verbs are plain
frame-buffer writes and the `WHEN` block keeps running. Measured A2 wiring
(docs/BOARD-PRECHIN-A2.md): 595 rows active-HIGH Q7=top, port columns
active-LOW bit7=left — both baked into the scan so image bytes read
top-down / MSB-left. 8051 family only (needs a whole port + the ISR tick),
like KEYPAD4X4. Frame buffer is **bit-plane packed, 2 planes / 4 levels /
16 bytes** (`MATRIX_PLANES`/`MATRIX_LEVELS` are the single widen-point to
16 levels / 32 bytes). This landing renders **threshold** (lit iff level ≠
0 = OR of the planes); the ISR carries a clearly-marked BCM seam
(`bw_scr_<name>_phase`) for later, ISR-only grayscale — Layer 2, a joint
timer decision, deferred. The ISR is the **sole writer** of the 595 and
the column port (the PART claim refuses PIN/PORT on those pins), which is
what closes the 8051 read-modify-write hazard.

New vocabulary → C, all writing the RAM frame buffer only:
- `clear screen` → `bw_scr_<n>_clear()`
- `light pixel X Y` / `clear pixel X Y` → `bw_scr_<n>_setpx(x,y, MATRIX_LEVELS-1 | 0)`
- `set pixel X Y to on|off` → same, level MAX / 0
- `set pixel X Y brightness B` → `bw_scr_<n>_setpx(x,y, bw_scr_level(B))`
- `draw row Y = <byte>` → `bw_scr_<n>_row(y, bits)` (1-bit → full, 0 → off)
- `show image <table> on screen` → `bw_scr_<n>_image(bw_tab_<table>)` (1-bit blit)
- `scroll screen left|right|up|down` → `bw_scr_<n>_scroll(0|1|2|3)`
- `set screen brightness B` → `bw_scr_<n>_dim = bw_scr_level(B)`
- reporter `pixel X Y is on` → `(bw_scr_<n>_getpx(x,y) != 0)`

Mirror carefully: (a) presence of a MATRIX8X8 must force sb3-creator's
cooperative-scheduler / ISR path even for a single script (here
`Program.has_matrix` feeds the `tasks` decision); (b) the scan hook goes in
the Timer-0 ISR **after `bw_ms++`, table-driven, no mul/div**; (c) a whole
`PORT` overlapping a PART's claimed pins is now refused in BOTH directions
(this fixed a pre-existing gap that also covered 595/keypad).

**`PART <name> = SEVENSEG8 SEGMENTS <port> SELECT <A> <B> <C>
[COMMON CATHODE|ANODE]` — mirror CLOSED same day (sb3-creator 952b623).** An 8-digit 7-segment display, multiplexed via
a 74HC245 (segments on a whole port) and a 74HC138 (3 address pins for
digit select). ISR-driven: one digit per tick from an 8-byte frame buffer
in RAM, 8 digits → 8 ms full cycle = 125 Hz. Built-in 0-F font in __code.
bw_ms++ stays first in the ISR per requirement #1. 8051 family only.

New vocabulary → C, all writing the RAM frame buffer only:
- `show number N on display` → `bw_<n>_show_number(N)` (integer, right-aligned)
- `show digit D = value V on display` → `bw_<n>_show_digit(D, V)` (one tube, 0-F)
- `set digit D to segments <byte> on display` → `bw_<n>_set_segments(D, segs)` (raw)
- `clear display` → `bw_<n>_clear()`

**`PART <name> = LEDBANK8 ON <port> [ACTIVE LOW]` — mirror CLOSED same
day (sb3-creator 952b623).** 8 LEDs on a port. All
writes go through an ISR-owned shadow byte — never direct port stores.
Emits a compile WARNING when sharing a port with a SEVENSEG8's select pins
(the A2 board's measured conflict: P2 carries both the 138 select and the
LEDs). The ISR pushes the shadow byte to the port on every tick; mainline
only writes the shadow. 8051 family only.

New vocabulary → C:
- `turn on led N on bank` → `bw_<n>_on(N)` (set bit N in shadow)
- `turn off led N on bank` → `bw_<n>_off(N)` (clear bit N)
- `set leds to <byte> on bank` → `bw_<n>_set(byte)` (write all 8)
- `light only led N on bank` → `bw_<n>_only(N)` (one-hot)

Mirror notes: (a) both PARTs store in `program.parts` with `isinstance`
dispatch (like MatrixPart/KeypadPart); (b) SEVENSEG8 and LEDBANK8 force
the ISR path but NOT the cooperative scheduler — a single WHEN block runs
straight-line with Timer 0 as the ISR scan; (c) the ISR scan hooks go
after bw_ms++ and after the matrix scan (if present), table-driven, no
mul/div; (d) `claimed` on SevenSegPart returns the 3 select pins only
(the segment port is claimed as a whole port, not individual pins).

## `mod` — closed 2026-08-18

sb3-creator's dialect spells modulo `a mod b` (three gallery programs use
it: 76-multimeter, arduino-02-state-change, eater6502-full-build); this
front end only knew `%`. Worse than a missing feature, the unknown word
ended expressions early: `font[copy mod 10]` died with "missing ']' after
font[". Fixed with a one-entry `WORD_OPS` table (`mod` → `%`) applied in
the precedence loop, so `mod` binds exactly like `*` and `/`. Both
spellings now parse; `test_mod_operator.py` pins it.

The same program exposed sb3-creator's mirror-image bug: its operator
splitter did not track `[` depth, so `font[copy mod 10]` split at ` mod `
and compiled SILENTLY to string arithmetic (`("font[copy" % "10]")`) with
zero warnings — worse than this side's loud error. Fixed there the same
day (splitBinary tracks `[]` like `()`; pinned in keypad-parity.test.mjs).
Cross-check: examples/78-a2-calculator/program.bw now parses in BOTH
front ends unchanged.

## Keypad reporters + `WHEN key N pressed` hats (2026-08-18) — CLOSED same day (sb3-creator 4962d4d)

The A2-BOARD-SUPPORT fan-out item. New vocabulary, all over the sole
KEYPAD4X4 (sole_keypad mirrors sole_matrix):

- `a key is pressed` — DESUGARS at parse time to `<pad> >= 0`; canonical
  printed form is the desugared one, so the fixed point is `keys >= 0`.
- `key N is pressed` / `key N is released` — desugars to `<pad> = N` /
  `not (<pad> = N)`. N range-checked 0..15. A four-token lookahead guard
  (`key <digit> is pressed|released`) keeps a VARIABLE named `key` (the
  keyshow example has one) parsing as a variable.
- **`WHEN key N pressed:` / `released:`** — edge hats. Lowered like the
  pin hats (polled task, `_prev` edge state) but on a shared DEBOUNCED
  scan: one `bw_kp_<pad>_poll` task per keypad, dispatched before the
  hats, reads the matrix at most every 5 ms and only updates
  `bw_kp_<pad>_key` after two agreeing reads. A hat forces the
  cooperative scheduler like a pin hat does. `when_hats` entries:
  pin hat = (pin, edge) 2-tuple, key hat = (pad, edge, n) 3-tuple.

Mirror notes for sb3-creator: the reporters need no new opcodes if the
desugar happens at parse time there too (they become plain compare
blocks over stc12_keypad); the hat needs a real hat block + the shared
poll in generated C byte-shaped like this side's (test_keypad.py
TestKeypadHats pins the C), plus Py/JS scheduler equivalents.

Mirror landed as sb3-creator 4962d4d: desugar in parseCondition (blocks
identical to hand-written `keys >= 0` / `keys = N`), `stc12_whenkey` hat
with byte-shaped poll + task C, reference-extension HAT block. Mirroring
exposed that sb3-creator's EXISTING pin-hat round trip was a fiction —
cToPseudocode degraded every hat task to `WHEN flag clicked:`. Its
`@bw script` marker now carries `hat pin <name> <edge>` / `hat key <n>
<edge>` and both hat kinds round-trip as fixed points. One convention
divergence stays by design: this side RAISES on a missing keypad or
key > 15, sb3-creator warns-and-degrades (its house style).

## MicroPython KEYPAD4X4 (2026-08-18) — sb3-creator's MP path must mirror

The settled shared-scanner lane (stc12c5a60s2-lab
docs/A2-BOARD-SUPPORT.md aabc0ad) is BUILT here for both MicroPython
targets: `supports` gains "keypad" on Pico and micro:bit, the scanner is
emitted per part in each board's Pin vocabulary (Pico: rows
`init(Pin.OUT, value=0)` / released to `init(Pin.IN)` — TRI-STATED, an
RP2040's push-pull rows short under two held keys in one column;
micro:bit: `write_digital(0)` / `read_digital()` as the tri-state,
columns `set_pull(PULL_UP)`), same unrolled shape and 0..15 index as the
silicon-verified 8051 C. The `WHEN key N` hats ride a shared debounced
poll GENERATOR scheduled first (`bw_kp_<pad>_poll`, 5 ms gate —
`ticks_diff` on the Pico, plain arithmetic on running_time() — two
agreeing reads). Verified behaviorally on the host against a mocked
machine.Pin: press fires once, held does not re-fire, release edge
fires, a 3 ms glitch is debounced away. test_keypad.py
TestKeypadMicroPython pins the emission.

Mirror note: sb3-creator's own MicroPython generator still 8051-gates
PART KEYPAD4X4 (its parser refuses the part off-8051), so a keypad
program that compiles here for DEVICE PICO is refused there. The mirror
is: lift the gate for pico/microbit and lower scan + index + hats in its
generator-based MP walk, following this emission.

SEVENSEG8/LEDBANK8 mirror detail (952b623): ISR advance, shadow push,
font bytes and helper bodies line-identical (test/a2-parts-parity pins
them against this side's exact lines); shared-port warning mirrored; @bw
part markers make the C round-trip a fixed point. One deliberate
divergence: mirror note (b) said these force the ISR but not the
scheduler — sb3-creator instead forces its scheduler path even for a
single WHEN (the matrix precedent: its ISR only exists there). Same ISR
bytes, different main() shell.
