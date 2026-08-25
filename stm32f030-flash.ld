/* stm32f030-flash.ld — STM32F030 flash layout for the BrickWright emulator.
 *
 * Unlike the pico's SRAM-only trick, the F0 image is a REAL flash image:
 * word 0 is the initial SP, word 1 the reset handler — the M0 boot the
 * emulator (bw-board CortexM0Machine.loadFirmware) and the silicon both
 * perform. KEEP(*(.vectors)) pins the generated bw_vectors table to the
 * very start of flash; without it -Os garbage-collects or reorders it and
 * the chip boots into whatever landed first.
 *
 * F030F4/K6 class: 16K flash at 0x08000000, 4K SRAM at 0x20000000.
 * (.data initialization is deliberately absent: the generated firmware is
 * static-init-free by construction, the same rule the pico build keeps.)
 */
ENTRY(main)

MEMORY
{
    FLASH (rx)  : ORIGIN = 0x08000000, LENGTH = 16K
    RAM   (rwx) : ORIGIN = 0x20000000, LENGTH = 4K
}

SECTIONS
{
    .text : { KEEP(*(.vectors)) *(.text.startup*) *(.text*) *(.rodata*) } > FLASH
    .bss  : { *(.bss*) *(COMMON) } > RAM
}
