/*
 * intrins.h — SDCC replacement for Keil C51's intrinsics header.
 *
 * Written from the documented behaviour of each intrinsic, not copied from
 * Keil: these are rotations and a nop, and the semantics are the whole
 * specification. Keil emits single instructions for them; SDCC's peephole
 * optimiser recognises these idioms and does the same for the 8-bit cases.
 *
 * SPDX-License-Identifier: MIT
 */
#ifndef _INTRINS_H_
#define _INTRINS_H_

/* One instruction cycle of delay. */
#define _nop_()  do { __asm nop __endasm; } while (0)

/* Rotate left / right, 8-bit. */
static unsigned char _crol_(unsigned char value, unsigned char n)
{
    n &= 7;
    return (unsigned char)((value << n) | (value >> (8 - n)));
}

static unsigned char _cror_(unsigned char value, unsigned char n)
{
    n &= 7;
    return (unsigned char)((value >> n) | (value << (8 - n)));
}

/* Rotate left / right, 16-bit. */
static unsigned int _irol_(unsigned int value, unsigned char n)
{
    n &= 15;
    return (unsigned int)((value << n) | (value >> (16 - n)));
}

static unsigned int _iror_(unsigned int value, unsigned char n)
{
    n &= 15;
    return (unsigned int)((value >> n) | (value << (16 - n)));
}

/* Rotate left / right, 32-bit. */
static unsigned long _lrol_(unsigned long value, unsigned char n)
{
    n &= 31;
    return (unsigned long)((value << n) | (value >> (32 - n)));
}

static unsigned long _lror_(unsigned long value, unsigned char n)
{
    n &= 31;
    return (unsigned long)((value >> n) | (value << (32 - n)));
}

/* Keil's _testbit_ reads a bit and clears it (the JBC instruction). */
#define _testbit_(b)  ((b) ? ((b) = 0, 1) : 0)

#endif /* _INTRINS_H_ */
