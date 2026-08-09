# Generated from BrickWright pseudocode by stc-compiler.
# Hand edits will be lost; change the pseudocode instead.
#
# MicroPython for the BBC micro:bit. Nothing to compile: flash it
# with uflash, or paste it into python.microbit.org.
from microbit import *

# 74HC595: eight outputs for three pins. Data is
# sampled on the rising edge of the shift clock, the latch
# transfers on its own. MSB first.
def bw_part_sr(value):
    pin7.write_digital(0)
    pin8.write_digital(0)
    for _ in range(8):
        pin6.write_digital(1 if value & 0x80 else 0)
        value = (value << 1) & 0xFF
        pin7.write_digital(1)
        pin7.write_digital(0)
    pin8.write_digital(1)   # transfer to the outputs
    pin8.write_digital(0)

# WHEN started:
def bw_script():
    while True:
        bw_part_sr((170) & 0xFF)
        sleep(100)
        bw_part_sr((85) & 0xFF)
        sleep(100)

bw_script()
