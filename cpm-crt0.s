; SPDX-License-Identifier: BSD-3-Clause
; CP/M loads this program at 0100h and supplies the stack.
.module bw_cpm_crt0
.globl _main
.globl s__DATA, l__DATA
.globl s__INITIALIZER, s__INITIALIZED, l__INITIALIZER

.area _CODE
    ld bc,#l__DATA
    ld a,b
    or c
    jr z,copy_data
    ld hl,#s__DATA
    ld (hl),#0
    dec bc
    ld a,b
    or c
    jr z,copy_data
    ld d,h
    ld e,l
    inc de
    ldir
copy_data:
    ld bc,#l__INITIALIZER
    ld a,b
    or c
    jr z,run_main
    ld hl,#s__INITIALIZER
    ld de,#s__INITIALIZED
    ldir
run_main:
    call _main
    jp 0x0000
.area _HOME
.area _INITIALIZER
.area _GSINIT
.area _GSFINAL
.area _DATA
.area _INITIALIZED
.area _BSEG
.area _BSS
.area _HEAP
