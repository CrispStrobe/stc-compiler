/*
 * An Arduino-style layer on the blob-free runtime: the calls a generated
 * sketch uses, written here against the chip's registers. No Arduino core
 * and no Espressif SDK code is involved.
 *
 * PROVIDED: pinMode, digitalWrite, digitalRead, delay, delayMicroseconds,
 * millis, micros, attachInterrupt, detachInterrupt, shiftOut, pulseIn,
 * analogWrite (1 kHz, values 0-255), tone, noTone, and the runtime's own
 * serial calls (bw_serial_begin, bw_serial_write). analogWrite and tone are
 * made in software on the first hardware timer, as on the chip there is no
 * PWM block; tone takes three arguments (0 milliseconds: until noTone).
 *
 * NOT PROVIDED, and a sketch that uses one does not link:
 *   analogRead   the chip's converter is driven through undocumented registers
 *   Servo        a C++ class; analogWrite at 50 Hz is not offered either
 *
 * Pins are GPIO numbers. GPIO 0-5 and 12-15 are usable; 6-11 are wired to
 * the flash chip on every module and GPIO16 belongs to a different block:
 * both are refused by doing nothing, and digitalRead of them reads 0.
 * micros() wraps after about 14 minutes (Arduino's wraps after about 71).
 */
#ifndef ARDUINO_H
#define ARDUINO_H

#include "bw8266.h"

#define LOW 0
#define HIGH 1
#define INPUT 0
#define OUTPUT 1
#define INPUT_PULLUP 2
#define RISING 1
#define FALLING 2
#define CHANGE 3
#define LSBFIRST 0
#define MSBFIRST 1
#define LED_BUILTIN 2

void pinMode(int pin, int mode);
void digitalWrite(int pin, int value);
int digitalRead(int pin);
void delay(unsigned long milliseconds);
void delayMicroseconds(unsigned int microseconds);
unsigned long millis(void);
unsigned long micros(void);
void attachInterrupt(int pin, void (*routine)(void), int mode);
void detachInterrupt(int pin);
void shiftOut(int dataPin, int clockPin, int bitOrder, unsigned char value);
unsigned long pulseIn(int pin, int state, unsigned long timeoutMicroseconds);
void analogWrite(int pin, int value);
void tone(int pin, unsigned int frequency, unsigned long durationMilliseconds);
void noTone(int pin);

/* The sketch. */
void setup(void);
void loop(void);

#endif
