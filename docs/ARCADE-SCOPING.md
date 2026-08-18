# Arcade target — scoping study (2026-08-18)

**What this is.** A scoping study for adding a MakeCode Arcade boundary-D target
to BrickWright, driven by the ElecFreaks micro:bit Arcade shield (160×120 colour
TFT, D-pad, A/B buttons). This follows the same "vendor an MIT sim, add a
lowering, plug a boundary-D target" pattern already proven by the MicroPython
micro:bit sim. See `MICROBIT-NATIVE.md §6a` for the owner's framing.

---

## 0. Provenance — all MIT, attribution only

Every component is MIT-licensed. Verified 2026-08-18 via GitHub license API.

| component | repo | licence | role |
|---|---|---|---|
| PXT platform + compiler | `microsoft/pxt` | MIT | TypeScript/Blocks → JS compiler, fiber runtime (`pxtsim.js`) |
| PXT Arcade game engine | `microsoft/pxt-arcade` | MIT | Arcade-specific libs (sprites, tilemaps, controller, screen) |
| PXT common packages | `microsoft/pxt-common-packages` | MIT | Shared runtime libs (game, scene, image, controller) |
| Arcade shield extension | `microbit-apps/display-shield` | MIT | Arcade-on-micro:bit (Kittenbot/ElecFreaks/Game:bit shields) |
| StarPilots reference game | `marceld23/StarPilots` | MIT | Full arcade space-shooter for ElecFreaks Retro, EN/DE |
| Code Club missions | `aliblol/code-club-missions` | MIT | Pedagogy reference for arcade + micro:bit curriculum |

**No clean-room constraints.** Unlike the `pxt-mbit-more-v2` firmware (unlicensed,
concepts only), every piece here is MIT and adoptable with attribution. Record
provenance in `NOTICE.md` when shipping.

---

## 1. The Arcade simulator — architecture and vendoring path

### 1a. How the sim works today

The MakeCode Arcade simulator runs at `trg-arcade.userpxt.io/---simulator` as an
HTML page loading two JS blobs from `cdn.makecode.com`:

- **`pxtsim.js`** (~256 KB) — the universal PXT runtime: fiber scheduler,
  postMessage dispatch, audio, debugger protocol. Source is public in
  `microsoft/pxt/pxtsim/` (MIT).
- **`sim.js`** (~256 KB est.) — the Arcade board skin: SVG shell, canvas
  renderer, controller input, palette engine. Source is in the **private**
  `pxt-arcade-sim` repo. The CDN artifact is MIT-licensed but not rebuildable.

### 1b. The postMessage protocol (host ↔ sim iframe)

The sim is driven entirely via `postMessage`. This is the same architectural
pattern as our existing MicroPython sim (`microbit-sim-pane.jsx`).

**Inbound (host → sim):**

| `type` | payload | effect |
|---|---|---|
| `run` | `{code, boardDefinition, parts}` | Start execution — `code` is compiled JS string |
| `stop` | — | Kill runtime |
| `debugger` | `{subtype: 'pause'}` | Pause execution |
| `debugger` | `{subtype: 'resume'}` | Resume |
| `debugger` | `{subtype: 'stepover\|stepinto\|stepout'}` | Debugger stepping |
| `debugger` | `{subtype: 'config', setBreakpoints: [...]}` | Set breakpoints |
| `debugger` | `{subtype: 'traceConfig', interval: ms}` | Set trace interval |
| `mute` | — | Mute/unmute audio |
| `recorder` | `{action: 'start\|stop'}` | Screenshot/GIF capture |
| `setactiveplayer` | `{player: N}` | Switch active player (up to 4) |

**Outbound (sim → host):**

| `type` | payload |
|---|---|
| `ready` | sim initialised |
| `status` | `{state: 'running\|killed'}` |
| `toplevelcodefinished` | main fiber completed |
| `debugger` | `{subtype: 'breakpoint', ...}` — hit a breakpoint |
| `debugger` | `{subtype: 'warning\|trace', ...}` |
| `aspectratio` | requested aspect ratio |

### 1c. The vendoring question

**Approach A — pin CDN blobs (fast, limited).** Download and pin the two
content-addressed CDN blobs, wrap in a thin HTML shell, serve from our domain.
Pro: works today. Con: `sim.js` is an opaque minified blob — auditable but not
modifiable. If we need to add boundary-D hooks (halt position, game-state
inspection), we cannot patch it.

**Approach B — build from source (preferred).** `pxtsim.js` is fully buildable
from `microsoft/pxt/pxtsim/`. For `sim.js`, two paths:
1. The `microbit-apps/display-shield` extension ships its **own** in-browser
   Arcade simulator (MIT, public source). It is a subset (160×120, 16 colours,
   keyboard input) — but it IS the exact simulator for the hardware we target.
   This is the cleaner vendoring path: build from public MIT source, same as we
   did for the MicroPython WASM sim.
2. Alternatively, build a minimal Arcade board renderer ourselves using the
   public `pxt-common-packages` runtime. The sprite/tilemap/screen APIs are all
   MIT and documented.

**Recommendation: Approach B, path 1** — vendor the `display-shield`'s sim
implementation (MIT, matches our hardware exactly), plus `pxtsim.js` from
`microsoft/pxt`. This gives us source-level control for boundary-D integration
without depending on a private repo's minified output.

### 1d. Comparison: Arcade sim vs MicroPython sim

| dimension | MicroPython sim (current) | Arcade sim (proposed) |
|---|---|---|
| artefact | `firmware.wasm` (1.1 MB) + `simulator.js` | `pxtsim.js` + `sim.js` (~512 KB total) |
| source | fully public MIT | `pxtsim` public; board skin via display-shield (MIT) |
| execution model | Python bytecode interpreter (WASM) | JS fiber scheduler (native browser JS) |
| display | 5×5 red LED matrix | 160×120 colour TFT, 16-colour palette |
| input | 2 buttons + accelerometer + pins | D-pad (4-way) + A + B + Menu + Reset |
| game primitives | none (imperative `display.set_pixel`) | sprites, tilemaps, game loop, scene transitions |
| debug surface | step/stepEndPosition/getState/resume | pause/resume/stepover/stepinto/stepout/breakpoints |
| boundary-D fit | `{kind: 'py-frames'}` position | `{kind: 'arcade-fiber'}` position (fiber + game tick) |

---

## 2. Compiler — dialect-to-Arcade lowering

### 2a. The block surface (graphics/game blocks)

A new block group in the dialect, sharing the front half of the pipeline
(parsing, AST) but lowering to Arcade TypeScript instead of C or MicroPython.
The blocks map directly to Arcade APIs:

| dialect verb | Arcade API | notes |
|---|---|---|
| `arcade sprite NAME IMAGE` | `sprites.create(img, SpriteKind.NAME)` | creates a sprite from a palette image |
| `arcade move SPRITE vx VX vy VY` | `sprite.setVelocity(vx, vy)` | |
| `arcade place SPRITE x X y Y` | `sprite.setPosition(x, y)` | |
| `arcade on overlap A B` | `sprites.onOverlap(KindA, KindB, handler)` | hat block |
| `arcade tilemap NAME MAP` | `scene.setTileMap(tilemap)` | set the tilemap |
| `arcade score add N` | `info.changeScoreBy(n)` | |
| `arcade life add N` | `info.changeLifeBy(n)` | |
| `arcade game over WIN` | `game.over(win)` | |
| `arcade screen fill COLOR` | `scene.setBackgroundColor(color)` | |
| `WHEN controller UP pressed` | `controller.up.onEvent(ControllerButtonEvent.Pressed, handler)` | hat block |
| `WHEN controller A pressed` | `controller.A.onEvent(...)` | hat block |
| `WHEN game update` | `game.onUpdate(handler)` | the game loop tick |
| `WHEN game update every MS` | `game.onUpdateInterval(ms, handler)` | timed game loop |

### 2b. Pipeline architecture

```
dialect pseudocode
    │
    ├── parse() ──▶ AST (shared with C/MicroPython)
    │
    ├── emit_c()            → SDCC → .hex        (8051/Z80/6502)
    ├── emit_micropython()  → MicroPython sim     (micro:bit)
    └── emit_arcade_ts()    → PXT compile → JS    (Arcade sim / shield)
```

The new `emit_arcade_ts()` backend produces TypeScript that the PXT compiler
(running in a Web Worker, from `microsoft/pxt`) compiles to the JS fiber format
the sim's `run` message expects. This is the same architecture MakeCode itself
uses: TS → JS-in-browser, no ARM compilation needed for simulation.

### 2c. What is NOT shared with the C backend

The Arcade target has capabilities the 8051 C target does not:

- **Sprites with physics** (velocity, acceleration, overlap detection)
- **Tilemaps** (tile-index grid with collision flags)
- **Scene management** (background colour, camera follow, screen shake)
- **Game state** (score, lives, countdown, game-over)
- **Multi-player** (up to 4 controllers)
- **Palette-indexed images** (4-bpp, 16-colour)

These are NOT lowered to C. They exist only in the Arcade lowering. The front
half of the pipeline (parsing, AST, control flow) is shared; the back half
diverges completely.

### 2d. What IS shared

- Control flow: `FOREVER`, `REPEAT`, `IF/ELSE`, `WAIT`
- Variables, expressions, operators
- `WHEN flag clicked` → `game.onUpdate` (the Arcade equivalent)
- `print` → `game.splash` or HUD text
- Sound/music → `music.playTone` (Arcade has a sound engine)

---

## 3. Run target — boundary-D capability column

The Arcade sim becomes a new `DebugTarget` implementation. Its capability column:

| capability | Arcade sim |
|---|---|
| halt / resume | **yes** — `debugger {subtype: 'pause/resume'}` |
| step instruction | **no** — JS fibers, not machine instructions |
| step source line | **yes** — `debugger {subtype: 'stepover'}` |
| step block (yield→yield) | **partial** — game tick granularity via `onUpdate` |
| breakpoint at source line | **yes** — `debugger {subtype: 'config', setBreakpoints}` |
| data watch | **TBD** — depends on runtime variable inspection hooks |
| read variables while halted | **yes** — debugger trace messages include locals |
| program time freezes while halted | **yes** — fiber scheduler paused |
| physical world freezes | **n/a** (sim) / **no** (real shield on micro:bit) |

Position model: `{kind: 'arcade-fiber', fiber: id, line: N, tick: T}` — which
fiber is active, at what source line, in which game tick. This extends the
`HaltReason.position` tagged union from `MICROBIT-NATIVE.md §3`:

```
position:
  | {kind: 'yield-tasks', tasks: [...]}      // 8051
  | {kind: 'py-frames', frames: [...]}       // micro:bit MicroPython
  | {kind: 'arcade-fiber', fiber, line, tick} // Arcade
```

---

## 4. The tie to existing infrastructure

### 4a. Controller panel (DESIGNED, lane bw-blocks)

The `ROADMAP.md` Controller panel (joystick, D-pad, buttons, sliders) is the
input half of this game engine. The Arcade D-pad and A/B buttons are exactly the
widgets the Controller panel provides. Build them together:

- Controller panel widgets → `setactiveplayer` + key events → Arcade sim
- Same widgets → `board.setControl` → 8051/micro:bit targets

### 4b. Display infrastructure (already built)

BrickWright already drives:
- LCD (HD44780, I2C)
- TFT (ILI9341 SPI, 240×320, RGB565)
- OLED (SSD1306, I2C, 128×64)
- LED matrix (8×8, ISR-scanned)

The Arcade screen (160×120, 16-colour palette) is the same class of output, one
notch richer with sprites/tilemaps. The compiler already knows how to emit
display driver code; the Arcade target substitutes Arcade API calls for raw
driver calls.

### 4c. Multi-platform game engine trajectory

This is not a micro:bit accessory. The Arcade sim is one target of a broader
graphics engine. Others on the same trajectory:
- ZX Spectrum (already has a sim/emu)
- 6502 VDP (TMS9918 — sprites, tilemaps, same class)
- TFT part (ILI9341 — already emitting driver code)
- Future: browser-native canvas target (no WASM)

---

## 5. Staging — each stage shippable

**Stage 0 — Vendor the sim.** Pin `pxtsim.js` (built from `microsoft/pxt`) +
the `display-shield` board renderer (MIT), or pin the CDN blobs as a faster
first step. Wrap in an iframe shell with the postMessage bridge. Acceptance: a
hardcoded Arcade JS program runs in the vendored sim, sprites move, D-pad works.

**Stage 1 — Compiler backend.** `emit_arcade_ts()` produces TypeScript from the
dialect AST. PXT compiles it to JS in a Web Worker. Oracle: `dialect → TS → JS →
sim` produces the expected sprites/movement/score. Start with: sprite create,
move, overlap, controller input, game over.

**Stage 2 — Boundary-D integration.** Implement the Arcade `DebugTarget` adapter
over the postMessage debug protocol. Wire pause/resume/step/breakpoints into the
existing debug panel, which already branches on `capabilities()`. The Arcade
column greys out `insn`/`block` step and lights `line` step + breakpoints.

**Stage 3 — Controller panel.** Wire the Controller panel's D-pad/button widgets
to the Arcade sim's input protocol. Same widgets serve both the Arcade target
and hardware targets via `board.setControl`.

**Stage 4 — Real hardware.** Flash Arcade hex to the micro:bit + ElecFreaks
shield via dapjs/WebUSB. The `display-shield` extension handles the TFT driver
on-device. This is the same flasher path `MICROBIT-NATIVE.md` Stage 4 uses.

---

## 6. Risks and open questions

1. **`sim.js` source availability.** The Arcade board skin's source is in a
   private Microsoft repo. The `display-shield` sim is the MIT alternative, but
   it may lack features the full Arcade sim has (audio, multi-player, full
   palette). Scope the delta before committing.

2. **PXT compiler in-browser.** Running the PXT TypeScript compiler in a Web
   Worker is feasible (MakeCode does it) but adds ~2 MB of JS. Alternative:
   pre-compile the dialect's TS output server-side (like stc-compiler does for
   SDCC). Trade-off: latency vs bundle size.

3. **Image/tilemap editors.** Arcade programs use palette-indexed images and
   tilemaps. MakeCode has rich editors for these (sprite editor, tilemap editor).
   We would need equivalent editors or a simpler interface (e.g., import from
   PNG, text-based tilemap definition).

4. **Game tick vs line-step granularity.** The Arcade debugger steps by source
   line, but game behaviour is meaningful at game-tick granularity. The debug
   panel may need a "step one tick" mode that is specific to the Arcade column.

---

## 7. Prototype — StarPilots-style example skeleton

See `examples/arcade-blink/` for a minimal dialect program that demonstrates the
Arcade block surface: sprite creation, movement, controller input, and game-over
condition. This is the "hello world" for the Arcade target — the simplest program
that exercises the core game-engine primitives.

The example uses the dialect syntax proposed in §2a and serves as the acceptance
test for Stage 1 of the compiler backend.
