/*
 * Link a blob-free ESP8266 program.
 *
 * Start-up code, the runtime and small programs go to instruction RAM, data
 * to data RAM: those are the segments of the boot image, which the ROM (or
 * this simulator's loader) copies to RAM before jumping to bw_start.
 *
 * Instruction RAM is 32 KB. Larger code runs in place from flash, which the
 * chip maps at 0x40200000 once bw_start has called Cache_Read_Enable: the
 * irom region is flash from offset 0x10000 up to the end of the first
 * megabyte. It takes the code and constants of every object file in a
 * directory named irom-obj (the TCP/IP stack and the programs that use it),
 * and anything placed in an .irom.text section by hand. tools/elf2image.js
 * writes it to the image file at its flash offset.
 *
 * Nothing that runs before flash is mapped, or from an interrupt, may be in
 * irom: runtime.c and net.c are not.
 */
MEMORY
{
  dram : org = 0x3FFE8000, len = 0x14000
  iram : org = 0x40100000, len = 0x8000
  irom : org = 0x40210000, len = 0xF0000
}

ENTRY(bw_start)

SECTIONS
{
  .irom.text :
  {
    *(.irom.literal .irom.text .irom.text.*)
    *irom-obj/*.o(.literal .literal.* .text .text.* .rodata .rodata.*)
  } > irom

  .text :
  {
    *(.entry.text)
    *(.literal .literal.* .text .text.*)
  } > iram

  .data :
  {
    *(.rodata .rodata.* .data .data.*)
  } > dram

  .bss (NOLOAD) :
  {
    . = ALIGN(4);
    _bss_start = .;
    *(.bss .bss.* COMMON)
    . = ALIGN(4);
    _bss_end = .;
  } > dram
}

INCLUDE rom.ld
