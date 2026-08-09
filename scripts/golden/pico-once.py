# Generated from BrickWright pseudocode by stc-compiler.
# Hand edits will be lost; change the pseudocode instead.
#
# @bw-begin
# @bw device pico
# @bw pin led GP25 output
# @bw-end
#
# MicroPython for the Raspberry Pi Pico. Nothing to compile:
# copy this to the board as main.py.
from machine import Pin
import time

# WHEN started:
def bw_script():
    for _ in range(3):
        _pin25.value(1)
        time.sleep_ms(100)
        _pin25.value(0)
        time.sleep_ms(100)

_pin25 = Pin(25, Pin.OUT)

_pin25.value(0)  # led off

bw_script()
