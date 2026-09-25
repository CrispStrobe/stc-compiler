/* picolibc console + minimal syscalls for the emulated RV32 machine.
 * stdout/stderr go to the machine's ecall write (a7=64); exit is a7=93.
 * picolibc 1.5.x exposes stdio via the __iob[] array. */
#include <stdio.h>
#include <stdint.h>
static long ec(long n,long a,long b,long c){
  register long x10 __asm__("a0")=a,x11 __asm__("a1")=b,x12 __asm__("a2")=c,x17 __asm__("a7")=n;
  __asm__ volatile("ecall":"+r"(x10):"r"(x11),"r"(x12),"r"(x17):"memory"); return x10;
}
static int con_putc(char c, FILE *f){ (void)f; ec(64,1,(long)&c,1); return (unsigned char)c; }
static int con_getc(FILE *f){ (void)f; return EOF; }
static FILE __con = FDEV_SETUP_STREAM(con_putc, con_getc, NULL, _FDEV_SETUP_RW);
struct __file *const __iob[3] = { &__con, &__con, &__con };
void _exit(int code){ ec(93,code,0,0); for(;;){} }
extern char __heap_start[], __heap_end[];
static char *hp = __heap_start;
void *sbrk(intptr_t incr){ char *p=hp; if (hp+incr > __heap_end) return (void*)-1; hp+=incr; return p; }
void *_sbrk(intptr_t incr){ return sbrk(incr); }
