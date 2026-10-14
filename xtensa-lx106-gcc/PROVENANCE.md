# xtensa-lx106-gcc — provenance

A minimal subset of a prebuilt GCC 10.3.0 cross compiler for the ESP8266
(`xtensa-lx106-elf`), used as a **tool** by the `esp8266` target — the same
precedent as `riscv-gcc/`. Nothing in it is modified; files are only omitted,
and `cc1` is stored xz-compressed (`bundle_xz.py`) and decompressed byte for
byte at staging time.

## Source

- Project: [earlephilhower/esp-quick-toolchain](https://github.com/earlephilhower/esp-quick-toolchain)
- Release: `3.1.0-gcc10.3`
- Archive: `x86_64-linux-gnu.xtensa-lx106-elf-e5f9fec.220621.tar.gz`
- URL: <https://github.com/earlephilhower/esp-quick-toolchain/releases/download/3.1.0-gcc10.3/x86_64-linux-gnu.xtensa-lx106-elf-e5f9fec.220621.tar.gz>
- SHA-256: `89d21f38a516e34e0bb44df7e30ef6b1bc2f7f125aae3b7986e5cae545a3ed83`
  (identical to the checksum the Arduino ESP8266 core's package index lists
  for its tool `xtensa-lx106-elf-gcc` `3.1.0-gcc10.3-e5f9fec`, the compiler
  core 3.1.2 installs — and the compiler CrispStrobe/esp8266js's CI builds
  the same runtime with)
- Reproduce: `./scripts/fetch-xtensa-lx106-gcc.sh` (downloads, checks the
  SHA-256, carves this directory).

## What is kept

| file | sha256 |
|---|---|
| `bin/xtensa-lx106-elf-gcc` | `f71204e140ac1a39ed7e213ff2c8d08c3e09f85b8faedca5ce130439551bc692` |
| `libexec/gcc/xtensa-lx106-elf/10.3.0/cc1` (stored as `cc1.xz`) | `43dc5dcb7c04dfccf0786c1f694f2f660d77a8a564403b0f5771f334419743cb` (decompressed) |
| `libexec/gcc/xtensa-lx106-elf/10.3.0/collect2` | `a90c2eb4b01ca1aec01746408d0c69998a589d6992d19b5d1c70f636d896d206` |
| `libexec/gcc/xtensa-lx106-elf/10.3.0/liblto_plugin.so` | `e088832a2788e1e938cd14de671aad747ec90117fb54392859391e49d81e5bbb` |
| `xtensa-lx106-elf/bin/as` | `11acd3867063dc6923fa16fc90b292562b787eae0edb018e967c1e16ea9fa89e` |
| `xtensa-lx106-elf/bin/ld` | `33ef769995a3446ecd165c89b86e8303bd8e0061b93a060236e27590fc29cfb1` |
| `lib/gcc/xtensa-lx106-elf/10.3.0/libgcc.a` | `27ac18239821c333475ce0121f9c9c6290eea63f1dcf59ae8d08dfcdf851c7ae` |
| `lib/gcc/xtensa-lx106-elf/10.3.0/include/`, `include-fixed/` | GCC's own freestanding headers |

`liblto_plugin.so` is kept only because the driver refuses to link without
it (`-fuse-linker-plugin` is its default); no LTO is used. The binaries are
statically linked against GMP/MPFR/MPC, so only glibc is needed (checked by
CI with `scripts/elf-needed.py`).

Omitted: `cc1plus`, `lto1`, gdb, the other binutils, newlib, libstdc++, the
Xtensa HAL library and all headers outside GCC's freestanding set. No
Espressif SDK, Arduino core code, mask ROM or stock firmware image is here.

## Licence

- GCC and GNU binutils: **GPL-3.0-or-later**. Corresponding source: the
  esp-quick-toolchain repository at the release tag above, which builds
  these binaries from upstream GCC 10.3 / binutils with its patches.
- `libgcc.a` and GCC's headers: GPL-3.0-or-later **with the GCC Runtime
  Library Exception 3.1**, which permits linking them into the produced
  programs under any terms.

Used as a tool only: the service runs the compiler; it does not link the
compiler into anything it distributes.
