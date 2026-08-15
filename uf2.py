"""uf2.py — convert a raw binary + base address to a UF2 container.

UF2 (USB Flashing Format) is a Microsoft-specified container for flashing
microcontrollers via USB mass storage.  Spec: https://github.com/microsoft/uf2

This is a clean-room implementation from the public spec (MIT license).
No pico-sdk code is used.

RP2040 family ID: 0xe48bff56 (from the spec's family ID registry).
Block payload: 256 bytes (the spec's standard payload size).
"""

import struct

UF2_MAGIC_START0 = 0x0A324655   # "UF2\n"
UF2_MAGIC_START1 = 0x9E5D5157
UF2_MAGIC_END    = 0x0AB16F30

UF2_FLAG_FAMILY_ID = 0x00002000

RP2040_FAMILY_ID = 0xe48bff56

PAYLOAD_SIZE = 256


def binary_to_uf2(data: bytes, base_address: int,
                  family_id: int = RP2040_FAMILY_ID) -> bytes:
    """Convert a raw binary image to a UF2 container.

    Args:
        data: the raw binary content
        base_address: the target address for the first byte (e.g. 0x10000000
            for RP2040 flash, 0x20000000 for SRAM)
        family_id: the UF2 family ID (default: RP2040)

    Returns:
        The complete UF2 file as bytes (512-byte blocks).
    """
    num_blocks = (len(data) + PAYLOAD_SIZE - 1) // PAYLOAD_SIZE
    if num_blocks == 0:
        num_blocks = 1  # at least one block even for empty data

    blocks = []
    for i in range(num_blocks):
        offset = i * PAYLOAD_SIZE
        chunk = data[offset:offset + PAYLOAD_SIZE]
        # Pad to PAYLOAD_SIZE with zeros
        chunk = chunk.ljust(PAYLOAD_SIZE, b'\x00')

        # 32 bytes header + 476 bytes data area + 4 bytes final magic
        # Header: magic0, magic1, flags, target_addr, payload_size,
        #         block_no, num_blocks, family_id
        header = struct.pack('<IIIIIIII',
                             UF2_MAGIC_START0,
                             UF2_MAGIC_START1,
                             UF2_FLAG_FAMILY_ID,
                             base_address + offset,
                             PAYLOAD_SIZE,
                             i,
                             num_blocks,
                             family_id)
        # Data area: 476 bytes (256 payload + 220 padding)
        data_area = chunk + b'\x00' * (476 - PAYLOAD_SIZE)
        # Final magic
        footer = struct.pack('<I', UF2_MAGIC_END)

        block = header + data_area + footer
        assert len(block) == 512, f"block size {len(block)} != 512"
        blocks.append(block)

    return b''.join(blocks)
