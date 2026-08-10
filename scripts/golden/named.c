/* Generated from BrickWright pseudocode by stc-compiler.
 * Regenerating overwrites this file. Edits are not stranded, though:
 * BrickWright's C reader imports this back into blocks and names what
 * it cannot represent. stc-compiler itself only goes forwards. */
#include <Arduino.h>

/* No clock constant here on purpose: millis() and delay() are
 * already correct for whatever the board is actually clocked at,
 * so a CLOCK line in the pseudocode is carried for the other
 * targets and deliberately ignored on this one. */

void setup()
{
    pinMode(13, OUTPUT);
    digitalWrite(13, LOW);   /* led off */

    for (;;) {
        digitalWrite(13, !digitalRead(13));
        delay(500);
    }
}

void loop()
{
    /* the script ran once, in setup(); nothing repeats here */
}
