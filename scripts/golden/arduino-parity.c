/* Generated from BrickWright pseudocode by stc-compiler.
 * Regenerating overwrites this file. Edits are not stranded, though:
 * BrickWright's C reader imports this back into blocks and names what
 * it cannot represent. stc-compiler itself only goes forwards. */
#include <Arduino.h>

/* No clock constant here on purpose: millis() and delay() are
 * already correct for whatever the board is actually clocked at,
 * so a CLOCK line in the pseudocode is carried for the other
 * targets and deliberately ignored on this one. */

/* Lookup tables. `const` on an AVR still costs RAM -- the
 * Harvard split means a plain const array is copied out of
 * flash at startup -- but PROGMEM would need pgm_read_byte at
 * every use, and the index expression is shared with the
 * other targets. A font is tens of bytes; a big table is the
 * case that would need a target hook for reads. */
static const unsigned char bw_tab_font[] = { 0x3F, 0x06, 0x5B, 0x4F };

/* A computed index is clamped rather than trusted: reading
 * past a table gives a plausible-looking wrong byte. */
static unsigned char bw_clamp(int i, unsigned char last)
{
    if (i < 0) return 0;
    if (i > (int)last) return last;
    return (unsigned char)i;
}

/* 0 Hz means silence, and the frequency is an expression, so
 * the choice has to be made at run time. */
static void bw_tone(unsigned char pin, unsigned int hz)
{
    if (hz) tone(pin, hz); else noTone(pin);
}

/* Variables (16-bit signed, like Scratch's integers). */
static int i = 0;

void setup()
{
    pinMode(9, OUTPUT);
    analogWrite(9, ((100 - (0)) * 255) / 100);   /* dim off */
    Serial.begin(9600);

    Serial.println("ready");
    i = 0;
    for (;;) {
        analogWrite(9, ((100 - (bw_tab_font[bw_clamp(i, 3)])) * 255) / 100);
        bw_tone(8, 440);
        Serial.println(analogRead(A0));
        delay(200);
        bw_tone(8, 0);
        i += 1;
    }
}

void loop()
{
    /* the script ran once, in setup(); nothing repeats here */
}
