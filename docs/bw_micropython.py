"""
bw_micropython — the MicroPython boards: BBC micro:bit and Raspberry Pi Pico.

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


class MicroPythonTarget(sp.Target):
    """Everything two MicroPython boards share, which is nearly all of it.

    The lowering is the whole value here: generators instead of a Duff's
    device, `global` where a script assigns a module variable, tables as
    tuples, the cooperative scheduler. None of that is board-specific.

    What IS board-specific is the vocabulary -- `from microbit import *`
    versus `from machine import Pin`, `running_time()` versus
    `time.ticks_ms()`, a 10-bit ADC versus a 16-bit one -- and that is what
    the hooks below are for. A subclass supplies words; it does not supply
    control flow.
    """

    # Whether an output's level has to be remembered in order to toggle it.
    # True where reading a pin cannot tell you what you last wrote to it.
    tracks_output_level = True

    # ---- the vocabulary a board must supply ------------------------------
    def imports(self, program) -> list[str]:
        raise NotImplementedError

    def board_setup(self, program) -> list[str]:
        """Anything that must happen before the first pin write."""
        return []

    def deadline_set(self, ms: str) -> str:
        """Assign `_deadline`, `ms` milliseconds from now."""
        raise NotImplementedError

    def deadline_pending(self) -> str:
        """True while `_deadline` has not arrived."""
        raise NotImplementedError

    def tone_lines(self, pin, hz: str, pad: str) -> list[str]:
        raise NotImplementedError



    # ---- the shared emitter, in full --------------------------------------
    def shift_helper(self, part) -> list[str]:
        """The 74HC595 bit-banger in MicroPython.

        Same edges in the same order as every other target -- the part cares
        about their sequence and not their timing -- but the shared C version
        is not reusable here, for the same reason the scheduler is not.
        """
        return [
            f"# {part.kind.upper()}: eight outputs for three pins. Data is",
            "# sampled on the rising edge of the shift clock, the latch",
            "# transfers on its own. MSB first.",
            f"def bw_part_{part.name}(value):",
            "    " + self.write_pin(part.clock, False),
            "    " + self.write_pin(part.latch, False),
            "    for _ in range(8):",
            f"        if value & 0x80:",
            "            " + self.write_pin(part.data, True),
            "        else:",
            "            " + self.write_pin(part.data, False),
            "        value = (value << 1) & 0xFF",
            "        " + self.write_pin(part.clock, True),
            "        " + self.write_pin(part.clock, False),
            "    " + self.write_pin(part.latch, True) + "   # transfer to the outputs",
            "    " + self.write_pin(part.latch, False),
            "",
        ]

    # ---- the emitter ----------------------------------------------------
    def emit(self, program) -> str:
        pins = {pin.name: pin for pin in program.pins.values()}
        tasks = len(program.whens) > 1 or any(program.when_hats)
        # Names that live at module scope and are assigned inside functions,
        # so every function that touches them needs a `global`.
        globals_ = list(program.variables)

        out = [
            "# Generated from BrickWright pseudocode by stc-compiler.",
            "# Hand edits will be lost; change the pseudocode instead.",
            "#",
        ]
        out += self.imports(program)
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
        if toggled and self.tracks_output_level:
            out += ["# write_digital has no read-back -- reading a pin would",
                    "# switch it to input mode -- so an output's level is",
                    "# remembered here instead.",
                    "_level = {" + ", ".join(f"{n!r}: 0" for n in toggled) + "}",
                    ""]
        if not self.tracks_output_level:
            toggled = []

        if program.variables:
            out += ["# Variables (Scratch integers).",
                    *(f"{name} = 0" for name in program.variables),
                    ""]

        for part in program.parts.values():
            out += self.shift_helper(part)

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
            hat = program.when_hats[number] if program.when_hats else None

            if hat is not None:
                # An event hat polls its pin and fires on the edge. read_pin
                # already folds ACTIVE LOW in, so `_now` is the LOGICAL level:
                # a button wired to ground reads 1 when it is held down.
                pin_name, edge = hat
                pin = pins[pin_name]
                fired = ("_now and not _prev" if edge == "pressed"
                         else "_prev and not _now")
                out += [f"# WHEN {pin_name} {edge}:", f"def {name}():"]
                # `global` belongs at function level, before anything nests.
                out += self._global_decl(block, program, globals_ + toggled,
                                         "    ")
                out += ["    _prev = 0",
                        "    while True:",
                        f"        _now = 1 if {self.read_pin(pin)} else 0",
                        f"        _fired = {fired}",
                        "        _prev = _now",
                        "        if _fired:"]
                body = self._stmts(block, 3, pins, program, tasks,
                                   globals_ + toggled)
                out += body or ["            pass"]
                # Poll once per scheduler pass. Also what makes this function a
                # generator when the body happens to contain no wait.
                out += ["        yield", ""]
                continue

            out += [f"# WHEN started:"
                    + (f" (script {number + 1})" if tasks else ""),
                    f"def {name}():"]
            body = self._stmts(block, 1, pins, program, tasks,
                               globals_ + toggled)
            out += body or ["    pass"]
            if tasks and not any("yield" in line for line in body):
                # A script with no wait and no loop yields nowhere, so `def`
                # would produce a plain function -- and the scheduler would
                # call next() on the None it returned. One bare yield makes it
                # the generator the scheduler expects.
                out += ["    yield   # runs once; this is what makes it a generator"]
            out.append("")

        out += self.board_setup(program)

        for pin in program.pins.values():
            if pin.direction == "output":
                out.append(self.write_pin(pin, pin.active_low)
                           + f"  # {pin.name} off")
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
            out += self._global_decl(body, program, globals_, pad)

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
                out += self.tone_lines(pins[node.pin],
                                       self._expr(node.hz, pins), pad)
            elif isinstance(node, sp.SetPart):
                part = program.parts[node.part]
                value = self._expr(node.value, pins)
                inner = (f"(~({value})) & 0xFF" if part.active_low
                         else f"({value}) & 0xFF")
                out.append(f"{pad}bw_part_{part.name}({inner})")
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
                    out += [f"{pad}{self.deadline_set(ms)}",
                            f"{pad}while {self.deadline_pending()}:",
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

    def _global_decl(self, body, program, globals_, pad) -> list[str]:
        """`global` for every module-level variable this block assigns.

        Without it MicroPython makes the assignment a local and the variable
        silently stops being shared -- no error, just a script that never sees
        what another one wrote.
        """
        assigned = {node.name for node in _walk_body(body)
                    if isinstance(node, (sp.SetVar, sp.ChangeVar))}
        needed = [g for g in dict.fromkeys(globals_)
                  if g in assigned and g in program.variables]
        return [f"{pad}global " + ", ".join(needed)] if needed else []

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



class MicrobitTarget(MicroPythonTarget):
    """BBC micro:bit V1/V2, emitting MicroPython."""

    key = "microbit"
    display = "BBC micro:bit"
    toolchain = "uflash"
    # Unused -- running_time() and sleep() are the runtime's, so CLOCK means
    # nothing here. It still has to be something the board plausibly runs at,
    # because the pseudocode back end writes it back out and an 8051 crystal
    # on a micro:bit would read as a mistake.
    default_clock = 16000000

    # PORT and PART are not here on purpose. A PORT is eight bits of one
    # register written at once, and a PART is a 74HC595 wired to P0.0-style
    # pins; both are 8051 shapes with no micro:bit equivalent, and `require()`
    # refuses them by name rather than emitting something that looks close.
    # PORT is the only one left out, and it is a real absence rather than a
    # gap: MicroPython has no whole-port write, and eight separate
    # write_digital calls would not land as the one store a PORT promises.
    supports = frozenset({"pwm", "tone", "print", "table", "part"})
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

    # ---- the board's words ----------------------------------------------
    def imports(self, program) -> list[str]:
        out = ["# MicroPython for the BBC micro:bit. Nothing to compile: flash it",
               "# with uflash, or paste it into python.microbit.org.",
               "from microbit import *"]
        if any(p.direction == "tone" for p in program.pins.values()):
            out.append("import music")
        return out

    def board_setup(self, program) -> list[str]:
        used = sorted({pin.number for pin in program.pins.values()
                       if pin.number in DISPLAY_PINS})
        if not used:
            return []
        return ["# P" + ", P".join(str(n) for n in used)
                + " are wired to the 5x5 LED matrix. The display driver",
                "# scans those pins continuously and would fight anything",
                "# else driving them, so it is switched off.",
                "display.off()",
                ""]

    def deadline_set(self, ms: str) -> str:
        # running_time() counts from boot and does not wrap in any run this
        # will see, so plain arithmetic is honest here.
        return f"_deadline = running_time() + ({ms})"

    def deadline_pending(self) -> str:
        return "running_time() < _deadline"

    def tone_lines(self, pin, hz: str, pad: str) -> list[str]:
        # music.pitch plays until stopped when duration is -1 and wait is
        # False. 0 Hz means silence, and since the frequency is an expression
        # the choice has to be made at run time.
        return [f"{pad}_hz = {hz}",
                f"{pad}if _hz:",
                f"{pad}    music.pitch(_hz, -1, {pin.obj}, False)",
                f"{pad}else:",
                f"{pad}    music.stop({pin.obj})"]

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


# ------------------------------------------------------------------- Pico
#
# The RP2040 runs MicroPython too, so it inherits the whole lowering above and
# differs only in vocabulary -- which is exactly the split the base class is
# for. The differences are worth naming, because each one is a way to be
# quietly wrong:
#
#   * pins are OBJECTS. `Pin(25, Pin.OUT)` has to be constructed before use,
#     where a micro:bit's `pin0` simply exists.
#   * the ADC is 16-BIT. read_u16() returns 0-65535 where read_analog()
#     returns 0-1023, so an unscaled port would read 64x high and look like a
#     wiring fault.
#   * ticks_ms() WRAPS. running_time() does not, in any run this will see.
#     Comparing wrapped ticks with `<` fails once every 12 days or so, which
#     is the worst possible failure rate: too rare to catch, too common to
#     ignore. time.ticks_diff exists precisely for this.
#   * there is no tone. A PWM channel at 50% duty is a square wave, which is
#     what a tone is.

# GP0-GP28 exist; GP26/27/28 are ADC0/1/2. GP25 is the on-board LED on a
# classic Pico -- on a Pico W it is on the wireless chip and not a GPIO at
# all, which is why the default is not assumed anywhere.
PICO_ANALOG = {26: 0, 27: 1, 28: 2}
PICO_MAX_GPIO = 28

PICO_PIN_RE = re.compile(r"^(?:gp?(\d{1,2})|(\d{1,2}))$", re.I)


class PicoPin(sp.Pin):
    """A Pico pin. `obj` is the module-level object built for it in setup."""

    def __init__(self, name, where, direction, active_low, obj, number):
        super().__init__(name, where, direction, active_low)
        self.obj = obj
        self.number = number


class PicoTarget(MicroPythonTarget):
    """Raspberry Pi Pico / Pico W (RP2040), emitting MicroPython."""

    # value() reads back the OUTPUT latch on an RP2040, so a toggle can ask
    # the pin rather than a dictionary. Keeping the dictionary as well would
    # be a second source of truth for one bit.
    tracks_output_level = False

    key = "pico"
    display = "Raspberry Pi Pico"
    toolchain = "uf2"
    default_clock = 125000000          # unused; the runtime owns the timebase
    source_extension = "py"
    supports = frozenset({"pwm", "tone", "print", "table", "part"})
    compile_hint = ("MicroPython is interpreted on the device, so there is "
                    "nothing to compile: copy the .py to the board, or write "
                    "it over the REPL.")

    # ---- pins -----------------------------------------------------------
    def resolve_pin(self, program, name, where, direction, active_low, line):
        match = PICO_PIN_RE.match(where)
        if not match:
            raise sp.PseudocodeError(
                line, f"{where.upper()} is not a pin on the {self.display}; "
                      f"use GP0 to GP{PICO_MAX_GPIO}")
        number = int(match.group(1) or match.group(2))
        if number > PICO_MAX_GPIO:
            raise sp.PseudocodeError(
                line, f"the {self.display} has GP0-GP{PICO_MAX_GPIO}, "
                      f"not GP{number}")
        if direction == "analog" and number not in PICO_ANALOG:
            usable = ", ".join(f"GP{n}" for n in sorted(PICO_ANALOG))
            raise sp.PseudocodeError(
                line, f"GP{number} has no ADC on the {self.display}; "
                      f"analog input is {usable}")
        prefix = {"analog": "_adc", "pwm": "_pwm", "tone": "_pwm"}.get(
            direction, "_pin")
        return PicoPin(name, f"GP{number}", direction, active_low,
                       f"{prefix}{number}", number)

    def write_pin(self, pin, high):
        return f"{pin.obj}.value({1 if high else 0})"

    def toggle_pin(self, pin):
        # value() reads back the OUTPUT latch on an RP2040, so unlike the
        # micro:bit there is no need to remember the level separately. The
        # shared emitter still keeps `_level`; using it here would be a second
        # source of truth for the same bit.
        return f"{pin.obj}.value(1 - {pin.obj}.value())"

    def read_pin(self, pin):
        read = f"{pin.obj}.value()"
        return f"(not {read})" if pin.active_low else read

    def read_analog(self, pin):
        # read_u16() is 0-65535. Every other target in this project reports
        # 0-1023, and a program that moves between boards must not change
        # meaning, so it is scaled here rather than in the program.
        return f"({pin.obj}.read_u16() >> 6)"

    def write_pwm(self, pin, value: str) -> str:
        duty = f"(100 - ({value}))" if pin.active_low else f"({value})"
        return f"{pin.obj}.duty_u16({duty} * 65535 // 100)"

    def delay(self, ms):
        return f"time.sleep_ms({ms})"

    def now(self):
        return "time.ticks_ms()"

    # ---- the board's words ----------------------------------------------
    def imports(self, program) -> list[str]:
        wanted = ["Pin"]
        if any(p.direction == "analog" for p in program.pins.values()):
            wanted.append("ADC")
        if any(p.direction in ("pwm", "tone") for p in program.pins.values()):
            wanted.append("PWM")
        return ["# MicroPython for the Raspberry Pi Pico. Nothing to compile:",
                "# copy this to the board as main.py.",
                f"from machine import {', '.join(wanted)}",
                "import time"]

    def board_setup(self, program) -> list[str]:
        out = []
        for pin in program.pins.values():
            if pin.direction == "output":
                out.append(f"{pin.obj} = Pin({pin.number}, Pin.OUT)")
            elif pin.direction == "input":
                # An ACTIVE LOW input is a button to ground, which is what the
                # internal pull-up is for.
                pull = "Pin.PULL_UP" if pin.active_low else "Pin.PULL_DOWN"
                out.append(f"{pin.obj} = Pin({pin.number}, Pin.IN, {pull})")
            elif pin.direction == "analog":
                out.append(f"{pin.obj} = ADC({pin.number})")
            elif pin.direction in ("pwm", "tone"):
                out.append(f"{pin.obj} = PWM(Pin({pin.number}))")
                if pin.direction == "pwm":
                    # A fixed carrier well above anything an LED or a motor
                    # driver cares about; the duty is what the program sets.
                    out.append(f"{pin.obj}.freq(1000)")
        for part in program.parts.values():
            for claimed in part.claimed:
                out.append(f"{claimed.obj} = Pin({claimed.number}, Pin.OUT)"
                           f"  # {part.name}")
        if out:
            out.append("")
        return out

    def deadline_set(self, ms: str) -> str:
        # ticks_add, not `+`: ticks_ms() wraps, and the runtime owns the
        # arithmetic that survives it.
        return f"_deadline = time.ticks_add(time.ticks_ms(), ({ms}))"

    def deadline_pending(self) -> str:
        return "time.ticks_diff(_deadline, time.ticks_ms()) > 0"

    def tone_lines(self, pin, hz: str, pad: str) -> list[str]:
        # A tone is a square wave, and a PWM channel at half duty is one.
        # freq(0) is an error rather than silence, so 0 Hz mutes by duty.
        return [f"{pad}_hz = {hz}",
                f"{pad}if _hz:",
                f"{pad}    {pin.obj}.freq(_hz)",
                f"{pad}    {pin.obj}.duty_u16(32768)",
                f"{pad}else:",
                f"{pad}    {pin.obj}.duty_u16(0)"]
