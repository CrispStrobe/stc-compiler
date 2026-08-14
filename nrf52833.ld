/* nrf52833.ld — bare-metal linker script for nRF52833 (micro:bit V2).
 *
 * Memory map from the Nordic nRF52833 Product Specification:
 *   Flash: 512 KB at 0x00000000
 *   RAM:   128 KB at 0x20000000
 *
 * Vector table at 0x00000000 (Cortex-M4 convention).
 * No SoftDevice, no CODAL — bare-metal assembly only.
 * Output: Intel HEX for DAPLink MSD drag-flash.
 */
ENTRY(Reset_Handler)

MEMORY
{
    FLASH (rx)  : ORIGIN = 0x00000000, LENGTH = 512K
    RAM   (rwx) : ORIGIN = 0x20000000, LENGTH = 128K
}

SECTIONS
{
    .text : {
        KEEP(*(.vectors))
        *(.text.startup*)
        *(.text*)
        *(.rodata*)
    } > FLASH

    .data : {
        *(.data*)
    } > RAM AT > FLASH

    .bss : {
        *(.bss*)
        *(COMMON)
    } > RAM

    _stack_top = ORIGIN(RAM) + LENGTH(RAM);
}
