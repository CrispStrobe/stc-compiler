# Generated from BrickWright pseudocode by stc-compiler.
# Hand edits will be lost; change the pseudocode instead.
#
# @bw-begin
# @bw device pico
# @bw pin led GP25 output
# @bw pin dim GP15 pwm active-low
# @bw pin buzz GP16 tone
# @bw pin pot GP26 analog
# @bw pin btn GP14 input active-low
# @bw-end
#
# MicroPython for the Raspberry Pi Pico. Nothing to compile:
# copy this to the board as main.py.
from machine import Pin, ADC, PWM
import time

# Lookup tables. Tuples rather than lists: they are
# constant, and MicroPython keeps a tuple in flash rather
# than building it in RAM at import time.
font = (0x3F, 0x06, 0x5B,)

# Variables (Scratch integers).
i = 0

# WHEN started: (script 1)
def bw_task0():
    global i
    print('ready')
    i = 0
    while True:
        _pin25.value(1 - _pin25.value())
        _pwm15.duty_u16((100 - (font[i])) * 65535 // 100)
        _deadline = time.ticks_add(time.ticks_ms(), ((_adc26.read_u16() >> 6)))
        while time.ticks_diff(_deadline, time.ticks_ms()) > 0:
            yield
        i += 1
        yield

# WHEN btn pressed:
def bw_task1():
    _prev = 0
    while True:
        _now = 1 if (not _pin14.value()) else 0
        _fired = _now and not _prev
        _prev = _now
        if _fired:
            _hz = 880
            if _hz:
                _pwm16.freq(_hz)
                _pwm16.duty_u16(32768)
            else:
                _pwm16.duty_u16(0)
            _deadline = time.ticks_add(time.ticks_ms(), (100))
            while time.ticks_diff(_deadline, time.ticks_ms()) > 0:
                yield
            _hz = 0
            if _hz:
                _pwm16.freq(_hz)
                _pwm16.duty_u16(32768)
            else:
                _pwm16.duty_u16(0)
        yield

_pin25 = Pin(25, Pin.OUT)
_pwm15 = PWM(Pin(15))
_pwm15.freq(1000)
_pwm16 = PWM(Pin(16))
_adc26 = ADC(26)
_pin14 = Pin(14, Pin.IN, Pin.PULL_UP)

_pin25.value(0)  # led off

# One WHEN block = one generator. Each yields at every wait
# and every loop back-edge (Scratch's own contract), so no
# script can starve the others. This is the same scheduling
# contract the C targets get from a Duff's device -- which
# MicroPython cannot express, having no goto.
_tasks = [bw_task0(), bw_task1()]
while _tasks:
    for _t in tuple(_tasks):
        try:
            next(_t)
        except StopIteration:
            _tasks.remove(_t)
