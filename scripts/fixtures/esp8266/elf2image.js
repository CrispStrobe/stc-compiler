#!/usr/bin/env node
/**
 * Turn a linked ELF into an ESP8266 flash image file.
 *
 * At offset 0, the boot image (the format the ROM boots), made of the ELF's
 * RAM segments:
 *   header   0xE9, segment count, flash mode, size/frequency, entry (LE32)
 *   segment  load address, length (both LE32), data padded to 4 bytes
 *   padding to a 16-byte boundary, whose last byte is the checksum
 *            (0xEF XOR every data byte)
 *
 * After it, the ELF's flash-mapped segments (code that runs in place from
 * flash, linked at 0x40200000 + flash offset), each at its flash offset, with
 * 0xFF (erased flash) in between. They are not part of the boot image: the
 * program reaches them through the flash mapping. A program without any
 * gives the boot image alone.
 *
 *   node tools/elf2image.js program.elf program.bin
 */
const fs = require('fs');

/** Where the chip maps flash, and how much of it one mapping covers (1 MB). */
const FLASH_MAP = 0x40200000;
const FLASH_MAP_SIZE = 0x100000;

const hex = (n) => `0x${n.toString(16)}`;

function elf2image(elf) {
  if (elf.readUInt32BE(0) !== 0x7f454c46 || elf[4] !== 1 || elf[5] !== 1) {
    throw new Error('not a 32-bit little-endian ELF');
  }
  const entry = elf.readUInt32LE(24);
  const phoff = elf.readUInt32LE(28);
  const phentsize = elf.readUInt16LE(42);
  const phnum = elf.readUInt16LE(44);
  const segments = [];
  const mapped = [];
  for (let i = 0; i < phnum; i++) {
    const at = phoff + i * phentsize;
    const type = elf.readUInt32LE(at);
    const offset = elf.readUInt32LE(at + 4);
    const address = elf.readUInt32LE(at + 12);
    const filesz = elf.readUInt32LE(at + 16);
    if (type !== 1 || filesz === 0) continue;
    const data = Buffer.alloc((filesz + 3) & ~3);
    elf.copy(data, 0, offset, offset + filesz);
    if (address >= FLASH_MAP && address < FLASH_MAP + FLASH_MAP_SIZE) {
      if (address + data.length > FLASH_MAP + FLASH_MAP_SIZE) {
        throw new Error(`the flash-mapped segment at ${hex(address)} runs past the mapped megabyte`);
      }
      mapped.push({ address, offset: address - FLASH_MAP, data });
    } else {
      segments.push({ address, data });
    }
  }
  if (!segments.length || segments.length > 255) throw new Error(`${segments.length} loadable RAM segments`);
  const parts = [Buffer.from([0xe9, segments.length, 0, 0, 0, 0, 0, 0])];
  parts[0].writeUInt32LE(entry, 4);
  let checksum = 0xef;
  for (const { address, data } of segments) {
    const head = Buffer.alloc(8);
    head.writeUInt32LE(address, 0);
    head.writeUInt32LE(data.length, 4);
    parts.push(head, data);
    for (const b of data) checksum ^= b;
  }
  const length = parts.reduce((n, p) => n + p.length, 0);
  const tail = Buffer.alloc(16 - (length % 16));
  tail[tail.length - 1] = checksum;
  parts.push(tail);
  const boot = Buffer.concat(parts);

  // The flash-mapped segments follow, in flash order. Nothing may share a byte of flash.
  mapped.sort((a, b) => a.offset - b.offset);
  let end = boot.length;
  let owner = `the boot image (${boot.length} bytes from flash offset 0)`;
  for (const m of mapped) {
    if (m.offset < end) {
      throw new Error(
        `the flash-mapped segment at ${hex(m.address)} (flash offset ${hex(m.offset)}) overlaps ${owner}, ` +
          `which ends at ${hex(end)}`
      );
    }
    end = m.offset + m.data.length;
    owner = `the flash-mapped segment at ${hex(m.address)}`;
  }
  const image = Buffer.alloc(end, 0xff);
  boot.copy(image, 0);
  for (const m of mapped) m.data.copy(image, m.offset);
  return {
    image,
    entry,
    bootLength: boot.length,
    segments: segments.map((s) => [s.address, s.data.length]),
    mapped: mapped.map((m) => [m.address, m.data.length, m.offset]),
  };
}

module.exports = { elf2image, FLASH_MAP };

if (require.main === module) {
  const [input, output] = process.argv.slice(2);
  if (!input || !output) {
    console.error('usage: elf2image.js <program.elf> <program.bin>');
    process.exit(2);
  }
  let out;
  try {
    out = elf2image(fs.readFileSync(input));
  } catch (e) {
    console.error(`elf2image: ${input}: ${e.message}`);
    process.exit(1);
  }
  fs.writeFileSync(output, out.image);
  const list = (items) => items.map(([a, n]) => `${hex(a)}+${n}`).join(', ');
  console.log(
    `entry ${hex(out.entry)}; RAM segments ${list(out.segments)}; boot image ${out.bootLength} bytes` +
      (out.mapped.length ? `; in flash ${out.mapped.map(([a, n, o]) => `${hex(a)}+${n} at offset ${hex(o)}`).join(', ')}` : '') +
      `; ${out.image.length} bytes`
  );
}
