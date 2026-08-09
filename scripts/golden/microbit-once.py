# Generated from BrickWright pseudocode by stc-compiler.
# Hand edits will be lost; change the pseudocode instead.
#
# MicroPython for the BBC micro:bit. Nothing to compile: flash it
# with uflash, or paste it into python.microbit.org.
from microbit import *

# WHEN started:
def bw_script():
    for _ in range(3):
        pin0.write_digital(1)
        sleep(100)
        pin0.write_digital(0)
        sleep(100)

pin0.write_digital(0)  # led off

bw_script()
