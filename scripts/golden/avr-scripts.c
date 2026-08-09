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

static unsigned int bw_task0_state;
static unsigned long bw_task0_until;
/* WHEN started: (script 1) */
static void bw_task0(void)
{
    switch (bw_task0_state) {
    case 0:
    bw_task0_state = 1;
    case 1:
    PINB = _BV(PB5);
    bw_task0_until = bw_now() + (500);
    bw_task0_state = 2;
    case 2:
    if ((long)(bw_now() - bw_task0_until) < 0) return;
    bw_task0_state = 1;
    return;
    }
    bw_task0_state = 0xFFFF;   /* ran to the end */
}

static unsigned int bw_task1_state;
static unsigned long bw_task1_until;
/* WHEN started: (script 2) */
static void bw_task1(void)
{
    switch (bw_task1_state) {
    case 0:
    bw_task1_state = 1;
    case 1:
    PINB = _BV(PB4);
    bw_task1_until = bw_now() + (300);
    bw_task1_state = 2;
    case 2:
    if ((long)(bw_now() - bw_task1_until) < 0) return;
    bw_task1_state = 1;
    return;
    }
    bw_task1_state = 0xFFFF;   /* ran to the end */
}

int main(void)
{
    DDRB |= _BV(PB5);   /* slow */
    DDRB |= _BV(PB4);   /* fast */
    PORTB &= (unsigned char)~_BV(PB5);   /* slow off */
    PORTB |= _BV(PB4);   /* fast off */

    TCCR0A = _BV(WGM01);           /* CTC */
    OCR0A  = 249;                 /* 1 kHz */
    TIMSK0 = _BV(OCIE0A);
    TCCR0B = _BV(CS01) | _BV(CS00);
    sei();

    for (;;) {
        bw_task0();
        bw_task1();
    }
}
