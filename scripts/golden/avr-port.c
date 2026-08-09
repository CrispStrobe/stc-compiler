/* Generated from BrickWright pseudocode by stc-compiler.
 * Hand edits will be lost; change the pseudocode instead. */
#include <avr/io.h>
#include <avr/interrupt.h>

#define F_CPU 16000000UL

/* Timer 0 in CTC mode, one interrupt per millisecond. Nothing
 * here busy-waits on the clock, so a wait costs no accuracy, and
 * the tick is exact rather than near: 16000000 / 64 / 250 = 1000 Hz. */
static volatile unsigned long bw_ms;

ISR(TIMER0_COMPA_vect)
{
    bw_ms++;
}

/* A 32-bit read is four instructions on an 8-bit core; hold the
 * tick off rather than risk tearing across the increment. */
static unsigned long bw_now(void)
{
    unsigned long t;
    unsigned char sreg = SREG;
    cli();
    t = bw_ms;
    SREG = sreg;
    return t;
}

static void delay_ms(unsigned int ms)
{
    unsigned long until = bw_now() + ms;
    while ((long)(bw_now() - until) < 0) ;
}

/* Lookup tables. A plain `const` array on an AVR is copied
 * from flash into RAM at startup -- the Harvard split means
 * `[]` cannot read flash directly. PROGMEM would avoid that,
 * but the index expression is shared with the other targets
 * and would have to become a target hook to say pgm_read_byte.
 * A font costs tens of bytes; a big table is the case that
 * would justify the hook. */
static const unsigned char bw_tab_font[] = { 0x3F, 0x06, 0x5B, 0x4F, 0x66 };

/* A computed index is clamped rather than trusted: reading
 * past a table gives a plausible-looking wrong byte. */
static unsigned char bw_clamp(int i, unsigned char last)
{
    if (i < 0) return 0;
    if (i > (int)last) return last;
    return (unsigned char)i;
}

/* Variables (16-bit signed, like Scratch's integers). */
static int i = 0;

int main(void)
{
    DDRD = 0xFF;               /* segs */
    DDRC = 0x00;
    PORTC = 0xFF;              /* keys pull-ups */

    TCCR0A = _BV(WGM01);           /* CTC */
    OCR0A  = 249;                 /* 1 kHz */
    TIMSK0 = _BV(OCIE0A);
    TCCR0B = _BV(CS01) | _BV(CS00);
    sei();

    i = 0;
    for (;;) {
        PORTD = (unsigned char)(bw_tab_font[bw_clamp(i, 4)]);
        if ((unsigned char)~PINC > 0) {
            i += 1;
        }
        delay_ms(200);
    }

    for (;;) ;
}
