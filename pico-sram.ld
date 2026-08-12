/* pico-sram.ld — RP2040 SRAM-only linker script for the BrickWright emulator.
 *
 * The emulator (rp2040js) loads the binary directly into SRAM at 0x20000000
 * and starts execution there. No bootrom, no flash, no second stage.
 *
 * .text.startup* MUST come first: under -Os, gcc places main in
 * .text.startup.main and other functions in plain .text.*. The emulator
 * jumps to 0x20000000, so main must be the first function in the image.
 */
ENTRY(main)

MEMORY
{
    RAM (rwx) : ORIGIN = 0x20000000, LENGTH = 256K
}

SECTIONS
{
    .text : { *(.text.startup*) *(.text.main) *(.text*) *(.rodata*) } > RAM
    .data : { *(.data*) } > RAM
    .bss  : { *(.bss*) *(COMMON) } > RAM
}
