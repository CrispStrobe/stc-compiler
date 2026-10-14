/* The Arduino-style layer. See Arduino.h for what is and is not provided. */
#include "Arduino.h"

#define GPIO_STATUS REG(0x6000031C)
#define GPIO_STATUS_W1TC REG(0x60000324)
#define GPIO_PIN(n) REG(0x60000328 + 4 * (n))
#define GPIO_ENABLE_W1TC REG(0x60000314)
#define GPIO_INTERRUPT 4
#define PIN_INT_TYPE_SHIFT 7
#define MUX_PULLUP (1u << 7)

/*
 * The pin function register of each GPIO, as an offset from 0x60000800, and
 * the function number that selects "GPIO" there; 0 offset = not usable.
 */
static const unsigned char MUX_OFFSET[16] = {0x34, 0x18, 0x38, 0x14, 0x3c, 0x40, 0, 0, 0, 0, 0, 0, 0x04, 0x08, 0x0c, 0x10};
static const unsigned char MUX_GPIO_FUNCTION[16] = {0, 3, 0, 3, 0, 0, 0, 0, 0, 0, 0, 0, 3, 3, 3, 3};

static void (*routines[16])(void);

static int usable(int pin) { return pin >= 0 && pin < 16 && MUX_OFFSET[pin] != 0; }

void pinMode(int pin, int mode) {
  if (!usable(pin)) return;
  volatile u32 *mux = (volatile u32 *)(0x60000800u + MUX_OFFSET[pin]);
  u32 function = MUX_GPIO_FUNCTION[pin];
  /* The function number sits in bits 4, 5 and 8; bit 7 is the pull-up. */
  u32 value = *mux & ~(0x130u | MUX_PULLUP);
  value |= ((function & 3u) << 4) | ((function & 4u) << 6);
  if (mode == INPUT_PULLUP) value |= MUX_PULLUP;
  *mux = value;
  if (mode == OUTPUT) GPIO_ENABLE_W1TS = 1u << pin;
  else GPIO_ENABLE_W1TC = 1u << pin;
}

void digitalWrite(int pin, int value) {
  if (!usable(pin)) return;
  if (value) GPIO_OUT_W1TS = 1u << pin;
  else GPIO_OUT_W1TC = 1u << pin;
}

int digitalRead(int pin) {
  if (!usable(pin)) return 0;
  return (GPIO_IN >> pin) & 1u;
}

unsigned long millis(void) { return bw_millis(); }

/* The second hardware timer counts at 5 MHz once bw_millis_begin has started it. */
unsigned long micros(void) { return FRC2_COUNT / 5u; }

void delay(unsigned long milliseconds) {
  unsigned long start = bw_millis();
  while (bw_millis() - start < milliseconds) {
  }
}

void delayMicroseconds(unsigned int microseconds) { ets_delay_us(microseconds); }

static void pin_interrupt(void *unused) {
  (void)unused;
  u32 pending = GPIO_STATUS;
  GPIO_STATUS_W1TC = pending;
  for (int pin = 0; pin < 16; pin++) {
    if ((pending & (1u << pin)) && routines[pin]) routines[pin]();
  }
}

void attachInterrupt(int pin, void (*routine)(void), int mode) {
  static int attached;
  if (!usable(pin) || mode < RISING || mode > CHANGE) return;
  routines[pin] = routine;
  if (!attached) {
    attached = 1;
    ets_isr_attach(GPIO_INTERRUPT, pin_interrupt, 0);
    ets_isr_unmask(1u << GPIO_INTERRUPT);
  }
  GPIO_STATUS_W1TC = 1u << pin;
  GPIO_PIN(pin) = (GPIO_PIN(pin) & ~(7u << PIN_INT_TYPE_SHIFT)) | ((u32)mode << PIN_INT_TYPE_SHIFT);
}

void detachInterrupt(int pin) {
  if (!usable(pin)) return;
  GPIO_PIN(pin) = GPIO_PIN(pin) & ~(7u << PIN_INT_TYPE_SHIFT);
  routines[pin] = 0;
}

void shiftOut(int dataPin, int clockPin, int bitOrder, unsigned char value) {
  for (int i = 0; i < 8; i++) {
    int bit = bitOrder == LSBFIRST ? i : 7 - i;
    digitalWrite(dataPin, (value >> bit) & 1);
    digitalWrite(clockPin, HIGH);
    digitalWrite(clockPin, LOW);
  }
}

unsigned long pulseIn(int pin, int state, unsigned long timeoutMicroseconds) {
  unsigned long start = micros();
  /* Let a pulse already under way end, wait for the next to begin, then time it. */
  while (digitalRead(pin) == state) if (micros() - start >= timeoutMicroseconds) return 0;
  while (digitalRead(pin) != state) if (micros() - start >= timeoutMicroseconds) return 0;
  unsigned long began = micros();
  while (digitalRead(pin) == state) if (micros() - start >= timeoutMicroseconds) return 0;
  return micros() - began;
}

/*
 * Waveforms: each active pin alternates between `high` and `low` FRC2 ticks
 * (5 per microsecond). The first timer is loaded with the time to the next
 * edge of any pin, and its interrupt makes the edges that are due.
 */
static struct {
  u32 high, low, next, until;
  unsigned char active, level, timed;
} wave[16];

static u32 interrupts_off(void) {
  u32 state;
  __asm__ volatile("rsil %0, 15" : "=a"(state));
  return state;
}

static void interrupts_restore(u32 state) { __asm__ volatile("wsr %0, ps\n rsync" : : "a"(state) : "memory"); }

static void reschedule(void) {
  u32 now = FRC2_COUNT, soonest = 0;
  int any = 0;
  for (int pin = 0; pin < 16; pin++) {
    if (!wave[pin].active) continue;
    if (wave[pin].timed && (int)(bw_millis() - wave[pin].until) >= 0) {
      wave[pin].active = 0;
      digitalWrite(pin, LOW);
      continue;
    }
    while ((int)(now - wave[pin].next) >= 0) {
      wave[pin].level ^= 1;
      digitalWrite(pin, wave[pin].level);
      wave[pin].next += wave[pin].level ? wave[pin].high : wave[pin].low;
    }
    u32 wait = wave[pin].next - now;
    if (!any || wait < soonest) soonest = wait;
    any = 1;
  }
  if (!any) {
    FRC1_CTRL = 0;
    return;
  }
  FRC1_CTRL = FRC1_ENABLE | FRC1_DIVIDE_16;
  FRC1_LOAD = soonest < 10 ? 10 : soonest;
}

static void timer_interrupt(void *unused) {
  (void)unused;
  FRC1_INT = 1;
  reschedule();
}

static void start_wave(int pin, u32 high, u32 low, unsigned long milliseconds) {
  static int attached;
  if (!attached) {
    attached = 1;
    ets_isr_attach(FRC1_INTERRUPT, timer_interrupt, 0);
    DPORT_EDGE_INT_ENABLE |= DPORT_EDGE_INT_FRC1;
    ets_isr_unmask(1u << FRC1_INTERRUPT);
  }
  u32 state = interrupts_off();
  wave[pin].high = high;
  wave[pin].low = low;
  wave[pin].level = 1;
  wave[pin].timed = milliseconds != 0;
  wave[pin].until = bw_millis() + milliseconds;
  digitalWrite(pin, HIGH);
  wave[pin].next = FRC2_COUNT + high;
  wave[pin].active = 1;
  reschedule();
  interrupts_restore(state);
}

static void stop_wave(int pin, int level) {
  u32 state = interrupts_off();
  wave[pin].active = 0;
  digitalWrite(pin, level);
  reschedule();
  interrupts_restore(state);
}

#define PWM_PERIOD 5000u /* 1 kHz in FRC2 ticks */

void analogWrite(int pin, int value) {
  if (!usable(pin)) return;
  pinMode(pin, OUTPUT);
  if (value <= 0) stop_wave(pin, LOW);
  else if (value >= 255) stop_wave(pin, HIGH);
  else {
    u32 high = PWM_PERIOD * (u32)value / 255u;
    start_wave(pin, high, PWM_PERIOD - high, 0);
  }
}

void tone(int pin, unsigned int frequency, unsigned long durationMilliseconds) {
  if (!usable(pin)) return;
  pinMode(pin, OUTPUT);
  if (!frequency) {
    stop_wave(pin, LOW);
    return;
  }
  u32 half = 2500000u / frequency;
  if (!half) half = 1;
  start_wave(pin, half, half, durationMilliseconds);
}

void noTone(int pin) {
  if (usable(pin)) stop_wave(pin, LOW);
}

int main(void) {
  bw_millis_begin();
  bw_interrupts_on();
  setup();
  for (;;) loop();
}
