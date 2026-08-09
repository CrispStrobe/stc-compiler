/* Generated from BrickWright pseudocode by stc-compiler.
 * Hand edits will be lost; change the pseudocode instead. */
#include <stc12.h>

#define FOSC_HZ 12000000UL

/* Timer 0, mode 1, clocked at FOSC/12 -- accuracy depends only on
 * FOSC, and every supported family counts this mode identically, so
 * the same program is timing-correct on a 12T STC89 and a 1T STC12
 * or STC15. Nothing in the generated code ever busy-waits. */
#define T0_RELOAD (65536UL - (FOSC_HZ / 12UL / 1000UL))

static void delay_ms(unsigned int ms)
{
    while (ms--) {
        TL0 = (unsigned char)(T0_RELOAD & 0xFF);
        TH0 = (unsigned char)(T0_RELOAD >> 8);
        TF0 = 0;
        TR0 = 1;
        while (!TF0) ;
        TR0 = 0;
        TF0 = 0;
    }
}

void main(void)
{
    P1M1 &= ~0x01;   /* push-pull */
    P1M0 |=  0x01;
    P2M1 &= ~0x80;   /* push-pull */
    P2M0 |=  0x80;
    P1_0 = 0;   /* a off */
    P2_7 = 1;   /* b off */

    AUXR &= ~0x80;                 /* Timer 0 at FOSC/12 */
    TMOD  = (TMOD & 0xF0) | 0x01;  /* Timer 0, mode 1 */

    for (;;) {
        P1_0 = 1;
        P2_7 = 0;
        P1_0 = !P1_0;
        if (P3_3) {
            P1_0 = 0;
        } else {
            P2_7 = 1;
        }
        while (!(!P3_4)) { }
    }
}
