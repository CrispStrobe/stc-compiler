/* Generated from BrickWright pseudocode by stc-compiler.
 * Hand edits will be lost; change the pseudocode instead. */
#include <stc12.h>

#define FOSC_HZ 11059200UL

/* Timer 0, mode 1, clocked at FOSC/12 -- accuracy depends only on
 * FOSC, and every supported family counts this mode identically, so
 * the same program is timing-correct on a 12T STC89 and a 1T STC12
 * or STC15. Nothing in the generated code ever busy-waits. */
#define T0_RELOAD (65536UL - (FOSC_HZ / 12UL / 1000UL))

/* One WHEN block = one cooperative task. Timer 0 interrupts
 * every millisecond; tasks yield at every wait and at every
 * loop iteration (Scratch's own scheduling contract), so no
 * task can starve the others. */
static volatile unsigned int bw_ms;

void bw_tick(void) __interrupt(1)
{
    TL0 = (unsigned char)(T0_RELOAD & 0xFF);
    TH0 = (unsigned char)(T0_RELOAD >> 8);
    bw_ms++;
}

/* A 16-bit read is not atomic on an 8051; hold the tick off. */
static unsigned int bw_now(void)
{
    unsigned int t;
    ET0 = 0;
    t = bw_ms;
    ET0 = 1;
    return t;
}

/* REPEAT counters live across yields. */
static unsigned int bw_i1;

static unsigned int bw_task0_state;
static unsigned int bw_task0_until;
/* WHEN started: (script 1) */
static void bw_task0(void)
{
    switch (bw_task0_state) {
    case 0:
    bw_task0_state = 1;
    case 1:
    P1_0 = !P1_0;
    bw_task0_until = bw_now() + (300);
    bw_task0_state = 2;
    case 2:
    if ((int)(bw_now() - bw_task0_until) < 0) return;
    bw_task0_state = 1;
    return;
    }
    bw_task0_state = 0xFFFF;   /* ran to the end */
}

static unsigned int bw_task1_state;
static unsigned int bw_task1_until;
/* WHEN started: (script 2) */
static void bw_task1(void)
{
    switch (bw_task1_state) {
    case 0:
    bw_task1_state = 1;
    case 1:
    bw_task1_state = 2;
    case 2:
    if (!(!P3_2)) return;
    P1_1 = !P1_1;
    bw_i1 = (4);
    bw_task1_state = 3;
    case 3:
    if (bw_i1) {
        bw_task1_until = bw_now() + (50);
        bw_task1_state = 4;
        case 4:
        if ((int)(bw_now() - bw_task1_until) < 0) return;
        bw_i1--;
        bw_task1_state = 3;
        return;
    }
    bw_task1_state = 5;
    case 5:
    if (!(!(!P3_2))) return;
    bw_task1_state = 1;
    return;
    }
    bw_task1_state = 0xFFFF;   /* ran to the end */
}

void main(void)
{
    P1M1 &= ~0x03;   /* push-pull */
    P1M0 |=  0x03;
    P1_0 = 1;   /* led1 off */
    P1_1 = 1;   /* led2 off */

    AUXR &= ~0x80;                 /* Timer 0 at FOSC/12 */
    TMOD  = (TMOD & 0xF0) | 0x01;  /* Timer 0, mode 1 */
    TL0 = (unsigned char)(T0_RELOAD & 0xFF);
    TH0 = (unsigned char)(T0_RELOAD >> 8);
    ET0 = 1;                       /* millisecond tick */
    EA  = 1;
    TR0 = 1;

    for (;;) {
        bw_task0();
        bw_task1();
    }
}
