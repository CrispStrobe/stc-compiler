/*
 * Start-up, serial output and a millisecond clock. See bw8266.h.
 *
 * All of this file is linked into instruction RAM: it runs before flash is
 * mapped (bw_start) or from an interrupt (tick).
 */
#include "bw8266.h"

extern u32 _bss_start, _bss_end;

void bw_start(void) __attribute__((section(".entry.text"), noreturn));
void bw_start(void) {
  /* The stack pointer is the one the ROM's loader left. */
  for (u32 *p = &_bss_start; p < &_bss_end; p++) *p = 0;
  /* Code linked for flash (the irom region of bw8266.ld) cannot run until flash is mapped. */
  Cache_Read_Enable(0, 0, 1);
  main();
  for (;;) {
  }
}

void bw_serial_begin(u32 baud) { rom_uart_div_modify(0, APB_HZ / baud); }

void bw_serial_write(const char *text) {
  for (; *text; text++) {
    while (UART0_TXFIFO_COUNT >= 126) {
    }
    UART0_FIFO = (u32)(unsigned char)*text;
  }
}

static volatile u32 milliseconds;

static void tick(void *unused) {
  (void)unused;
  milliseconds++;
  FRC2_ALARM = FRC2_ALARM + FRC2_TICKS_PER_MS;
}

void bw_millis_begin(void) {
  FRC2_CTRL = FRC2_ENABLE | FRC2_DIVIDE_16;
  FRC2_ALARM = FRC2_COUNT + FRC2_TICKS_PER_MS;
  ets_isr_attach(FRC2_INTERRUPT, tick, 0);
  DPORT_EDGE_INT_ENABLE |= DPORT_EDGE_INT_FRC2;
  ets_isr_unmask(1u << FRC2_INTERRUPT);
}

u32 bw_millis(void) { return milliseconds; }

void bw_interrupts_on(void) { __asm__ volatile("rsil a2, 0" : : : "a2"); }
