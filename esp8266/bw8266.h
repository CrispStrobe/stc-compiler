/*
 * A small bare-metal runtime for the ESP8266, with no Espressif SDK code.
 *
 * Register addresses and bit meanings are the chip's, written out here from
 * public documentation of the peripherals; no vendor header is included. The
 * ROM functions declared below are the chip's own mask ROM entry points.
 */
#ifndef BW8266_H
#define BW8266_H

typedef unsigned int u32;

#define REG(addr) (*(volatile u32 *)(addr))

/* GPIO */
#define GPIO_OUT_W1TS REG(0x60000304)
#define GPIO_OUT_W1TC REG(0x60000308)
#define GPIO_ENABLE_W1TS REG(0x60000310)
#define GPIO_IN REG(0x60000318)
/* Pin function select for GPIO2 (the on-board LED of most modules): bits 4, 5, 8; 0 = GPIO. */
#define IOMUX_GPIO2 REG(0x60000838)

/* UART0 */
#define UART0_FIFO REG(0x60000000)
#define UART0_STATUS REG(0x6000001C)
#define UART0_TXFIFO_COUNT ((UART0_STATUS >> 16) & 0xff)

/* The second hardware timer (FRC2): a free-running up counter with an alarm. */
#define FRC2_COUNT REG(0x60000624)
#define FRC2_CTRL REG(0x60000628)
#define FRC2_ALARM REG(0x60000630)
#define FRC2_ENABLE (1u << 7)
#define FRC2_DIVIDE_16 (2u << 1)
#define FRC2_INTERRUPT 10
/* Lets FRC2's alarm reach the CPU as an edge interrupt. */
#define DPORT_EDGE_INT_ENABLE REG(0x3FF00004)
#define DPORT_EDGE_INT_FRC2 (1u << 2)

/* The first hardware timer (FRC1): a down counter that interrupts at zero. */
#define FRC1_LOAD REG(0x60000600)
#define FRC1_CTRL REG(0x60000608)
#define FRC1_INT REG(0x6000060C)
#define FRC1_ENABLE (1u << 7)
#define FRC1_DIVIDE_16 (2u << 1)
#define FRC1_INTERRUPT 9
#define DPORT_EDGE_INT_FRC1 (1u << 1)

/* The peripheral clock, and FRC2 ticks per millisecond at divide-by-16. */
#define APB_HZ 80000000u
#define FRC2_TICKS_PER_MS (APB_HZ / 16u / 1000u)

/* Mask ROM */
void ets_isr_attach(int number, void (*routine)(void *), void *argument);
void ets_isr_unmask(u32 mask);
void rom_uart_div_modify(int uart, u32 divider);
void ets_delay_us(u32 microseconds);
/* Map flash into the address space at 0x40200000: (0, 0, 1) maps the first megabyte from offset 0. */
void Cache_Read_Enable(u32 odd_even, u32 megabyte_pair, u32 one_megabyte);

/* This runtime (runtime.c) */
void bw_serial_begin(u32 baud);
void bw_serial_write(const char *text);
void bw_millis_begin(void);
u32 bw_millis(void);
void bw_interrupts_on(void);

/*
 * The simulator's network device (net.c). Not ESP8266 hardware: it exists
 * only in the simulator, and bw_net_present() is 0 on a real chip.
 * Frames are Ethernet II frames: destination, source, type, payload.
 */
#define BW_NET_JOINED 1
#define BW_NET_NO_NETWORK -1
#define BW_NET_WRONG_PASSWORD -2
#define BW_NET_NO_DEVICE -3
int bw_net_present(void);
int bw_net_join(const char *ssid, const char *password);
void bw_net_mac(unsigned char mac[6]);
void bw_net_send(const unsigned char *frame, u32 length);
/* Copies the waiting frame (at most `room` bytes of it) and returns its full length; 0 if none waits. */
u32 bw_net_receive(unsigned char *frame, u32 room);

int main(void);

#endif
