# `wasm/riscv-cc.wasm` — provenance

`wasm/riscv-cc.wasm` is the RISC-V C compiler `src/riscv-cc-wasm.js` runs in the
browser. It is a build of **shecc** — a small self-hosting C compiler whose
RV32IM backend emits a Linux ELF32 directly, no external assembler or linker —
compiled to `wasm32-wasi`.

| | |
|---|---|
| Upstream | https://github.com/sysprog21/shecc |
| Commit | `362b94b948c24cce7f897619de7e189376c6caa1` (2026-09-18) |
| Licence | BSD-2-Clause — full text in `wasm/riscv-cc.LICENSE.txt` (travels with the binary) |
| Built with | wasi-sdk 24.0 (clang 18.1.2, `wasm32-wasi`) |
| Build flags | `-Oz -Wl,--strip-all`, `SOURCE_DATE_EPOCH=1789687869` (byte-reproducible) |
| Target arch | RV32IM (`shecc`'s riscv backend; `make ARCH=riscv`) |
| sha256 | `638c9129044f520f95a66cc25b76e64a59a075fd60a46cc92ba0a9ce3a8221d6` |
| Size | 402 763 bytes |

## Reproducing it

`scripts/build-riscv-cc-wasm.sh` performs the whole build from the pinned commit
and prints the sha256 to compare against the table above. It needs a host
toolchain (`gcc`, `make`) to generate shecc's config + bundled-libc include, and
[wasi-sdk](https://github.com/WebAssembly/wasi-sdk) to cross-compile to wasm:

```
WASI_SDK=/path/to/wasi-sdk-24.0 scripts/build-riscv-cc-wasm.sh
```

The build is **byte-reproducible**: shecc bakes a translation timestamp into the
compiler, so the script pins `SOURCE_DATE_EPOCH` (to the shecc commit's own
date) and two builds produce identical bytes — the sha256 above.

## The one local patch

shecc's `src/elf.c` ends by `chmod`-ing the output ELF executable. WASI has no
`chmod`, and the call fails *after* a valid ELF has already been written — which
would make `_start` exit non-zero over good output. The build script guards that
one call with `#ifndef __wasi__`; nothing else is changed. The consumer
(`riscv-cc-wasm.js`) reads the ELF back as bytes, so the mode bit is irrelevant.

## Why a build artifact, not a source build in CI

Building shecc-to-wasm needs wasi-sdk (~114 MB) and a host compiler, which is
heavy for every CI run. The wasm is therefore committed, and `test/riscv-cc-wasm.test.mjs`
exercises it end to end (C → image → booted on `RiscV32Machine`) on every run.
The sha256 above pins exactly which bytes those tests vouch for; re-running the
build script reproduces them from the pinned commit.
