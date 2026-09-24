# Third-Party Notices & Attribution

`stc-compiler` is distributed under the **MIT** license (see [`LICENSE`](LICENSE)),
which covers **only CrispStrobe's original wrapper code** — `app.py`, the
deployment configuration, and `scripts/fetch-sdcc.sh`.

The repository also ships **pre-compiled third-party compiler binaries and
their headers and libraries**, which are *not* CrispStrobe's work and retain
their own upstream licenses.

| Vendored artifact | Upstream project | License |
|---|---|---|
| `bin/sdcc`, `bin/sdcpp`, `bin/sdas8051`, `bin/sdld`, `bin/packihx`, `bin/makebin` | [SDCC — Small Device C Compiler](https://sdcc.sourceforge.net/) | **GPL-2.0-or-later** |
| `share/sdcc/include/**`, `share/sdcc/lib/**` | SDCC runtime headers and libraries | **GPL-2.0-or-later with a linking exception** (see below) |
| `avr/bin/**`, `avr/lib/gcc/**`, `avr/libexec/**` | [GCC](https://gcc.gnu.org/) for AVR + GNU binutils | **GPL-3.0-or-later**, runtime under the **GCC Runtime Library Exception** |
| `avr/lib/avr/include/**`, `avr/lib/avr/lib*/**` | [avr-libc](https://github.com/avrdudes/avr-libc) | **BSD-3-Clause** |
| `arm/bin/**`, `arm/lib/gcc/**`, `arm/libexec/**` | GCC for `arm-none-eabi` + GNU binutils | **GPL-3.0-or-later**, runtime under the **GCC Runtime Library Exception** |
| `avr/lib-deps/**`, `arm/lib-deps/**` | the shared libraries those compilers link (GMP, MPFR, MPC, zlib, …) | LGPL-3.0-or-later / zlib, as each upstream states |
| `cc65/bin/**`, `cc65/lib/**`, `cc65/include/**`, `cc65/asminc/**` | [cc65](https://github.com/cc65/cc65) | **zlib** (Debian: BSD-3-zlib) |
| `arduino-core/cores/arduino/**`, `arduino-core/variants/{standard,eightanaloginputs,mega}/**`, `arduino-core/libraries/arduino/**` | [ArduinoCore-avr](https://github.com/arduino/ArduinoCore-avr) 1.8.8 by Arduino | **LGPL-2.1-or-later** (see below) |
| `arduino-core/cores/tiny/**`, `arduino-core/variants/{tinyx5,tinyx8}/**`, `arduino-core/libraries/tiny/**` | [ATTinyCore](https://github.com/SpenceKonde/ATTinyCore) by Spence Konde | **LGPL-2.1-or-later** (see below) |
| `arduino-core/libraries/arduino/Servo/**` | [Servo](https://github.com/arduino-libraries/Servo) 1.3.0 by Arduino | **LGPL-2.1-or-later** (see below) |
| `arduino-core/libraries/common/LiquidCrystal/**` | [LiquidCrystal](https://github.com/arduino-libraries/LiquidCrystal) 1.0.7 by Arduino | **LGPL-2.1-or-later** (see below) |
| `arduino-core/libraries/common/Adafruit_NeoPixel/**` | [Adafruit_NeoPixel](https://github.com/adafruit/Adafruit_NeoPixel) 1.15.5 by Adafruit | **LGPL-3.0** (see below; licence text in its `COPYING`) |
| `riscv/riscv-cc.wasm` | [shecc](https://github.com/sysprog21/shecc) — RV32IM C compiler, built to `wasm32-wasi` | **BSD-2-Clause** |
| `riscv-gcc/bin/**`, `riscv-gcc/lib/gcc/**`, `riscv-gcc/lib/riscv64-unknown-elf/bin/**` | [GCC](https://gcc.gnu.org/) for `riscv64-unknown-elf` + GNU binutils | **GPL-3.0-or-later**, runtime under the **GCC Runtime Library Exception** |
| `riscv-gcc/picolibc/**` | [picolibc](https://github.com/picolibc/picolibc) — the C library | **BSD-2/3-Clause** (a few files under other permissive terms) |
| `riscv-gcc/lib-deps/**` | the shared libraries the compiler links (GMP, MPFR, MPC, ISL, zlib) | LGPL-3.0-or-later / zlib, as each upstream states |

### Provenance

Every bundle records where it came from and how to get its corresponding
source, in `vendor/<name>/`:

| bundle | source | provenance |
|---|---|---|
| SDCC | Debian bullseye `sdcc` + `sdcc-libraries` `4.0.0+dfsg-2`, unmodified | `vendor/sdcc/VERSION`, `vendor/sdcc/copyright` |
| avr-gcc | Debian bullseye `gcc-avr`, `binutils-avr`, `avr-libc`, unmodified | `vendor/avr/VERSION` + three `.copyright` files |
| arm-none-eabi | Debian bullseye `gcc-arm-none-eabi`, `binutils-arm-none-eabi`, unmodified | `vendor/arm/VERSION` + two `.copyright` files |
| cc65 | **built from upstream source**, commit `547d923` — not a Debian package | `vendor/cc65/VERSION`, `vendor/cc65/LICENSE` |
| shecc | **built from upstream source**, commit `362b94b`, cross-compiled to `wasm32-wasi` — byte-reproducible, see `riscv/riscv-cc.PROVENANCE.md` | `vendor/shecc/VERSION`, `vendor/shecc/LICENSE` |
| riscv-gcc | Debian bullseye `gcc-riscv64-unknown-elf`, `binutils-riscv64-unknown-elf`, `picolibc-riscv64-unknown-elf`, unmodified — trimmed to the rv32imac/ilp32 multilib, DWARF stripped | `vendor/riscv-gcc/VERSION` + three `.copyright` files |

`scripts/fetch-sdcc.sh`, `scripts/fetch-avr-gcc.sh`,
`scripts/fetch-arm-gcc.sh` and `scripts/fetch-riscv-gcc.sh` reproduce those
four bundles exactly. The fetch scripts strip the bundles down — to one
target, one multilib, and without DWARF — which is a *subset*, not a
modification: no binary is patched, and the corresponding source for each is
the upstream Debian source package. (The RISC-V bundle also carries picolibc
as its C library, where the ARM bundle is freestanding.)

cc65 is the exception in every sense: it was compiled rather than repackaged,
so `vendor/cc65/VERSION` names the upstream commit instead of a `.deb`. Its
licence carries no copyleft and no linking condition, but its third condition
requires the notice to travel with a source distribution — which is what
`vendor/cc65/LICENSE` is for.

shecc is the same kind of exception, one step further: it was compiled *from
source to WebAssembly* (`wasm32-wasi`), so `riscv/riscv-cc.wasm` is a build
artifact, not a native bundle. `riscv/riscv-cc.PROVENANCE.md` pins the upstream
commit, the wasi-sdk version and a byte-reproducible build; `vendor/shecc/LICENSE`
carries its BSD-2-Clause notice, which the binary-redistribution condition
requires to travel with the wasm. It runs under wasmtime, so no native RISC-V
toolchain is hosted or staged.

## What the GPL does and does not reach here

Three separate questions, which are easy to conflate:

**1. Does the wrapper become GPL?** No. `app.py` communicates with SDCC by
`fork`/`exec` with command-line arguments and files on disk. Nothing is linked;
nothing derives from SDCC's source. They are separate programs that happen to
ship together — what the GPL calls mere aggregation (GPLv2 §2, GPLv3 §5). The
wrapper stays MIT.

**2. Does the compiler output become GPL?** No. SDCC's runtime libraries and
headers — including the `mcs51/stc12.h` this service's callers rely on — carry
an explicit linking exception:

> As a special exception, if you link this library with other files, some of
> which are compiled with SDCC, to produce an executable, this library does not
> by itself cause the resulting executable to be covered by the GNU General
> Public License.

So a `.hex` compiled by this service belongs to whoever wrote the C.

**3. Does serving it over HTTP trigger anything?** No. GPLv2 and GPLv3 are
triggered by *distribution*, not by use over a network — that is the AGPL,
which SDCC is not under. Callers receive compiler *output*, never the compiler.

**3b. That answer flips the moment the compiler runs in the browser.** The
plan in the README's "toolchain in the browser" section ships SDCC compiled to
WebAssembly as a static asset, so the visitor receives **the compiler itself**,
not its output. That is distribution, plainly, and the reasoning in (3) stops
applying to it — (1) and (2) are unaffected, since the wrapper still does not
link SDCC and the linking exception still covers the `.hex`.

What compliance requires there, and it is not onerous:

- Publish the corresponding source for that exact build — the upstream tarball
  URL and its SHA-256, not just "SDCC 4.5.0".
- Publish **any patches** applied to make it build under Emscripten. Patches
  are part of the corresponding source; applying them without publishing them
  is the failure mode to avoid.
- Record the toolchain that produced it (the `emcc` version), so the build can
  be reproduced rather than merely inspected.
- Carry the licence text and notices alongside the artifact, as this repository
  already does for the vendored Linux binaries.

None of this restricts what anyone does with the `.hex` they compile: the
runtime-library linking exception in (2) is what governs the output, and it is
unchanged.

What **does** apply is that this repository redistributes GPL binaries, so it
carries the license text, preserves the notices, and identifies the exact
corresponding source. SDCC is unmodified upstream; the source for this precise
build is `apt-get source sdcc=4.0.0+dfsg-2`, and upstream releases are at
<https://sdcc.sourceforge.net/>. Written requests: open an issue on this
repository.

*None of the above is legal advice.*

## The Arduino cores — LGPL-2.1 posture

The `arduino-core/` directory vendors two Arduino cores and three libraries,
each at a pinned commit recorded in `arduino-core/VERSION` and fetched,
checksum-verified, by `scripts/fetch-arduino-core.sh`. The libraries -- Servo
and LiquidCrystal (LGPL-2.1-or-later) and Adafruit_NeoPixel (LGPL-3.0) -- are
held to the same posture as the cores below. The cores:

- [ArduinoCore-avr](https://github.com/arduino/ArduinoCore-avr) 1.8.8
  (© Arduino and contributors, © 2005–2006 David A. Mellis) — `cores/arduino`,
  the `standard`, `eightanaloginputs` and `mega` variants, and the bundled
  EEPROM, SPI, Wire and SoftwareSerial libraries.
- [ATTinyCore](https://github.com/SpenceKonde/ATTinyCore) (© 2015–2022 Spence
  Konde, © 2005–2006 David A. Mellis) — `cores/tiny`, the `tinyx5` and
  `tinyx8` variants, and its EEPROM, SPI, Wire and SoftwareSerial.

Both are licensed under **LGPL-2.1-or-later**. The LGPL's linking obligation
means that anyone who receives a binary linked against LGPL code must be able
to relink it with a modified version of the library. Here:

1. **The core source is public and unmodified.** It is checked into this
   repository exactly as published upstream (the fetch script copies files
   verbatim; the one compatibility shim, `DECIMAL_DIG` for gcc 5.4, is a
   compiler flag, not an edit). Anyone can inspect, modify and rebuild it.
2. **The compiled .hex is produced server-side** from the user's own sketch,
   which the user already has, and that public core source. The core's
   objects are cached on the server between requests but are never returned;
   the response is the linked Intel HEX image only. The flags every build uses
   are in `arduino_build.py`, so the same image can be relinked against a
   modified core with the same toolchain.
3. **No core source is shipped to the browser or to downstream app repos.**
   The core files live only on the server. The returned .hex is the user's
   program linked with the core — the same combined work the Arduino IDE
   produces on a user's own machine.

This is standard Arduino-ecosystem practice: the Arduino IDE itself compiles
LGPL core libraries alongside user sketches and ships the resulting .hex to the
board. This service does the same thing, just over HTTP.

The full LGPL-2.1 license text is at `arduino-core/LICENSE.md`.

## Related

STC MCU Limited publishes no open-source repository — only the datasheet PDF
and the Windows-only, proprietary STC-ISP.exe. Nearly all third-party STC12
code on GitHub carries no license at all. That is precisely why this service
uses SDCC's own properly-licensed `stc12.h` rather than a vendored vendor
header. See [`CrispStrobe/stc12c5a60s2-lab`](https://github.com/CrispStrobe/stc12c5a60s2-lab)
for the hardware side.
