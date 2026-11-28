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

`cowgol-c`, `cgen`, `cowgol-z80-cpm`, `8080-cpm`, `cowgol-8080-cpm`, and
`i8086-dos` are aliases. Requesting an unrelated machine such as an STC part
returns a target error; it does not feed generic C to an incompatible chip.
The C runtime uses 64-bit pointer slots and libc. The existence of a C output
does not qualify it for every SDCC, AVR or cc65 board.

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
and `log` fields. Cowgol also returns `toolchain`, `target`, `compiler_commit`
and `artifact_kind` (`executable` or `source`). Both CP/M routes return
`origin: 256`, `memory_end` and a memory summary. The assembler's symbol map
must prove that code, the 128-byte transient stack and static workspace fit
below `$D000`; a short image with an oversized uninitialized array is refused.
DOS returns a complete MZ image. Cowgol debug symbols and
disassembly are currently unsupported and explicit requests for them fail.

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

## Build and install

On a Linux x86-64 build host, install the dependencies listed at the top of
`scripts/build-cowgol-bundle.py`, then run:

```sh
python3 scripts/build-cowgol-bundle.py
```

This fetches exactly the pinned commit, builds Cowgol's bootstrapped C
frontend plus the 16-bit frontend, the Z80/8080/8086 backends, the CP/M/DOS
linkers, zmac and DJLINK, and produces `vendor/cowgol`. NASM comes from the
build host's installed package. The manifest records Cowgol's commit, the
host C compiler, NASM version, target list, and a SHA-256 for every deployed
file. DJLINK's C++ support libraries are linked statically so serverless hosts
do not need a system libstdc++; the bundle retains Cowgol, zmac, NASM, DJLINK
and GCC runtime licence notices.

For a reproducible source checkout and machine execution tests:

```sh
python3 scripts/build-cowgol-bundle.py --source /path/to/pinned/cowgol --oracles
REQUIRE_COWGOL_TOOLCHAIN=1 \
COWGOL_CPMEMU=/path/to/pinned/cowgol/bin/cpmemu \
COWGOL_EMU2=/path/to/pinned/cowgol/bin/emu2 \
python3 -m unittest -v test_cowgol
```

The source checkout must be clean and at the requested pin. `--revision`
allows an explicit alternate 40-character commit; `--output` selects a fresh
bundle destination. `COWGOL_BUNDLE` can select that directory when running
the service. The service checks the manifest hashes before staging tools into
an executable temporary directory, which also handles deployments that lose
file execute bits.

The dedicated Cowgol GitHub Actions job builds on Debian Bullseye and uploads
`cowgol-bundle-linux-x64.tar.gz`. It executes the emitted CP/M and DOS programs
in independent machine emulators and compiles/runs the C output. The emulators
are test tools outside the deployed bundle. CI with
`REQUIRE_COWGOL_TOOLCHAIN=1` fails if any compiler or execution oracle is absent.

The Dockerfile builds the bundle in a separate Bullseye stage and copies it
into the final Python image. The HF staging script carries the one builder
script needed for that stage. For the existing manual Vercel release, unpack
the verified Linux bundle beneath `vendor/` before using the release script;
`.vercelignore` keeps the generated bundle in the upload even though Git
ignores it. Prefer the Bullseye artifact to binaries built on a newer libc or
another OS. Deployments remain manual; pushing a feature branch does not
release this service.

## Hosted source boundary

The lexer-aware guard allows only `cowgol.coh`, `strings.coh`, `malloc.coh`,
`file.coh`, `argv.coh` and `brickwright.coh`. It rejects path traversal,
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
