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

**`PART <name> = KEYPAD4X4 ROWS <4 pins> COLS <4 pins>`** now exists
HERE first: a read-only part (the scanned key 0..15, -1 for none) whose
emitted scanner is the one verified on Prechin A2 silicon the same
evening (stc12c5a60s2-lab 06-matrix89/09-keyshow89). 8051 family only —
push-pull targets need row tri-stating before they may opt in.
sb3-creator's generateC does NOT have it yet; until it does, keypad
programs compile through this service only (`compile-remote.sh -p`).
