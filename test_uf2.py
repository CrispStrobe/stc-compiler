"""test_uf2 — verify the UF2 container implementation.

Hand-computed field checks per the Microsoft UF2 spec.
"""
import struct
import unittest

import uf2


class TestUF2Container(unittest.TestCase):

    def test_single_block_structure(self):
        """A 16-byte payload produces one 512-byte UF2 block."""
        data = bytes(range(16))
        container = uf2.binary_to_uf2(data, 0x10000000)
        self.assertEqual(len(container), 512)

    def test_magic_bytes(self):
        """First two words are UF2 magic, last word is final magic."""
        data = b'\x00' * 10
        container = uf2.binary_to_uf2(data, 0x10000000)
        m0, m1 = struct.unpack_from('<II', container, 0)
        self.assertEqual(m0, 0x0A324655, "magic start 0")
        self.assertEqual(m1, 0x9E5D5157, "magic start 1")
        end_magic = struct.unpack_from('<I', container, 508)[0]
        self.assertEqual(end_magic, 0x0AB16F30, "magic end")

    def test_family_id_rp2040(self):
        """Block carries the RP2040 family ID (0xe48bff56)."""
        data = b'\xAA' * 10
        container = uf2.binary_to_uf2(data, 0x10000000)
        # Family ID is at offset 28 (8th word)
        family = struct.unpack_from('<I', container, 28)[0]
        self.assertEqual(family, 0xe48bff56)

    def test_family_id_flag(self):
        """Flags field has the FAMILY_ID bit set (0x2000)."""
        container = uf2.binary_to_uf2(b'\x00' * 10, 0x10000000)
        flags = struct.unpack_from('<I', container, 8)[0]
        self.assertTrue(flags & 0x2000, "FAMILY_ID flag must be set")

    def test_target_address(self):
        """Target address in the block matches the origin."""
        container = uf2.binary_to_uf2(b'\x00' * 10, 0x20000000)
        addr = struct.unpack_from('<I', container, 12)[0]
        self.assertEqual(addr, 0x20000000)

    def test_payload_size_256(self):
        """Payload size field is always 256."""
        container = uf2.binary_to_uf2(b'\x00' * 10, 0x10000000)
        payload_size = struct.unpack_from('<I', container, 16)[0]
        self.assertEqual(payload_size, 256)

    def test_block_count_single(self):
        """Data <= 256 bytes → 1 block."""
        container = uf2.binary_to_uf2(b'\x00' * 100, 0x10000000)
        num_blocks = struct.unpack_from('<I', container, 24)[0]
        self.assertEqual(num_blocks, 1)

    def test_block_count_multiple(self):
        """600 bytes → 3 blocks (ceil(600/256))."""
        data = b'\x55' * 600
        container = uf2.binary_to_uf2(data, 0x10000000)
        self.assertEqual(len(container), 3 * 512)
        num_blocks = struct.unpack_from('<I', container, 24)[0]
        self.assertEqual(num_blocks, 3)

    def test_sequential_addresses(self):
        """Multi-block: target addresses increment by 256."""
        data = b'\x00' * 600
        container = uf2.binary_to_uf2(data, 0x10000000)
        for i in range(3):
            addr = struct.unpack_from('<I', container, i * 512 + 12)[0]
            self.assertEqual(addr, 0x10000000 + i * 256,
                             f"block {i} address")

    def test_block_numbers(self):
        """Block numbers are sequential 0, 1, 2, ..."""
        data = b'\x00' * 600
        container = uf2.binary_to_uf2(data, 0x10000000)
        for i in range(3):
            block_no = struct.unpack_from('<I', container, i * 512 + 20)[0]
            self.assertEqual(block_no, i, f"block {i} number")

    def test_payload_content(self):
        """The payload bytes appear at offset 32 in each block."""
        data = bytes(range(256)) + bytes(range(100))
        container = uf2.binary_to_uf2(data, 0x10000000)
        # First block: full 256 bytes
        self.assertEqual(container[32:32 + 256], bytes(range(256)))
        # Second block: 100 bytes + padding
        self.assertEqual(container[512 + 32:512 + 32 + 100],
                         bytes(range(100)))

    def test_sram_origin(self):
        """SRAM origin 0x20000000 works (the emulator path)."""
        data = b'\x00' * 32
        container = uf2.binary_to_uf2(data, 0x20000000)
        addr = struct.unpack_from('<I', container, 12)[0]
        self.assertEqual(addr, 0x20000000)


if __name__ == "__main__":
    unittest.main()
