/* stm32g030-flash.ld — STM32G030 flash layout for the BrickWright emulator.
 *
 * Real flash image: word 0 is the initial SP (top of 8K SRAM: 0x20002000),
 * word 1 the reset handler — the Cortex-M0+ boot sequence.
 * KEEP(*(.vectors)) pins the generated vector table to the very start of flash.
 *
 * STM32G030 class: 64K flash at 0x08000000, 8K SRAM at 0x20000000.
 * (.data initialization is absent: generated firmware is static-init-free by construction.)
 */
ENTRY(main)

MEMORY
{
    FLASH (rx)  : ORIGIN = 0x08000000, LENGTH = 64K
    RAM   (rwx) : ORIGIN = 0x20000000, LENGTH = 8K
}

SECTIONS
{
    .text : { KEEP(*(.vectors)) *(.text.startup*) *(.text*) *(.rodata*) } > FLASH
    .bss  : { *(.bss*) *(COMMON) } > RAM
}
