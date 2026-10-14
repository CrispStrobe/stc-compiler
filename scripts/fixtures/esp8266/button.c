/*
 * A sketch in the shape a generated program has: the LED on GPIO2 follows the
 * button on GPIO5, every press is counted by an interrupt and announced, and
 * setup() shifts one byte out on GPIO4 (data) and GPIO14 (clock).
 */
#include <Arduino.h>

#define LED 2
#define BUTTON 5

static volatile unsigned presses;

static void pressed(void) { presses++; }

void setup(void) {
  bw_serial_begin(115200);
  pinMode(LED, OUTPUT);
  pinMode(BUTTON, INPUT_PULLUP);
  pinMode(4, OUTPUT);
  pinMode(14, OUTPUT);
  shiftOut(4, 14, MSBFIRST, 0xA5);
  attachInterrupt(BUTTON, pressed, FALLING);
  bw_serial_write("ready\n");
}

void loop(void) {
  static unsigned shown;
  digitalWrite(LED, digitalRead(BUTTON) ? HIGH : LOW);
  if (shown != presses) {
    shown = presses;
    bw_serial_write("press\n");
  }
  delay(1);
}
