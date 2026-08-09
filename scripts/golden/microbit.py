# Generated from BrickWright pseudocode by stc-compiler.
# Hand edits will be lost; change the pseudocode instead.
#
# MicroPython for the BBC micro:bit. Nothing to compile: flash it
# with uflash, or paste it into python.microbit.org.
from microbit import *

# write_digital has no read-back -- reading a pin would
# switch it to input mode -- so an output's level is
# remembered here instead.
_level = {'led': 0}

# Variables (Scratch integers).
hits = 0

# WHEN started: (script 1)
def bw_task0():
    while True:
        _level['led'] = 1 - _level['led']
        pin0.write_digital(_level['led'])
        _deadline = running_time() + (pin2.read_analog())
        while running_time() < _deadline:
            yield
        yield

# WHEN started: (script 2)
def bw_task1():
    global hits
    hits = 0
    while True:
        while not (button_a.is_pressed()):
            yield
        hits += 1
        pin1.write_digital(0)
        _deadline = running_time() + (50)
        while running_time() < _deadline:
            yield
        pin1.write_digital(1)
        yield

pin0.write_digital(0)  # led off
pin1.write_digital(1)  # spk off

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
