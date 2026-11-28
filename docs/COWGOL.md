# Cowgol compiler service

The `cowgol` language route runs the real compiler from
[CrispStrobe/cowgol](https://github.com/CrispStrobe/cowgol), with a source commit
pinned in `scripts/build-cowgol-bundle.py`. It accepts full Cowgol language
syntax, subject to the hosted include/assembler boundary below. It never runs
user programs on the server.

## Targets and artifacts

| Request target | Required format | Output | Runtime |
| --- | --- | --- | --- |
| `z80-cpm` | `bin` | `main.com`, origin `256` | Z80 CP/M; existing Brickwright CP/M machine loader |
| `i8080-cpm` | `bin` | `main.com`, origin `256` | Intel 8080 CP/M; also runs on Z80 CP/M |
| `dos` | `bin` | `main.exe` | 8086 DOS small-model MZ executable |
| `cowgol-cgen` | `c` | `main.c` | Standalone generated C for a little-endian host |
| `riscv32` | `elf` | `main.elf` plus loadable `image` | Cowgol C backend → pinned GCC/picolibc → Brickwright RV32IMAC console machine |
| `eater6502` | `bin` | `main.bin`, exactly 32768 bytes | Native 65C02 ROM for the canonical Brickwright EATER6502 map; ACIA console and VIA digital GPIO |
| `atmega328p` | `hex` | `main.hex`, validated Intel HEX | Cowgol C backend → AVR GCC/avr-libc → 16 MHz Uno, 9600-baud UART, digital GPIO and blocking delays |

`cowgol-c`, `cgen`, `cowgol-z80-cpm`, `8080-cpm`, `cowgol-8080-cpm`, and
`i8086-dos` are aliases; `arduino-uno` selects `atmega328p`. Requesting an unrelated machine such as an STC part
returns a target error; it does not feed generic C to an incompatible chip.
The C runtime uses 64-bit pointer slots and libc. The RV32 route supplies a
tested 32-bit runtime profile and the ATmega328P route has its own bounded
16-bit-pointer binding. C output alone does not qualify other SDCC, AVR or
cc65 boards.

Example request:

```json
{
  "language": "cowgol",
  "target": "z80-cpm",
  "format": "bin",
  "code": "include \"cowgol.coh\";\nprint(\"Hello from Cowgol!\\n\");\n"
}
```

`POST /compile` returns the familiar `success`, `base64`, `filename`, `bytes`
and `log` fields. Cowgol also returns `language`, `toolchain`, `target`, `format`, `compiler_commit`
and `artifact_kind` (`executable` or `source`). Both CP/M routes return
`origin: 256`, `memory_end` and a memory summary. The assembler's symbol map
must prove that code, the 128-byte transient stack and static workspace fit
below `$D000`; a short image with an oversized uninitialized array is refused.
DOS returns a complete MZ image. Cowgol debug symbols and
disassembly are currently unsupported and explicit requests for them fail.

The RISC-V route compiles the original Cowgol program through cgen and the
service's existing `riscv-gcc` bundle. It returns `toolchain: "cowgol-riscv32"`,
`artifact_kind: "executable"`, `mcu: "rv32imac"`, the ELF in `base64`, and the
same `{entry, segments: [{addr, bytes}]}` image used by Brickwright's RISC-V
loader (`bytes` is base64). It is an executable compiled via C, not a new
Cowgol assembly backend or a physical RISC-V board port.

Every ELF load segment is checked before allocation: ELF32 little-endian,
RISC-V executable type, bounded program headers and file ranges, no overlap,
valid alignment, and an even entry inside file-backed executable code.
Code and static workspace, including BSS, must fit `$001000–$3F7FFF` in the
four-MiB machine. The top 32 KiB is reserved for the runtime stack. Returned
segments include zero-filled BSS, and `memory_end` reports the highest loaded
address plus one. Runtime pointer operations are still ordinary Cowgol;
this static bound is not a dynamic memory-safety guarantee.

Cowgol's current object/layout format has a separate 16-bit size limit.
Oversized array extents, array byte sizes and accumulated static workspaces
are rejected before narrowing, including when generating C; a larger guest
RAM does not remove that compiler-format limit.

The RV32 embedded console profile disables POSIX and process arguments,
uses a 64 KiB Cowgol arena, and retains the compiler's signed-wrap and
aliasing flags. `argv.coh` observes an empty argument list; `malloc.coh`
uses the target's picolibc allocator. `file.coh` is rejected because this
console machine has no filesystem. Input follows the existing RV32 runtime
and returns EOF. GPIO, timers and canvas callbacks remain unresolved linker
errors. The service never executes the produced ELF.

The EATER6502 route uses the real `65c02-rom` Cowgol backend, a board-specific
linker/runtime, and 64tass. It returns `toolchain: "cowgol-eater6502"`,
`mcu: "w65c02"`, `format: "bin"`, and `origin: 32768` / `entry: 32768`.
This is the canonical 1 MHz configuration: RAM `$0000–$3FFF`, ROM
`$8000–$FFFF`, W65C51 ACIA at `$5000`, and W65C22 VIA at `$6000`.
Other 6502 memory maps need their own runtime and are not inferred from the CPU.

This backend keeps writable return addresses and initialized arrays in RAM;
startup copies their initial bytes out of ROM on every reset. The service
checks the assembler's memory labels, the exact ROM length, reset/NMI/IRQ
vectors, the STP exit opcode and the RTI interrupt handler. Static workspace and relocated data must
end at or before `$3FF8` (eight bytes remain for allocator bookkeeping),
zero-page storage must fit 256 bytes, and ROM code plus initialization data
must end by `$FFFA`, before hardware vectors. A linker/assembler failure or
inconsistent layout returns no image.

The returned `memory_layout` contains integer values named `lomem`,
`eater_workspace_end`, `eater_zp_end`, `eater_code_end`, `eater_data_load`,
`eater_data_size`, `eater_reset`, `eater_irq`, and `eater_halt`.
`memory_end` equals `lomem`. The service verifies
`eater_data_size = lomem - eater_workspace_end` and
`eater_data_load + eater_data_size = eater_code_end`.
These checks cover static layout; ordinary Cowgol pointers and dynamic
allocation still require a valid program. Hardware execution is not claimed.

The ROM backend limits each subroutine's complete input argument list to
128 bytes and its complete output/result list to 128 bytes independently.
Larger lists are refused before assembly. The 256-byte 65C02 hardware stack
also remains finite; these checks do not prove arbitrary call depth safe.

The ACIA uses 19200 baud, 8N1, with paced output at the fixed 1 MHz clock;
`get_char` waits for a received byte. `argv.coh` returns no process arguments,
and `file.coh` is unavailable. `bw_pin_mode`, `bw_digital_write`,
`bw_digital_read` and `bw_digital_toggle` operate the real VIA. Pins `0–7`
map to `PA0–PA7`; `8–15` map to `PB0–PB7`. Modes are `0` input and `1`
output. Toggle changes the stored output latch even when an input reads low.
Invalid coordinates or modes print a diagnostic and halt with failure status.
Timers, pullups, analog/PWM and canvas calls have no native binding.

### ATmega328P / Uno

The `atmega328p` route uses cgen and the service's existing AVR GCC/avr-libc
bundle. It returns `toolchain: "cowgol-atmega328p"`, `format: "hex"`,
`mcu: "atmega328p"`, `origin: 0`, `entry: 0`, `f_cpu: 16000000` and
`uart_baud: 9600`. `base64` encodes the exact ASCII Intel HEX download, and
`bytes` counts those ASCII bytes. The fixed runtime initializes USART0 for
9600 baud, 8N1. `get_char` waits for input and exit drains the final output byte.

The linker limits flash to `$0000–$7DFF` (32256 bytes), retaining the Uno's
512-byte bootloader area. SRAM spans `$0100–$08FF`. Static data, generated
workspace and a fixed **512-byte heap** must all fit `$0100–$06FF`; the upper
512 bytes are reserved for the C stack. The allocator is bound to the static
arena, rather than borrowing the space below the current SP. Its metadata
also occupies the arena. Allocation lengths are checked before narrowing into
AVR's 16-bit `size_t`, so a large request cannot silently become a small one.

The service requires an AVR executable ELF with reset entry zero. Real linked
symbols prove the initialized-data extent, BSS end, heap object and stack
reservation. The HEX must contain one contiguous flash image, valid record
lengths/checksums, data records and a single EOF. Sparse/overlapping data,
other record types and reset jumps outside executable flash are refused.

`memory_layout` contains `flash_end`, `flash_limit` (32256), `data_start`
(256), `data_end`, `bss_end`, `heap_start`, `heap_end`, `stack_start` (1792),
`stack_reserve` (512), and `ram_end` (2304), all in bytes. `memory_end` equals
`bss_end`; `heap_end - heap_start` is 512. `halted_addr` and `status_addr`
identify the actual guest-written bytes used by the execution oracle.
The initialized data's flash copy follows code, so executable flash ends at
`flash_end - (data_end - data_start)`.

GPIO uses Uno coordinates: D2–D13 are pins `2..13`, and A0–A5 are pins
`14..19`. Pins `0/1` are reserved for UART and fail explicitly if used as
GPIO. Modes `0` input, `1` output and `2` input with pull-up are supported.
Read observes `PIN`; write and toggle change `PORT`, the output latch.
Invalid modes and pin numbers print a diagnostic and stop with failure status.

`bw_delay(milliseconds)` accepts a signed int32 count. Each positive millisecond
executes 16,000 CPU cycles plus a small loop overhead; zero returns without
waiting. Negative counts print a diagnostic and stop with failure status.
This synchronous delay keeps interrupts disabled and supplies a minimum wait
at the fixed 16 MHz clock. It does not supply a scheduler or a `bw_millis` clock.
Compiler-service tests measure the actual GPIO edges around 0, 1 and 7 ms waits
after repeated cold starts, as well as refusal of negative delays.

Arrays, records, pointers, nested calls, integer division, allocation/free and
empty arguments are available through the full compiler. Filesystem includes
are refused before compilation. Clock queries, analog/PWM and graphics callbacks
remain unresolved linker errors. This port is fixed to the declared chip and
clock; no other AVR board or Arduino core is inferred. Tests execute from
nonzero SRAM and measure the fixture's stack use; arbitrary pointer writes
and call depth, and physical-board behavior, remain outside that proof.

For C output, the response includes both the base64 artifact and `c` text,
with `source_language: "c"`. Its runtime and Brickwright host declarations
are included inline, along with Cowgol's complete BSD licence notice, so the
download needs no private compiler include path. `POST /transpile` with
`language: "cowgol"` selects this C export automatically and uses the same
HTTP compilation limits as `/compile`. `POST /download` returns the artifact
directly, with its filename.

`GET /health` includes a `cowgol` object containing installed targets and
the pinned compiler commit. An absent bundle is reported as unavailable;
compile requests fail with an installation instruction.

## Pseudocode, canvas and circuits

The Brickwright/sb3-creator frontend supplies a separate, checked Cowgol ↔
pseudocode subset. That path can use the application's canvas/circuit host
and supported pseudocode machine compilers. This hosted service is the native
Cowgol compiler path; it does not silently substitute the pseudocode subset.

`include "brickwright.coh";` is accepted by each target:

- The C target declares the real canvas, turtle, clock and pin host callbacks.
  Applications provide those callbacks when linking the exported source;
  absent callbacks remain linker errors.
- CP/M and DOS implement the two portable console calls,
  `bw_print_text([uint8])` and `bw_print_number(int32)`, each followed by a
  newline. Signed integer limits are tested on each emulator. Canvas, GPIO,
  clock and delay APIs are not invented for these operating-system targets;
  asking for them produces a compiler diagnostic.
- RV32 implements the same two portable console calls through its actual
  ECALL console. Arrays, pointers, allocation and integer division compile
  through the full Cowgol compiler even when the Blocks converter refuses
  that source. CI executes those constructs on Brickwright's machine.
- EATER6502 implements console and digital GPIO against its actual ACIA/VIA,
  including output-latch toggling. Its native ROM tests also cover mutable
  initialized data, nested calls and reset behavior while auditing ROM writes.
- ATmega328P implements serial console/input, digital GPIO and input pull-ups.
  Tests execute the returned HEX through avr8js, including cold startup,
  allocation bounds, signed limits and the reserved serial pins.

## Build and install

On a Linux x86-64 build host, install the dependencies listed at the top of
`scripts/build-cowgol-bundle.py`, then run:

```sh
python3 scripts/build-cowgol-bundle.py
```

This fetches exactly the pinned commit, builds Cowgol's bootstrapped C
frontend plus the 16-bit and 6502 frontends, the Z80/8080/8086/65C02 ROM
backends, target linkers, zmac and DJLINK, and produces `vendor/cowgol`.
NASM and 64tass come from the build host's installed packages. The manifest
records Cowgol's commit, the host C compiler, both assembler versions,
target list, and a SHA-256 for every deployed
file. DJLINK's C++ support libraries are linked statically so serverless hosts
do not need a system libstdc++; the bundle retains Cowgol, zmac, NASM, DJLINK
and GCC runtime licence notices, plus 64tass's copyright and GPL/LGPL texts.
`TASS64_LICENSE` and `TASS64_LICENSE_DIR` select those notices on build hosts
where the Debian paths are unavailable.

To build only the C export and RISC-V compiler stages, without the legacy
assemblers or full compiler build dependencies:

```sh
python3 scripts/build-cowgol-bundle.py --targets cowgol-cgen riscv32
```

That subset requires Python, a host C compiler, and Git if a checkout is not
supplied. The manifest advertises only selected targets. RV32 also requires
the already-vendored GCC, picolibc and console runtime; health omits RV32 if
those are absent. The default builds all six targets.

For a reproducible source checkout and machine execution tests:

```sh
python3 scripts/build-cowgol-bundle.py --source /path/to/pinned/cowgol --oracles
REQUIRE_COWGOL_TOOLCHAIN=1 \
COWGOL_CPMEMU=/path/to/pinned/cowgol/bin/cpmemu \
COWGOL_EMU2=/path/to/pinned/cowgol/bin/emu2 \
COWGOL_BW_BOARD=/path/to/pinned/bw-board \
python3 -m unittest -v test_cowgol
```

The source checkout must be clean and at the requested pin. `--revision`
allows an explicit alternate 40-character commit; `--output` selects a fresh
bundle destination. `COWGOL_BUNDLE` can select that directory when running
the service. The service checks the manifest hashes before staging tools into
an executable temporary directory, which also handles deployments that lose
file execute bits.

The machine oracle uses `CrispStrobe/bw-board` at
`d21d6d8b819263eb08231134b7556efa82289a90`, the current Brickwright pin,
and Node 22. The RV32 and EATER6502 runners need no npm packages and are outside the deployed
bundle. The tests check the checkout's exact commit before running programs.

The dedicated Cowgol GitHub Actions job builds on Debian Bullseye and uploads
`cowgol-bundle-linux-x64.tar.gz`. It executes the emitted CP/M and DOS programs
in independent machine emulators, boots RV32 and EATER6502 output on Brickwright's machines,
and compiles/runs the C output. The emulators
are test tools outside the deployed bundle. CI with
`REQUIRE_COWGOL_TOOLCHAIN=1` fails if any compiler or execution oracle is absent.

The build-only apt sources use the signed Debian snapshot from 2025-01-01
in `scripts/cowgol-bullseye.list`. This preserves the older glibc ABI without
depending on disappearing packages in a live Bullseye mirror. Package signatures
and hashes remain checked; the expiry check for historical Release metadata is
disabled. The final service image keeps its normal package sources.

The Dockerfile builds the bundle in a separate Bullseye stage and copies it
into the final Python image. The HF staging script carries the builder and
apt archive list needed for that stage. For the existing manual Vercel release, unpack
the verified Linux bundle beneath `vendor/` before using the release script;
`.vercelignore` keeps the generated bundle in the upload even though Git
ignores it. Prefer the Bullseye artifact to binaries built on a newer libc or
another OS. Deployments remain manual; pushing a feature branch does not
release this service.

## Hosted source boundary

The lexer-aware guard allows only `cowgol.coh`, `strings.coh`, `malloc.coh`,
`file.coh`, `argv.coh` and `brickwright.coh`; RV32 and EATER6502 omit
`file.coh`. It rejects path traversal,
absolute paths, escaped include names, inline assembly and external symbol
directives before invoking tools. Compiler options and defines are not
accepted for this route. This keeps compiler/assembler file reads inside the
known runtime rather than exposing server files through diagnostics. Local
Cowgol compilation remains available for programs that need custom headers
or assembly.

Compilation uses argument vectors, private request directories, a total
25-second timeout, bounded log reads and target-appropriate output size checks.
It emits no image on syntax/toolchain failure. Errors retain source filenames
and line diagnostics while removing private temporary directory prefixes.
