/* Generated from BrickWright pseudocode by stc-compiler.
 * Regenerating overwrites this file. Edits are not stranded, though:
 * BrickWright's C reader imports this back into blocks and names what
 * it cannot represent. stc-compiler itself only goes forwards. */
#include <stc12.h>

/* STC15 supplement -- registers stc12.h lacks (STC15-PERIPHERAL-MODEL.md) */
__sbit __at (0xCC) P5_4;      /* DIP-40 pin 17, RST-shared */
__sbit __at (0xCD) P5_5;      /* DIP-40 pin 19 */
__sbit __at (0xCE) P5_6;      /* not bonded on DIP-40 */
__sbit __at (0xCF) P5_7;      /* not bonded on DIP-40 */
__sfr  __at (0xD6) T2H;       /* Timer 2 -- the UART1 baud source */
__sfr  __at (0xD7) T2L;
__sfr  __at (0xBA) P_SW2;     /* peripheral pin switch 2 */
__sfr  __at (0xAA) WKTCL;     /* wake-up timer */
__sfr  __at (0xAB) WKTCH;
__sfr  __at (0xDC) CCAPM2;    /* third PCA/CCP channel */
__sfr  __at (0xEC) CCAP2L;
__sfr  __at (0xFC) CCAP2H;
__sfr  __at (0xF4) PCA_PWM2;
#define P_SW1    AUXR1        /* STC15 name for 0xA2 */
#define INT_CLKO WAKE_CLKO    /* STC15 name for 0x8F */

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
        delay_ms(adc_read(3));
        P1_0 = !P1_0;
    }
}
