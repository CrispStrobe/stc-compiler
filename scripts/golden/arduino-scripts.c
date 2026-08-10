/* Generated from BrickWright pseudocode by stc-compiler.
 * Regenerating overwrites this file. Edits are not stranded, though:
 * BrickWright's C reader imports this back into blocks and names what
 * it cannot represent. stc-compiler itself only goes forwards. */
#include <Arduino.h>

/* No clock constant here on purpose: millis() and delay() are
 * already correct for whatever the board is actually clocked at,
 * so a CLOCK line in the pseudocode is carried for the other
 * targets and deliberately ignored on this one. */

/* REPEAT counters live across yields. */
static unsigned int bw_i1;

static unsigned int bw_task0_state;
static unsigned long bw_task0_until;
/* WHEN started: (script 1) */
static void bw_task0(void)
{
    switch (bw_task0_state) {
    case 0:
    bw_task0_state = 1;
    case 1:
    digitalWrite(13, !digitalRead(13));
    bw_task0_until = millis() + (500);
    bw_task0_state = 2;
    case 2:
    if ((long)(millis() - bw_task0_until) < 0) return;
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
    bw_i1 = (3);
    bw_task1_state = 1;
    case 1:
    if (bw_i1) {
        digitalWrite(12, !digitalRead(12));
        bw_task1_until = millis() + (150);
        bw_task1_state = 2;
        case 2:
        if ((long)(millis() - bw_task1_until) < 0) return;
        bw_i1--;
        bw_task1_state = 1;
        return;
    }
    bw_task1_state = 3;
    case 3:
    digitalWrite(12, !digitalRead(12));
    bw_task1_until = millis() + (300);
    bw_task1_state = 4;
    case 4:
    if ((long)(millis() - bw_task1_until) < 0) return;
    bw_task1_state = 3;
    return;
    }
    bw_task1_state = 0xFFFF;   /* ran to the end */
}

void setup()
{
    pinMode(13, OUTPUT);
    pinMode(12, OUTPUT);
    digitalWrite(13, LOW);   /* slow off */
    digitalWrite(12, HIGH);   /* fast off */
}

void loop()
{
    bw_task0();
    bw_task1();
}
