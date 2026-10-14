# esp8266 — provenance

The blob-free ESP8266 runtime the `esp8266` target links every program with:
start-up, serial output, a millisecond clock (`runtime.c`, `bw8266.h`), the
linker script (`bw8266.ld`), the ROM entry-point table (`rom.ld` — names and
addresses only, no ROM code) and an Arduino-style layer
(`arduino/Arduino.h`, `arduino/arduino.c`).

- Repository: [CrispStrobe/esp8266js](https://github.com/CrispStrobe/esp8266js)
  (our fork of urish/esp8266js)
- Commit: `66cdf43ced59b7a89c230994de4a7e62a16f4de6`
- Directory: `firmware/` — copied unmodified

| file here | from | sha256 |
|---|---|---|
| `bw8266.h` | `firmware/bw8266.h` | `43bdfe33919212b3dbdbae1fdf3906472f780c6730fb2e5fd2d2ac748298a9cd` |
| `bw8266.ld` | `firmware/bw8266.ld` | `60238ff5080109bea05d3d75897e5ffba9c6149370843e3a2b61bfdeaa6d4357` |
| `rom.ld` | `firmware/rom.ld` | `e4f2e5d74879c94390cea19aa217a3539e68273025b0af87d8ab64befd622b3b` |
| `runtime.c` | `firmware/runtime.c` | `c6edd62e8a9896f685a24b5c5ec532a596e025c3b040cc781626c900309a7807` |
| `arduino/Arduino.h` | `firmware/arduino/Arduino.h` | `3fc99584f26e965427a796f091c537c2be5b44fcce7fb4ecc0b89d1777fc5534` |
| `arduino/arduino.c` | `firmware/arduino/arduino.c` | `11f2d4e8fc9f7c6f3187d4559f342659a8a3642bd98bb5de8bea5fa0808312a9` |
| `LICENSE` | `LICENSE` | `3df01526a8a277cb34d5be806321bebd876120d588f90f5e4bffdfde00bb7bee` |

Also from the same commit, as test fixtures in `scripts/fixtures/esp8266/`:

| file | from | sha256 |
|---|---|---|
| `elf2image.js` | `tools/elf2image.js` | `39599635088655e7bddef3b3603c5cb2d65da702fe16aa6e75cd19e65cdc53b9` |
| `button.c` | `firmware/sketches/button.c` | `7ac6a1d88363f4730e24a85e0b3ebc8e905574d54042373c35299a319480bab5` |
| `wave.c` | `firmware/sketches/wave.c` | `da28eee7d0d9fccc4cbb6d1599a1da82ec96aec8cc7b25f95767e434ddf039fe` |

`esp8266_image.py` at the repository root is a Python port of
`tools/elf2image.js`; `test_esp8266_build.py` checks its output is
byte-identical to the JavaScript's, run with node.

## Licence

**MIT** — `LICENSE` here, the esp8266js repository's licence text.

Nothing of the Espressif SDK (NONOS/RTOS), the Arduino ESP8266 core, the
chip's mask ROM or any stock firmware image is included or linked: a program
is these files, the user's sketch and the compiler's `libgcc.a`.
