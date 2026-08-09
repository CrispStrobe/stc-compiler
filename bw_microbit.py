"""
bw_microbit — the BBC micro:bit as a target, emitting MicroPython.

This is the target the interface was drawn for, and the one that proves where
the seam had to go. Every other target so far emits C and shares
`stmts_c`/`stmts_task`: they differ in which register a pin write becomes, and
that is all. The micro:bit differs in the *lowering*.

Concretely: several `WHEN started:` blocks compile to cooperative tasks, and
on the 8051 and AVR that is a Duff's device -- a `switch` whose `case` labels
sit inside the loops, so a task resumes by jumping back into the middle of its
own control flow. MicroPython has no `goto` and no `switch`, so that shape
cannot be expressed at all.

What it does have is generators. A task becomes a generator function, and
every place the C back end would set `<task>_state` and `return`, this one
says `yield`. The scheduler round-robins `next()` over them. The contract is
identical -- yield at every wait and every loop back-edge, so no script can
starve another -- while the code implementing it has nothing in common.

That is why `Target.emit` exists rather than only the primitive hooks: a
target has to be able to say "none of the shared walkers apply to me".

Not a compiler target. MicroPython is interpreted on the device, so there is
nothing to build: the output is a .py to paste into the micro:bit editor or
flash with uflash. `POST /compile` refuses and says so.
"""

from __future__ import annotations

import re

import stc_pseudocode as sp


# Edge-connector pins, plus the two buttons. `where` is canonicalised to the
# spelling printed on the board ("P0", "BUTTON_A"), and the MicroPython object
# is derived from it.
MICROBIT_PIN_RE = re.compile(r"^(?:p(\d{1,2})|(button_[ab]))$", re.I)

# read_analog() works on these only. The rest are digital-capable but have no
# ADC channel behind them.
ANALOG_PINS = {0, 1, 2, 3, 4, 10}

# Shared with the 5x5 LED matrix. Using one of these as a GPIO needs the
# display turned off first, or the matrix scan fights whatever is driving the
# pin -- the same class of hardware fact as the 8051's active-low LEDs, and
# the same reason to encode it rather than document it.
DISPLAY_PINS = {3, 4, 6, 7, 9, 10}


class MicrobitPin(sp.Pin):
    """A micro:bit pin. `obj` is the MicroPython object it maps to."""

    def __init__(self, name, where, direction, active_low, obj, number=None):
        super().__init__(name, where, direction, active_low)
        self.obj = obj
        self.number = number        # None for the buttons


class MicrobitTarget(sp.Target):
    """BBC micro:bit V1/V2, emitting MicroPython."""

    key = "microbit"
    display = "BBC micro:bit"
    toolchain = "uflash"

    # PORT and PART are not here on purpose. A PORT is eight bits of one
    # register written at once, and a PART is a 74HC595 wired to P0.0-style
    # pins; both are 8051 shapes with no micro:bit equivalent, and `require()`
    # refuses them by name rather than emitting something that looks close.
    supports = frozenset({"pwm", "tone", "print", "table"})
    source_extension = "py"
    compile_hint = ("MicroPython is interpreted on the device, so there is "
                    "nothing to compile: flash the .py with uflash, or paste "
                    "it into python.microbit.org.")

    # ---- pins -----------------------------------------------------------
    def resolve_pin(self, program, name, where, direction, active_low, line):
        match = MICROBIT_PIN_RE.match(where)
        if not match:
            raise sp.PseudocodeError(
                line, f"{where.upper()} is not a pin on the {self.display}; "
                      "use P0-P20, BUTTON_A or BUTTON_B")
        number, button = match.groups()

        if button:
            if direction != "input":
                raise sp.PseudocodeError(
                    line, f"{button.upper()} is a button and can only be an "
                          f"INPUT, not an {direction.upper()}")
            return MicrobitPin(name, button.upper(), direction, active_low,
                               button.lower())

        number = int(number)
        if number > 20:
            raise sp.PseudocodeError(
                line, f"the {self.display} has P0-P20, not P{number}")
        # PWM and tone need no special pin here: write_analog works on any
        # digital pin, and music.pitch takes the pin as an argument. That is a
        # real difference from the STC12, where PWM exists only on the PCA
        # pins and there is exactly one tone because there is one Timer 1.
        if direction == "analog" and number not in ANALOG_PINS:
            raise sp.PseudocodeError(
                line, f"P{number} has no ADC on the {self.display}; "
                      f"analog input is P"
                      + ", P".join(str(n) for n in sorted(ANALOG_PINS)))
        return MicrobitPin(name, f"P{number}", direction, active_low,
                           f"pin{number}", number)

    # The primitive hooks still exist and are still used -- by this target's
    # own walkers. What changes is that the SHARED walkers do not apply.
    def write_pin(self, pin, high):
        return f"{pin.obj}.write_digital({1 if high else 0})"

    def toggle_pin(self, pin):
        # There is no read-back of an output pin here: read_digital() would
        # switch the pin to input mode to answer. So the level is tracked in
        # a dict, which also avoids a `global` declaration at every use.
        return (f"_level[{pin.name!r}] = 1 - _level[{pin.name!r}]\n"
                f"{pin.obj}.write_digital(_level[{pin.name!r}])")

    def read_pin(self, pin):
        read = (f"{pin.obj}.is_pressed()" if pin.number is None
                else f"{pin.obj}.read_digital()")
        return f"(not {read})" if pin.active_low else read

    def read_analog(self, pin):
        return f"{pin.obj}.read_analog()"

    def write_pwm(self, pin, value: str) -> str:
        # write_analog takes 0-1023 as the proportion of time the PIN is high,
        # while the AST stores the percentage of time the LOAD is on. They are
        # the same number only on an active-high pin.
        # The outer parentheses are load-bearing: `100 - x * 1023 // 100`
        # binds as `100 - ((x * 1023) // 100)`, which is a different and
        # entirely plausible-looking brightness.
        duty = f"(100 - ({value}))" if pin.active_low else f"({value})"
        return f"{pin.obj}.write_analog({duty} * 1023 // 100)"

    def delay(self, ms):
        return f"sleep({ms})"

    def now(self):
        return "running_time()"

    # ---- the emitter ----------------------------------------------------
    def emit(self, program) -> str:
        pins = {pin.name: pin for pin in program.pins.values()}
        tasks = len(program.whens) > 1
        # Names that live at module scope and are assigned inside functions,
        # so every function that touches them needs a `global`.
        globals_ = list(program.variables)

        tone_pins = [p for p in program.pins.values() if p.direction == "tone"]
        out = [
            "# Generated from BrickWright pseudocode by stc-compiler.",
            "# Hand edits will be lost; change the pseudocode instead.",
            "#",
            "# MicroPython for the BBC micro:bit. Nothing to compile: flash it",
            "# with uflash, or paste it into python.microbit.org.",
            "from microbit import *",
        ]
        if tone_pins:
            out.append("import music")
        out.append("")

        if program.tables:
            out += ["# Lookup tables. Tuples rather than lists: they are",
                    "# constant, and MicroPython keeps a tuple in flash rather",
                    "# than building it in RAM at import time."]
            for name, values in program.tables.items():
                packed = ", ".join(f"0x{v:02X}" for v in values)
                out.append(f"{name} = ({packed},)")
            out.append("")

        toggled = sorted({node.pin for node in _walk(program)
                          if isinstance(node, sp.Toggle)})
        if toggled:
            out += ["# write_digital has no read-back -- reading a pin would",
                    "# switch it to input mode -- so an output's level is",
                    "# remembered here instead.",
                    "_level = {" + ", ".join(f"{n!r}: 0" for n in toggled) + "}",
                    ""]

        if program.variables:
            out += ["# Variables (Scratch integers).",
                    *(f"{name} = 0" for name in program.variables),
                    ""]

        # CLOCK is meaningless here and saying so is better than ignoring it:
        # running_time() and sleep() are the runtime's, not ours.
        used_display = sorted(
            {pin.number for pin in program.pins.values()
             if pin.number in DISPLAY_PINS})

        for procedure in program.procedures.values():
            params = ", ".join(procedure.params)
            out += [f"# DEFINE {procedure.name}",
                    f"def {procedure.c_name}({params}):"]
            body = self._stmts(procedure.body, 1, pins, program, False,
                               globals_ + toggled)
            out += body or ["    pass"]
            out.append("")

        task_names = []
        for number, block in enumerate(program.whens):
            name = f"bw_task{number}" if tasks else "bw_script"
            task_names.append(name)
            out += [f"# WHEN started:"
                    + (f" (script {number + 1})" if tasks else ""),
                    f"def {name}():"]
            body = self._stmts(block, 1, pins, program, tasks,
                               globals_ + toggled)
            out += body or ["    pass"]
            out.append("")

        if used_display:
            out += ["# P" + ", P".join(str(n) for n in used_display)
                    + " are wired to the 5x5 LED matrix. The display driver",
                    "# scans those pins continuously and would fight anything",
                    "# else driving them, so it is switched off.",
                    "display.off()",
                    ""]

        for pin in program.pins.values():
            if pin.direction == "output":
                out.append(f"{pin.obj}.write_digital"
                           f"({1 if pin.active_low else 0})"
                           f"  # {pin.name} off")
        if any(p.direction == "output" for p in program.pins.values()):
            out.append("")

        if tasks:
            out += [
                "# One WHEN block = one generator. Each yields at every wait",
                "# and every loop back-edge (Scratch's own contract), so no",
                "# script can starve the others. This is the same scheduling",
                "# contract the C targets get from a Duff's device -- which",
                "# MicroPython cannot express, having no goto.",
                "_tasks = [" + ", ".join(f"{n}()" for n in task_names) + "]",
                "while _tasks:",
                "    for _t in tuple(_tasks):",
                "        try:",
                "            next(_t)",
                "        except StopIteration:",
                "            _tasks.remove(_t)",
                "",
            ]
        else:
            out += [f"{task_names[0]}()", ""]
        return "\n".join(out)

    # ---- statement lowering ---------------------------------------------
    def _stmts(self, body, depth, pins, program, tasks, globals_) -> list[str]:
        pad = "    " * depth
        out: list[str] = []

        if depth == 1 and globals_:
            assigned = {node.name for node in _walk_body(body)
                        if isinstance(node, (sp.SetVar, sp.ChangeVar))}
            needed = [g for g in dict.fromkeys(globals_)
                      if g in assigned and g in program.variables]
            if needed:
                out.append(f"{pad}global " + ", ".join(needed))

        for node in body:
            if isinstance(node, sp.SetPin):
                out.append(pad + self.write_pin(pins[node.pin], node.high))
            elif isinstance(node, sp.Toggle):
                out += [pad + line for line
                        in self.toggle_pin(pins[node.pin]).split("\n")]
            elif isinstance(node, sp.SetPwm):
                out.append(pad + self.write_pwm(pins[node.pin],
                                                self._expr(node.value, pins)))
            elif isinstance(node, sp.SetTone):
                # music.pitch plays until stopped when duration is -1 and
                # wait is False. 0 Hz means silence, and since the frequency
                # is an expression the choice has to be made at run time.
                obj = pins[node.pin].obj
                out += [f"{pad}_hz = {self._expr(node.hz, pins)}",
                        f"{pad}if _hz:",
                        f"{pad}    music.pitch(_hz, -1, {obj}, False)",
                        f"{pad}else:",
                        f"{pad}    music.stop({obj})"]
            elif isinstance(node, sp.Print):
                if node.value is None:
                    out.append(f"{pad}print({node.text!r})")
                else:
                    out.append(f"{pad}print({self._expr(node.value, pins)})")
            elif isinstance(node, sp.Wait):
                ms = self._ms(node, pins)
                if tasks:
                    # The generator equivalent of "set state, return, and
                    # re-test on the next pass".
                    out += [f"{pad}_deadline = {self.now()} + ({ms})",
                            f"{pad}while {self.now()} < _deadline:",
                            f"{pad}    yield"]
                else:
                    out.append(f"{pad}{self.delay(ms)}")
            elif isinstance(node, sp.WaitUntil):
                test = self._expr(node.cond, pins)
                out.append(f"{pad}while not ({test}):")
                out.append(f"{pad}    " + ("yield" if tasks else "sleep(1)"))
            elif isinstance(node, sp.SetVar):
                out.append(f"{pad}{node.name} = {self._expr(node.value, pins)}")
            elif isinstance(node, sp.ChangeVar):
                out.append(f"{pad}{node.name} += {self._expr(node.delta, pins)}")
            elif isinstance(node, sp.Forever):
                out.append(f"{pad}while True:")
                inner = self._stmts(node.body, depth + 1, pins, program,
                                    tasks, [])
                out += inner or [f"{pad}    pass"]
                if tasks:
                    out.append(f"{pad}    yield")      # loop back-edge
            elif isinstance(node, sp.Repeat):
                out.append(f"{pad}for _ in range({self._expr(node.count, pins)}):")
                inner = self._stmts(node.body, depth + 1, pins, program,
                                    tasks, [])
                out += inner or [f"{pad}    pass"]
                if tasks:
                    out.append(f"{pad}    yield")
            elif isinstance(node, sp.Loop):
                test = self._expr(node.cond, pins)
                out.append(f"{pad}while " + (f"not ({test}):" if node.until
                                             else f"{test}:"))
                inner = self._stmts(node.body, depth + 1, pins, program,
                                    tasks, [])
                out += inner or [f"{pad}    pass"]
                if tasks:
                    out.append(f"{pad}    yield")
            elif isinstance(node, sp.If):
                out.append(f"{pad}if {self._expr(node.cond, pins)}:")
                inner = self._stmts(node.body, depth + 1, pins, program,
                                    tasks, [])
                out += inner or [f"{pad}    pass"]
                if node.orelse:
                    out.append(f"{pad}else:")
                    inner = self._stmts(node.orelse, depth + 1, pins, program,
                                        tasks, [])
                    out += inner or [f"{pad}    pass"]
            elif isinstance(node, sp.Call):
                args = ", ".join(self._expr(a, pins) for a in node.args)
                out.append(f"{pad}{program.procedures[node.name.lower()].c_name}"
                           f"({args})")
            elif isinstance(node, sp.Stop):
                out.append(f"{pad}return")
            else:
                raise TypeError(node)
        return out

    def _ms(self, node, pins) -> str:
        if isinstance(node.amount, sp.Num):
            value = node.amount.value
            return str(int(round(value * 1000 if node.unit == "seconds"
                                 else value)))
        inner = self._expr(node.amount, pins, sp.UNARY_LEVEL)
        return inner if node.unit == "ms" else f"({inner}) * 1000"

    def _expr(self, node, pins, parent_level: int = -1) -> str:
        if isinstance(node, sp.Num):
            return str(int(node.value))
        if isinstance(node, sp.Var):
            return node.name
        if isinstance(node, sp.PinRef):
            pin = pins[node.name]
            return (self.read_analog(pin) if pin.direction == "analog"
                    else self.read_pin(pin))
        if isinstance(node, sp.Index):
            return f"{node.table}[{self._expr(node.where, pins)}]"
        if isinstance(node, sp.Unary):
            inner = self._expr(node.operand, pins, sp.UNARY_LEVEL)
            return f"not ({inner})" if node.op == "not" else f"-({inner})"
        if isinstance(node, sp.Binary):
            level = sp.LEVEL[node.op]
            text = (f"{self._expr(node.left, pins, level)} "
                    f"{TO_PYTHON[node.op]} "
                    f"{self._expr(node.right, pins, level + 1)}")
            return f"({text})" if level < parent_level else text
        raise TypeError(node)


# `/` is integer division in the C targets, because Scratch variables are
# 16-bit ints there. Python's `/` would silently start producing floats, so
# the same program would drift apart between targets over a division.
TO_PYTHON = {"or": "or", "and": "and", "=": "==", "!=": "!=",
             "<": "<", ">": ">", "<=": "<=", ">=": ">=",
             "+": "+", "-": "-", "*": "*", "/": "//", "%": "%"}


def _walk_body(body):
    for node in body:
        yield node
        for inner in ("body", "orelse"):
            yield from _walk_body(getattr(node, inner, []))


def _walk(program):
    for block in program.whens:
        yield from _walk_body(block)
    for procedure in program.procedures.values():
        yield from _walk_body(procedure.body)
