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

/* 10-bit ADC, polled, AVcc as reference. The prescaler is set
 * once in main(); this only selects the channel and waits. */
static unsigned int adc_read(unsigned char channel)
{
    ADMUX = (unsigned char)(_BV(REFS0) | (channel & 0x0F));
    ADCSRA |= _BV(ADSC);
    while (ADCSRA & _BV(ADSC)) ;
    return ADC;
}

int main(void)
{
    DDRB |= _BV(PB5);   /* slow */
    DDRB |= _BV(PB4);   /* fast */
    DDRD &= (unsigned char)~_BV(PD2);
    PORTD |= _BV(PD2);   /* btn pull-up */
    DDRC &= (unsigned char)~_BV(PC0);   /* pot analog in */
    PORTB &= (unsigned char)~_BV(PB5);   /* slow off */
    PORTB |= _BV(PB4);   /* fast off */

    ADCSRA = _BV(ADEN) | _BV(ADPS2) | _BV(ADPS1) | _BV(ADPS0);

    TCCR0A = _BV(WGM01);           /* CTC */
    OCR0A  = 249;                 /* 1 kHz */
    TIMSK0 = _BV(OCIE0A);
    TCCR0B = _BV(CS01) | _BV(CS00);
    sei();

    while (!(!(PIND & _BV(PD2)))) { }
    { unsigned int _i1;
      for (_i1 = 0; _i1 < (3); _i1++) {
            PINB = _BV(PB5);
            delay_ms(adc_read(0));
      }
    }
    if (adc_read(0) > 512) {
        PORTB &= (unsigned char)~_BV(PB4);
    } else {
        PORTB |= _BV(PB4);
    }

    for (;;) ;
}
