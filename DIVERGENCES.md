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
