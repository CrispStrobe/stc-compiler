/* Generated from BrickWright pseudocode by stc-compiler.
 * Regenerating overwrites this file. Edits are not stranded, though:
 * BrickWright's C reader imports this back into blocks and names what
 * it cannot represent. stc-compiler itself only goes forwards. */
#include <stc12.h>

#define FOSC_HZ 11059200UL

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

static void bw_pulse(int ms);
static void bw_burst(int n, int ms);

/* DEFINE pulse */
static void bw_pulse(int ms)
{
    P1_7 = 0;
    delay_ms(ms);
    P1_7 = 1;
    delay_ms(ms);
}

/* DEFINE burst */
static void bw_burst(int n, int ms)
{
    { unsigned int _i1;
      for (_i1 = 0; _i1 < (n); _i1++) {
            bw_pulse(ms);
      }
    }
}

void main(void)
{
    P1M1 &= ~0x80;   /* push-pull */
    P1M0 |=  0x80;
    P1_7 = 1;   /* led off */

    AUXR &= ~0x80;                 /* Timer 0 at FOSC/12 */
    TMOD  = (TMOD & 0xF0) | 0x01;  /* Timer 0, mode 1 */

    for (;;) {
        bw_burst(3, 80);
        bw_burst(1, 400);
    }
}
