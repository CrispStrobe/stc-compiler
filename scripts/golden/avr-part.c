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
static const unsigned char bw_tab_font[] = { 0x3F, 0x06, 0x5B };

/* A computed index is clamped rather than trusted: reading
 * past a table gives a plausible-looking wrong byte. */
static unsigned char bw_clamp(int i, unsigned char last)
{
    if (i < 0) return 0;
    if (i > (int)last) return last;
    return (unsigned char)i;
}

/* 74HC595: eight outputs for three pins. Data is
 * sampled on the rising edge of the shift clock, and the latch
 * transfers on its own rising edge.
 *
 * MSB first, so the byte reads left to right on the outputs. */
static void bw_part_sr(unsigned char value)
{
    unsigned char i;
    PORTD &= (unsigned char)~_BV(PD7);
    PORTB &= (unsigned char)~_BV(PB0);
    for (i = 0; i < 8; i++) {
        if (value & 0x80) { PORTD |= _BV(PD4); }
        else { PORTD &= (unsigned char)~_BV(PD4); }
        value = (unsigned char)(value << 1);
        PORTD |= _BV(PD7);
        PORTD &= (unsigned char)~_BV(PD7);
    }
    PORTB |= _BV(PB0);      /* transfer to the outputs */
    PORTB &= (unsigned char)~_BV(PB0);
}

/* Variables (16-bit signed, like Scratch's integers). */
static int i = 0;

int main(void)
{
    DDRD |= _BV(PD4);   /* sr */
    DDRD |= _BV(PD7);   /* sr */
    DDRB |= _BV(PB0);   /* sr */

    TCCR0A = _BV(WGM01);           /* CTC */
    OCR0A  = 249;                 /* 1 kHz */
    TIMSK0 = _BV(OCIE0A);
    TCCR0B = _BV(CS01) | _BV(CS00);
    sei();

    i = 0;
    for (;;) {
        bw_part_sr((unsigned char)~(bw_tab_font[bw_clamp(i, 2)]));
        delay_ms(200);
        i += 1;
    }

    for (;;) ;
}
