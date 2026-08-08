/* Generated from BrickWright pseudocode by stc-compiler.
 * Hand edits will be lost; change the pseudocode instead. */
#include <stc12.h>

#define FOSC_HZ 22118400UL

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

/* 10-bit ADC, polled. Channel n is on P1.n; the channel is selected
 * and the conversion started in one write, as STC's examples do. */
static unsigned int adc_read(unsigned char channel)
{
    unsigned char settle;
    ADC_CONTR = (unsigned char)(0xE8 | channel);  /* power|fast|start|chan */
    for (settle = 0; settle < 8; settle++) ;      /* let the mux settle */
    while (!(ADC_CONTR & 0x10)) ;                 /* wait for ADC_FLAG */
    ADC_CONTR &= ~0x10;                           /* clear it by hand */
    return ((unsigned int)ADC_RES << 2) | (ADC_RESL & 0x03);
}

/* Variables (16-bit signed, like Scratch's integers). */
static int v = 0;

void main(void)
{
    P1M1 &= ~0x01;   /* push-pull */
    P1M0 |=  0x01;
    P1_0 = 1;   /* led off */

    P1ASF = 0x08;                 /* analog function on P1 */
    P1M1 |=  0x08;                /* high-impedance input */
    P1M0 &= ~0x08;
    ADC_CONTR = 0xE0;              /* ADC on, fastest conversion */

    AUXR &= ~0x80;                 /* Timer 0 at FOSC/12 */
    TMOD  = (TMOD & 0xF0) | 0x01;  /* Timer 0, mode 1 */

    for (;;) {
        v = adc_read(3);
        if (v < 100) {
            v = 100;
        }
        P1_0 = 0;
        delay_ms(v);
        P1_0 = 1;
        delay_ms(v);
    }
}
