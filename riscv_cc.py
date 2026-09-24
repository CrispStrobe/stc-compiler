"""Compile C to a RISC-V (RV32IM) image by running **shecc** as WebAssembly.

shecc (github.com/sysprog21/shecc, BSD-2-Clause) is a small self-hosting C
compiler whose RV32IM backend emits a Linux ELF32 directly -- no external
assembler or linker. It is built to wasm32-wasi and vendored as
``riscv/riscv-cc.wasm`` (provenance in ``riscv/riscv-cc.PROVENANCE.md``); this
module runs that wasm under wasmtime, so the service needs no native RISC-V
toolchain and the *same* artifact powers the browser page and the bw-board
engine.

The public surface mirrors the other toolchains: ``available()`` says whether
the compiler can run here (so the endpoint can refuse cleanly, and tests skip),
and ``compile_c()`` returns ``(ok, elf_bytes, log)``. ``elf32_to_image()`` turns
the linked ELF into ``{entry, segments}`` -- the form the RISC-V machine loads.
"""

import os
import shutil
import struct
import tempfile

WASM = os.path.join(os.path.dirname(os.path.abspath(__file__)), "riscv", "riscv-cc.wasm")

# A generous instruction budget: shecc compiles the largest sane learner program
# in a few million fuel units; this caps a pathological input without touching a
# real one. Exhaustion is reported as a compile failure, never a hang.
FUEL = 20_000_000_000


class RiscvUnavailable(RuntimeError):
    """The RISC-V compiler cannot run here (wasmtime or the wasm is absent)."""


_engine = None
_module = None


def _load():
    global _engine, _module
    if _module is not None:
        return
    try:
        from wasmtime import Engine, Module, Config
    except ImportError as exc:  # wasmtime not installed
        raise RiscvUnavailable("wasmtime is not installed") from exc
    if not os.path.exists(WASM):
        raise RiscvUnavailable("riscv/riscv-cc.wasm is not vendored in this deployment")
    cfg = Config()
    cfg.consume_fuel = True
    _engine = Engine(cfg)
    _module = Module.from_file(_engine, WASM)


def available() -> bool:
    """True if a compile can actually run here."""
    try:
        _load()
        return True
    except RiscvUnavailable:
        return False


def compile_c(source: str):
    """Compile C source to a RISC-V ELF.

    Returns ``(ok, elf_bytes, log)``: on success ``ok`` is True and ``elf_bytes``
    is the linked RV32 ELF32; on a compile error ``ok`` is False, ``elf_bytes`` is
    None and ``log`` carries shecc's own diagnostics. Raises ``RiscvUnavailable``
    if the compiler cannot run at all.
    """
    _load()
    from wasmtime import Store, Linker, WasiConfig

    work = tempfile.mkdtemp(prefix="riscv-cc-")
    try:
        with open(os.path.join(work, "in.c"), "w", encoding="utf-8") as handle:
            handle.write(source)
        out_log = os.path.join(work, "out.log")
        err_log = os.path.join(work, "err.log")

        linker = Linker(_engine)
        linker.define_wasi()
        store = Store(_engine)
        store.set_fuel(FUEL)

        wasi = WasiConfig()
        wasi.argv = ["shecc", "-o", "/work/out.elf", "/work/in.c"]
        wasi.preopen_dir(work, "/work")
        wasi.stdout_file = out_log
        wasi.stderr_file = err_log
        store.set_wasi(wasi)

        instance = linker.instantiate(store, _module)
        start = instance.exports(store)["_start"]

        exit_code = 0
        try:
            start(store)
        except Exception as exc:  # wasmtime raises on proc_exit and on traps
            # A clean exit(0) surfaces as an ExitTrap with code 0; a nonzero
            # exit or a fuel/trap failure is a compile failure.
            exit_code = getattr(exc, "exit_code", getattr(exc, "code", 1))
            if exit_code is None:
                exit_code = 1

        log = (_read(err_log) + _read(out_log)).strip()
        elf_path = os.path.join(work, "out.elf")
        if exit_code != 0 or not os.path.exists(elf_path) or os.path.getsize(elf_path) == 0:
            return (False, None, log)
        with open(elf_path, "rb") as handle:
            return (True, handle.read(), log)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def elf32_to_image(elf: bytes) -> dict:
    """Parse a little-endian RV32 ELF32 into ``{entry, segments}``.

    Each ``segments`` entry is ``{addr, bytes}`` (a ``bytes`` object, .bss
    zero-filled) at its virtual address. The one exec-ELF reader the service
    has, matching bw-board's ``execElfToImage`` so client and server agree.
    """
    if len(elf) < 52 or elf[:4] != b"\x7fELF":
        raise ValueError("not an ELF")
    if elf[4] != 1:
        raise ValueError("not ELF32")
    machine = struct.unpack_from("<H", elf, 18)[0]
    if machine != 243:
        raise ValueError(f"not a RISC-V ELF (e_machine={machine})")
    entry = struct.unpack_from("<I", elf, 24)[0]
    phoff = struct.unpack_from("<I", elf, 28)[0]
    phentsize = struct.unpack_from("<H", elf, 42)[0]
    phnum = struct.unpack_from("<H", elf, 44)[0]
    segments = []
    for i in range(phnum):
        off = phoff + i * phentsize
        p_type = struct.unpack_from("<I", elf, off)[0]
        if p_type != 1:  # PT_LOAD
            continue
        p_offset = struct.unpack_from("<I", elf, off + 4)[0]
        p_vaddr = struct.unpack_from("<I", elf, off + 8)[0]
        p_filesz = struct.unpack_from("<I", elf, off + 16)[0]
        p_memsz = struct.unpack_from("<I", elf, off + 20)[0]
        data = bytearray(p_memsz)  # memsz >= filesz; tail (.bss) stays zero
        data[:p_filesz] = elf[p_offset:p_offset + p_filesz]
        segments.append({"addr": p_vaddr, "bytes": bytes(data)})
    if not segments:
        raise ValueError("no PT_LOAD segments")
    return {"entry": entry, "segments": segments}


def _read(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return ""
