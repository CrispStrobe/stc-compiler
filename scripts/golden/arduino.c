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
    pinMode(2, INPUT_PULLUP);
    digitalWrite(13, LOW);   /* led off */

    while (!(!digitalRead(2))) { }
    { unsigned int _i1;
      for (_i1 = 0; _i1 < (4); _i1++) {
            digitalWrite(13, !digitalRead(13));
            delay(analogRead(A0));
      }
    }
    if (analogRead(A0) > 512) {
        digitalWrite(13, HIGH);
    } else {
        digitalWrite(13, LOW);
    }
}

void loop()
{
    /* the script ran once, in setup(); nothing repeats here */
}
