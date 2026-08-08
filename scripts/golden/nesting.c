/* Generated from BrickWright pseudocode by stc-compiler.
 * Hand edits will be lost; change the pseudocode instead. */
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

/* Variables (16-bit signed, like Scratch's integers). */
static int n = 0;

void main(void)
{

    AUXR &= ~0x80;                 /* Timer 0 at FOSC/12 */
    TMOD  = (TMOD & 0xF0) | 0x01;  /* Timer 0, mode 1 */

    n = 0;
    for (;;) {
        { unsigned int _i1;
          for (_i1 = 0; _i1 < (3); _i1++) {
                { unsigned int _i2;
                  for (_i2 = 0; _i2 < (4); _i2++) {
                        if (n > 5) {
                            while (n > 0) {
                                n += -(1);
                            }
                        } else {
                            n += 1;
                        }
                  }
                }
          }
        }
        delay_ms(1);
    }
}
