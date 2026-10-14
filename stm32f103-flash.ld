/* stm32f103-flash.ld — STM32F103 (Blue Pill) flash layout for the BrickWright emulator.
 *
 * Real flash image: word 0 is the initial SP (top of 20K SRAM: 0x20005000),
 * word 1 the reset handler — the Cortex-M3 boot sequence.
 * KEEP(*(.vectors)) pins the generated vector table to the very start of flash.
 *
 * STM32F103C8 (Blue Pill) class: 64K flash at 0x08000000, 20K SRAM at 0x20000000.
 * (.data initialization is absent: generated firmware is static-init-free by construction.)
 */
ENTRY(main)

MEMORY
{
    FLASH (rx)  : ORIGIN = 0x08000000, LENGTH = 64K
    RAM   (rwx) : ORIGIN = 0x20000000, LENGTH = 20K
}

SECTIONS
{
    .text : { KEEP(*(.vectors)) *(.text.startup*) *(.text*) *(.rodata*) } > FLASH
    .bss  : { *(.bss*) *(COMMON) } > RAM
}
