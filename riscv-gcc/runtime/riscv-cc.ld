OUTPUT_ARCH(riscv)
ENTRY(_start)
MEMORY { RAM (rwx) : ORIGIN = 0x1000, LENGTH = 0x3FF000 }
SECTIONS {
  . = 0x1000;
  .text : { *(.text._start) *(.text*) *(.rodata*) *(.srodata*) } > RAM
  . = ALIGN(16);
  __global_pointer$ = . + 0x800;
  .data : { *(.data*) *(.sdata*) } > RAM
  .bss  : { __bss_start = .; *(.bss*) *(.sbss*) *(COMMON); . = ALIGN(4); __bss_end = .; } > RAM
  . = ALIGN(16); __heap_start = .; __heap_end = ORIGIN(RAM) + LENGTH(RAM) - 0x8000;
  __stack_top = ORIGIN(RAM) + LENGTH(RAM);
}
