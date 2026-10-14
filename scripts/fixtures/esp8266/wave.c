/* analogWrite a quarter duty on GPIO4 and play a 1 kHz tone on GPIO5 for 20 milliseconds. */
#include <Arduino.h>

void setup(void) {
  bw_serial_begin(115200);
  analogWrite(4, 64);
  tone(5, 1000, 20);
  bw_serial_write("ready\n");
}

void loop(void) {}
