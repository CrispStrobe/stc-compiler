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
static const unsigned char bw_tab_font[] = { 0x3F, 0x06, 0x5B, 0x4F };

/* A computed index is clamped rather than trusted: reading
 * past a table gives a plausible-looking wrong byte. */
static unsigned char bw_clamp(int i, unsigned char last)
{
    if (i < 0) return 0;
    if (i > (int)last) return last;
    return (unsigned char)i;
}

/* Tone: Timer 1 in CTC mode toggling OC1A, so the frequency
 * is F_CPU/(2*8*(OCR1A+1)) and the whole audible band is
 * reachable. Toggling in hardware costs no interrupts and
 * does not drift, which a software square wave would while
 * the scheduler is busy elsewhere.
 *
 * This takes Timer 1 outright, which is why PWM on D9 and
 * D10 is refused in the same program. */
static void bw_tone(unsigned int hz)
{
    if (hz) {
        OCR1A  = (unsigned int)(F_CPU / 16UL / (unsigned long)hz - 1UL);
        TCCR1A = _BV(COM1A0);          /* toggle OC1A on match */
        TCCR1B = _BV(WGM12) | _BV(CS11);
    } else {
        TCCR1A = 0;                    /* release the pin */
        TCCR1B = 0;
        PORTB &= (unsigned char)~_BV(PB1);
    }
}

/* Serial console on USART0, 8N1. Blocking on UDRE0 is
 * deliberate: a ring buffer costs RAM this part has little of,
 * and a dropped diagnostic is worse than a slow one. */
static void bw_putc(char c)
{
    while (!(UCSR0A & _BV(UDRE0))) ;
    UDR0 = (unsigned char)c;
}

static void bw_print(const char *s)
{
    while (*s) bw_putc(*s++);
    bw_putc('\r');
    bw_putc('\n');
}

static void bw_print_num(int v)
{
    char buffer[7];
    unsigned char i = 0;
    unsigned int u;
    if (v < 0) { bw_putc('-'); u = (unsigned int)(-v); }
    else u = (unsigned int)v;
    do { buffer[i++] = (char)('0' + (u % 10)); u /= 10; } while (u);
    while (i) bw_putc(buffer[--i]);
    bw_putc('\r');
    bw_putc('\n');
}

/* 10-bit ADC, polled, AVcc as reference. The prescaler is set
 * once in main(); this only selects the channel and waits. */
static unsigned int adc_read(unsigned char channel)
{
    ADMUX = (unsigned char)(_BV(REFS0) | (channel & 0x0F));
    ADCSRA |= _BV(ADSC);
    while (ADCSRA & _BV(ADSC)) ;
    return ADC;
}

/* Variables (16-bit signed, like Scratch's integers). */
static int i = 0;

int main(void)
{
    DDRB |= _BV(PB3);   /* dim (pwm) */
    DDRB |= _BV(PB1);   /* buzz (tone) */
    DDRC &= (unsigned char)~_BV(PC0);   /* pot analog in */

    TCCR2A = _BV(COM2A1) | _BV(WGM20) | _BV(WGM21);
    TCCR2B = _BV(CS22);
    OCR2A = ((100 - (0)) * 255) / 100;   /* dim off */

    /* USART0, 8N1. UBRR0 is the divisor for the baud
     * rate, derived from F_CPU at compile time. */
    UBRR0 = (unsigned int)(F_CPU / 16UL / 9600UL - 1UL);
    UCSR0B = _BV(TXEN0);
    UCSR0C = _BV(UCSZ01) | _BV(UCSZ00);

    ADCSRA = _BV(ADEN) | _BV(ADPS2) | _BV(ADPS1) | _BV(ADPS0);

    TCCR0A = _BV(WGM01);           /* CTC */
    OCR0A  = 249;                 /* 1 kHz */
    TIMSK0 = _BV(OCIE0A);
    TCCR0B = _BV(CS01) | _BV(CS00);
    sei();

    bw_print("ready");
    i = 0;
    for (;;) {
        OCR2A = ((100 - (bw_tab_font[bw_clamp(i, 3)])) * 255) / 100;
        bw_tone(440);
        bw_print_num(adc_read(0));
        delay_ms(200);
        bw_tone(0);
        i += 1;
    }

    for (;;) ;
}
