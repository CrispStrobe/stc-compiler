# Generated from BrickWright pseudocode by stc-compiler.
# Hand edits will be lost; change the pseudocode instead.
#
# @bw-begin
# @bw device microbit
# @bw pin btn BUTTON_A input
# @bw pin led P0 output
# @bw-end
#
# MicroPython for the BBC micro:bit. Nothing to compile: flash it
# with uflash, or paste it into python.microbit.org.
from microbit import *

# Variables (Scratch integers).
hits = 0

# WHEN started: (script 1)
def bw_task0():
    while True:
        _deadline = running_time() + (500)
        while running_time() < _deadline:
            yield
        yield

# WHEN btn pressed:
def bw_task1():
    global hits
    _prev = 0
    while True:
        _now = 1 if button_a.is_pressed() else 0
        _fired = _now and not _prev
        _prev = _now
        if _fired:
            hits = 0
            hits += 1
            pin0.write_digital(1)
        yield

pin0.write_digital(0)  # led off

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
