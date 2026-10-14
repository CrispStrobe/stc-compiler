"""esp8266_image — turn a linked ELF into an ESP8266 flash image file.

A line-for-line Python port of tools/elf2image.js from CrispStrobe/esp8266js
(MIT; the commit is recorded in esp8266/PROVENANCE.md). Its output must be
byte-identical to the JavaScript's; test_esp8266_build.py checks that against
the real elf2image.js whenever node is available, and CI makes it available.

At offset 0, the boot image (the format the ROM boots), made of the ELF's RAM
segments:
  header   0xE9, segment count, flash mode, size/frequency, entry (LE32)
  segment  load address, length (both LE32), data padded to 4 bytes
  padding to a 16-byte boundary, whose last byte is the checksum
           (0xEF XOR every data byte)

After it, the ELF's flash-mapped segments (code that runs in place from flash,
linked at 0x40200000 + flash offset), each at its flash offset, with 0xFF
(erased flash) in between.
"""

from __future__ import annotations

import struct
import sys

# Where the chip maps flash, and how much of it one mapping covers (1 MB).
FLASH_MAP = 0x40200000
FLASH_MAP_SIZE = 0x100000


def elf2image(elf: bytes) -> dict:
    """Return {image, entry, boot_length, segments, mapped}; ValueError on a
    malformed or unplaceable ELF (the messages match elf2image.js)."""
    if len(elf) < 52 or elf[:4] != b"\x7fELF" or elf[4] != 1 or elf[5] != 1:
        raise ValueError("not a 32-bit little-endian ELF")
    entry, phoff = struct.unpack_from("<II", elf, 24)
    phentsize, phnum = struct.unpack_from("<HH", elf, 42)
    segments: list[tuple[int, bytes]] = []
    mapped: list[tuple[int, int, bytes]] = []
    for i in range(phnum):
        at = phoff + i * phentsize
        ptype, offset, _vaddr, address, filesz = struct.unpack_from("<IIIII", elf, at)
        if ptype != 1 or filesz == 0:
            continue
        raw = elf[offset:offset + filesz]
        data = raw + b"\x00" * (((filesz + 3) & ~3) - len(raw))
        if FLASH_MAP <= address < FLASH_MAP + FLASH_MAP_SIZE:
            if address + len(data) > FLASH_MAP + FLASH_MAP_SIZE:
                raise ValueError(f"the flash-mapped segment at {address:#x} "
                                 "runs past the mapped megabyte")
            mapped.append((address, address - FLASH_MAP, data))
        else:
            segments.append((address, data))
    if not segments or len(segments) > 255:
        raise ValueError(f"{len(segments)} loadable RAM segments")

    boot = bytearray([0xE9, len(segments), 0, 0]) + struct.pack("<I", entry)
    checksum = 0xEF
    for address, data in segments:
        boot += struct.pack("<II", address, len(data)) + data
        for b in data:
            checksum ^= b
    # Always at least one byte of padding (a full 16 when already aligned),
    # exactly as the JavaScript does.
    tail = bytearray(16 - (len(boot) % 16))
    tail[-1] = checksum
    boot += tail

    mapped.sort(key=lambda m: m[1])
    end = len(boot)
    owner = f"the boot image ({len(boot)} bytes from flash offset 0)"
    for address, off, data in mapped:
        if off < end:
            raise ValueError(f"the flash-mapped segment at {address:#x} (flash offset "
                             f"{off:#x}) overlaps {owner}, which ends at {end:#x}")
        end = off + len(data)
        owner = f"the flash-mapped segment at {address:#x}"
    image = bytearray(b"\xff" * end)
    image[:len(boot)] = boot
    for _address, off, data in mapped:
        image[off:off + len(data)] = data
    return {
        "image": bytes(image),
        "entry": entry,
        "boot_length": len(boot),
        "segments": [(a, len(d)) for a, d in segments],
        "mapped": [(a, len(d), o) for a, o, d in mapped],
    }


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit("usage: esp8266_image.py <program.elf> <program.bin>")
    with open(sys.argv[1], "rb") as fh:
        out = elf2image(fh.read())
    with open(sys.argv[2], "wb") as fh:
        fh.write(out["image"])
