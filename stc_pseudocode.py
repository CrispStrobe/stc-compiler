"""
stc_pseudocode — BrickWright-style pseudocode ⇄ C for the STC12 / 8051.

Parsing is target-neutral and so is the control-flow lowering; everything that
knows about a chip lives behind `Target` (registers, headers, the timebase,
what a pin location token even means). See that class for where the seam runs
and why it runs there.

The dialect follows the conventions already used by sb3-creator's pseudocode
(`SPRITE Name:` / `WHEN flag clicked:` / `REPEAT n:` / `IF x > y THEN:` /
`set v to n`): UPPERCASE for structure and control flow, lowercase for
statements, indentation for nesting, and `=` comparing rather than assigning.

Parsing builds an **AST**, and both back ends walk it:

    text ──parse──▶ Program (AST) ──emit_c─────────▶ C ──▶ SDCC ──▶ .hex
                          │
                          └────────emit_pseudocode─▶ text

That is the same shape as sb3-creator, where blocks are the IR and
`decompile(project)` walks it back to pseudocode — and it is what makes the
round-trip testable, because `parse` and `emit_pseudocode` have to be inverses.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

PORT_RE = re.compile(r"^P([0-4])\.([0-7])$", re.I)
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class PseudocodeError(Exception):
    """Carries a line number so the caller can point at the offending line."""

    def __init__(self, line: int, message: str):
        self.line = line
        super().__init__(f"line {line}: {message}")


# ============================================================== expression AST

class Expr:
    pass


@dataclass
class Num(Expr):
    value: float

    def text(self) -> str:
        return str(int(self.value)) if float(self.value).is_integer() else str(self.value)


@dataclass
class Var(Expr):
    name: str


@dataclass
class PinRef(Expr):
    name: str


@dataclass
class Unary(Expr):
    op: str            # "not" | "-"
    operand: Expr


@dataclass
class Binary(Expr):
    op: str            # pseudocode spelling: or and = != < > <= >= + - * / %
    left: Expr
    right: Expr


# Lowest binding first. The index doubles as precedence, so the emitters can
# work out where parentheses are genuinely needed instead of adding them
# everywhere -- which matters because the pseudocode back end's output has to
# parse back to the identical tree.
PRECEDENCE = [
    ("or",),
    ("and",),
    ("=", "!=", "<", ">", "<=", ">="),
    ("+", "-"),
    ("*", "/", "%"),
]
LEVEL = {op: index for index, ops in enumerate(PRECEDENCE) for op in ops}
UNARY_LEVEL = len(PRECEDENCE)

TO_C = {"or": "||", "and": "&&", "=": "==", "!=": "!=",
        "<": "<", ">": ">", "<=": "<=", ">=": ">=",
        "+": "+", "-": "-", "*": "*", "/": "/", "%": "%"}
SYNONYM = {"==": "=", "<>": "!="}


# =============================================================== statement AST

class Stmt:
    pass


@dataclass
class SetPin(Stmt):
    pin: str
    high: bool
    # How it was written, so decompiling gives back the same sentence:
    # "turn on/off" reads better for LEDs, "set high/low" for logic levels.
    style: str = "level"        # "level" | "onoff"


@dataclass
class Toggle(Stmt):
    pin: str


@dataclass
class Wait(Stmt):
    amount: Expr
    unit: str                   # "seconds" | "ms"


@dataclass
class WaitUntil(Stmt):
    cond: Expr


@dataclass
class SetVar(Stmt):
    name: str
    value: Expr


@dataclass
class ChangeVar(Stmt):
    name: str
    delta: Expr


@dataclass
class Forever(Stmt):
    body: list


@dataclass
class Repeat(Stmt):
    count: Expr
    body: list


@dataclass
class Loop(Stmt):
    cond: Expr
    body: list
    until: bool = False         # REPEAT UNTIL c  vs  WHILE c


@dataclass
class If(Stmt):
    cond: Expr
    body: list
    orelse: list = field(default_factory=list)


@dataclass
class Call(Stmt):
    name: str
    args: list


@dataclass
class Stop(Stmt):
    pass


# ===================================================================== program

@dataclass
class Pin:
    """A declared pin, in terms every target shares.

    `where` is the token exactly as the target canonicalised it -- "P1.0" on an
    8051, "D13" or "A0" on an Arduino. The AST stores that string and nothing
    about ports, bits or registers: those are the target's business, and a
    target that has no registers at all still has to fit here.
    """
    name: str
    where: str
    direction: str              # "output" | "input" | "analog"
    active_low: bool = False


@dataclass
class Pin8051(Pin):
    """The 8051 view: a port and a bit, which is what the registers are named for."""
    port: int = 0
    bit: int = 0

    @property
    def sfr(self) -> str:
        return f"P{self.port}_{self.bit}"

    @property
    def mask(self) -> int:
        return 1 << self.bit

    @property
    def adc_channel(self) -> int:
        # ADC channel n is on P1.n. True of this family, not of 8051s in
        # general, and certainly not a rule any other target should inherit.
        return self.bit


@dataclass
class Procedure:
    name: str
    params: list
    body: list = field(default_factory=list)

    @property
    def c_name(self) -> str:
        return "bw_" + re.sub(r"\W", "_", self.name)


# ============================================================ target interface

class Target:
    """Everything below the AST: what a pin is, how you drive it, what a
    millisecond costs, and what has to happen before main()'s first statement.

    The control-flow lowering (`stmts_c`, `stmts_task`) is portable and must
    stay that way, so it never formats a register name -- it asks the target
    for a statement or an expression and pastes that in. The seam is drawn
    here rather than one layer down because targets differ at *statement*
    level too, not only in primitives: a target with no `goto` cannot use the
    Duff's-device lowering at all and has to schedule some other way.
    """

    key = ""                    # canonical device token, as written in DEVICE
    display = ""                # how to name it in an error message

    # Which compiler turns this target's output into an image. Transpiling is
    # free; compiling is not, and the hosted service vendors SDCC only. Saying
    # so here lets the caller refuse clearly instead of handing Arduino C++ to
    # `sdcc -mmcs51` and reporting whatever it makes of that.
    toolchain = "sdcc-mcs51"

    # The C type a millisecond count lives in, and its signed counterpart for
    # the scheduler's wraparound-safe deadline compare. 16 bits is right for a
    # Timer-0 counter we increment ourselves; a target whose clock is the
    # core's `millis()` gets a 32-bit one whether it wants it or not, and
    # casting that to a 16-bit int would make every deadline past 32 s wrong.
    time_type = "unsigned int"
    time_signed = "int"

    # ---- pins -----------------------------------------------------------
    def resolve_pin(self, program, name, where, direction, active_low,
                    line: int) -> Pin:
        """Turn the declaration's opaque location token into a Pin, or explain
        why this target cannot offer what was asked for."""
        raise NotImplementedError

    def write_pin(self, pin: Pin, high: bool) -> str:
        raise NotImplementedError

    def toggle_pin(self, pin: Pin) -> str:
        raise NotImplementedError

    def read_pin(self, pin: Pin) -> str:
        raise NotImplementedError

    def read_analog(self, pin: Pin) -> str:
        raise NotImplementedError

    # ---- time -----------------------------------------------------------
    def delay(self, ms: str) -> str:
        """A blocking wait, as a statement. Only the straight-line back end
        uses it; tasks yield instead."""
        raise NotImplementedError

    def now(self) -> str:
        """Milliseconds since boot, as an expression."""
        raise NotImplementedError

    # ---- the shell around the generated statements -----------------------
    def prologue(self, program) -> list[str]:
        raise NotImplementedError

    def runtime(self, program, tasks: bool) -> list[str]:
        """The tick/now/delay machinery and any peripheral helpers. A target
        whose language already provides a timebase returns nothing."""
        raise NotImplementedError

    def setup(self, program) -> list[str]:
        """The first statements of main(): pin directions, peripherals, timer."""
        raise NotImplementedError

    def start_scheduler(self, task_names: list[str]) -> list[str]:
        """Start the timebase and run the cooperative tasks forever."""
        raise NotImplementedError

    def main(self, program, setup_lines: list[str], body_lines: list[str],
             task_names: list[str]) -> list[str]:
        """The whole shell around the generated statements.

        Not merely main()'s braces: a target is free to have no main() at all.
        The Arduino core owns main() and calls setup() and loop() from it, so
        this is two functions there and one here -- a difference the AST
        walker must not have to know about. `task_names` is empty for a
        single-script program, and `body_lines` is empty for a multi-script
        one; exactly one of the two is ever non-empty.
        """
        raise NotImplementedError


class Stc8051Target(Target):
    """The 8051 families, which differ from each other only in three flags.

    An STC12C5A60S2 drops into an STC89C52 socket pin-for-pin, but the 1T core
    runs software delay loops 6-12x too fast. Our generated code never
    busy-waits -- every delay and every scheduler tick is Timer 0 at FOSC/12,
    which both families count identically -- so the same pseudocode is
    timing-correct on either chip.
    """

    def __init__(self, key: str, display: str, header: str,
                 port_modes: bool, aux_1t_bit: bool, adc: bool):
        self.key = key
        self.display = display
        self.header = header        # the SDCC header with this family's registers
        self.port_modes = port_modes  # PxM0/PxM1 exist (STC12); STC89 is quasi-bidi
        self.aux_1t_bit = aux_1t_bit  # AUXR.7 selects T0 1T mode and must be cleared
        self.adc = adc                # 10-bit ADC on P1 (STC12 only)

    # ---- pins -----------------------------------------------------------
    def resolve_pin(self, program, name, where, direction, active_low, line):
        match = PORT_RE.match(where)
        if not match:
            raise PseudocodeError(
                line, f"{where.upper()} is not a pin on the {self.display}; "
                      "use P0.0 to P4.7")
        port, bit = int(match.group(1)), int(match.group(2))
        if direction == "analog":
            if port != 1:
                raise PseudocodeError(
                    line, "ANALOG is only available on P1.0-P1.7 "
                          f"(ADC0-ADC7), not {where.upper()}")
            if not self.adc:
                raise PseudocodeError(
                    line, f"ANALOG pins need an ADC, and the {program.part} has none")
        return Pin8051(name, f"P{port}.{bit}", direction, active_low, port, bit)

    def write_pin(self, pin, high):
        return f"{pin.sfr} = {1 if high else 0};"

    def toggle_pin(self, pin):
        return f"{pin.sfr} = !{pin.sfr};"

    def read_pin(self, pin):
        return f"!{pin.sfr}" if pin.active_low else pin.sfr

    def read_analog(self, pin):
        return f"adc_read({pin.adc_channel})"

    # ---- time -----------------------------------------------------------
    def delay(self, ms):
        return f"delay_ms({ms});"

    def now(self):
        return "bw_now()"

    # ---- the shell ------------------------------------------------------
    def prologue(self, program):
        return [
            f"#include <{self.header}>",
            "",
            f"#define FOSC_HZ {program.clock}UL",
            "",
            "/* Timer 0, mode 1, clocked at FOSC/12 -- accuracy depends only on",
            " * FOSC, and every supported family counts this mode identically, so",
            " * the same program is timing-correct on a 12T STC89 and a 1T STC12",
            " * or STC15. Nothing in the generated code ever busy-waits. */",
            "#define T0_RELOAD (65536UL - (FOSC_HZ / 12UL / 1000UL))",
            "",
        ]

    def runtime(self, program, tasks):
        out = []
        if tasks:
            out += [
                "/* One WHEN block = one cooperative task. Timer 0 interrupts",
                " * every millisecond; tasks yield at every wait and at every",
                " * loop iteration (Scratch's own scheduling contract), so no",
                " * task can starve the others. */",
                "static volatile unsigned int bw_ms;",
                "",
                "void bw_tick(void) __interrupt(1)",
                "{",
                "    TL0 = (unsigned char)(T0_RELOAD & 0xFF);",
                "    TH0 = (unsigned char)(T0_RELOAD >> 8);",
                "    bw_ms++;",
                "}",
                "",
                "/* A 16-bit read is not atomic on an 8051; hold the tick off. */",
                "static unsigned int bw_now(void)",
                "{",
                "    unsigned int t;",
                "    ET0 = 0;",
                "    t = bw_ms;",
                "    ET0 = 1;",
                "    return t;",
                "}",
                "",
            ]
        else:
            out += [
                "static void delay_ms(unsigned int ms)",
                "{",
                "    while (ms--) {",
                "        TL0 = (unsigned char)(T0_RELOAD & 0xFF);",
                "        TH0 = (unsigned char)(T0_RELOAD >> 8);",
                "        TF0 = 0;",
                "        TR0 = 1;",
                "        while (!TF0) ;",
                "        TR0 = 0;",
                "        TF0 = 0;",
                "    }",
                "}",
                "",
            ]
        if program.uses_adc:
            out += [
                "/* 10-bit ADC, polled. Channel n is on P1.n; the channel is selected",
                " * and the conversion started in one write, as STC's examples do. */",
                "static unsigned int adc_read(unsigned char channel)",
                "{",
                "    unsigned char settle;",
                "    ADC_CONTR = (unsigned char)(0xE8 | channel);  /* power|fast|start|chan */",
                "    for (settle = 0; settle < 8; settle++) ;      /* let the mux settle */",
                "    while (!(ADC_CONTR & 0x10)) ;                 /* wait for ADC_FLAG */",
                "    ADC_CONTR &= ~0x10;                           /* clear it by hand */",
                "    return ((unsigned int)ADC_RES << 2) | (ADC_RESL & 0x03);",
                "}",
                "",
            ]
        return out

    def setup(self, program):
        out: list[str] = []
        outputs: dict = {}
        for pin in program.pins.values():
            if pin.direction == "output":
                outputs[pin.port] = outputs.get(pin.port, 0) | pin.mask
        if self.port_modes:
            for port in sorted(outputs):
                mask = outputs[port]
                out += [f"    P{port}M1 &= ~0x{mask:02X};   /* push-pull */",
                        f"    P{port}M0 |=  0x{mask:02X};"]
        # On a quasi-bidirectional-only part (STC89) there is nothing to set up:
        # active-low wiring sinks the LED current either way.
        for pin in program.pins.values():
            if pin.direction == "output":
                out.append(f"    {pin.sfr} = {1 if pin.active_low else 0};"
                           f"   /* {pin.name} off */")

        analog = 0
        for pin in program.pins.values():
            if pin.direction == "analog":
                analog |= pin.mask
        if analog:
            out += ["",
                    f"    P1ASF = 0x{analog:02X};                 /* analog function on P1 */",
                    f"    P1M1 |=  0x{analog:02X};                /* high-impedance input */",
                    f"    P1M0 &= ~0x{analog:02X};",
                    "    ADC_CONTR = 0xE0;              /* ADC on, fastest conversion */"]

        out.append("")
        if self.aux_1t_bit:
            out.append("    AUXR &= ~0x80;                 /* Timer 0 at FOSC/12 */")
        out.append("    TMOD  = (TMOD & 0xF0) | 0x01;  /* Timer 0, mode 1 */")
        return out

    def start_scheduler(self, task_names):
        return ["    TL0 = (unsigned char)(T0_RELOAD & 0xFF);",
                "    TH0 = (unsigned char)(T0_RELOAD >> 8);",
                "    ET0 = 1;                       /* millisecond tick */",
                "    EA  = 1;",
                "    TR0 = 1;",
                "",
                "    for (;;) {",
                *(f"        {name}();" for name in task_names),
                "    }"]

    def main(self, program, setup_lines, body_lines, task_names):
        out = ["void main(void)", "{"] + setup_lines
        if task_names:
            out += self.start_scheduler(task_names)
        else:
            out.append("")
            out += body_lines
        return out + ["}", ""]


ARDUINO_PIN_RE = re.compile(r"^(?:d(\d{1,2})|a(\d{1,2})|(\d{1,2}))$", re.I)


@dataclass
class ArduinoPin(Pin):
    """The Arduino view: whatever expression the core's functions accept.

    A digital pin is its bare number; an analog one is the `A0` macro, which
    is also a perfectly good argument to digitalWrite. So one string covers
    both, and nothing here needs to know about a port or a register.
    """
    ref: str = ""


class ArduinoTarget(Target):
    """Boards programmed through the Arduino core, emitted as core C++.

    This target writes almost no runtime of its own, and that is the point.
    The scheduler contract the 8051 back end had to build by hand -- a
    millisecond tick that never busy-waits, so cooperative tasks can share the
    processor -- is what `millis()` already is. So `runtime()` is empty, and
    the generated code is the AST lowering and nothing else.

    The debt is at the other end: `millis()` is 32-bit, so the deadline
    statics and the wraparound compare have to widen with it (see
    `time_type`). Truncating it to 16 bits would look right and would break
    every wait longer than 32 seconds.
    """

    # millis() is `unsigned long`, and the deadline arithmetic must match it.
    time_type = "unsigned long"
    time_signed = "long"

    # Core C++ needs the Arduino build system; SDCC cannot touch it.
    toolchain = "arduino-cli"

    def __init__(self, key: str, display: str, digital_max: int, analog_max: int):
        self.key = key
        self.display = display
        self.digital_max = digital_max
        self.analog_max = analog_max

    # ---- pins -----------------------------------------------------------
    def resolve_pin(self, program, name, where, direction, active_low, line):
        match = ARDUINO_PIN_RE.match(where)
        if not match:
            raise PseudocodeError(
                line, f"{where.upper()} is not a pin on the {self.display}; "
                      f"use D0-D{self.digital_max} or A0-A{self.analog_max}")
        digital, analog, bare = match.groups()

        if analog is not None:
            number = int(analog)
            if number > self.analog_max:
                raise PseudocodeError(
                    line, f"the {self.display} has A0-A{self.analog_max}, "
                          f"not A{number}")
            # An analog pin is still a perfectly good digital one, so this
            # deliberately does not check the direction.
            return ArduinoPin(name, f"A{number}", direction, active_low,
                              f"A{number}")

        number = int(digital if digital is not None else bare)
        if number > self.digital_max:
            raise PseudocodeError(
                line, f"the {self.display} has D0-D{self.digital_max}, "
                      f"not D{number}")
        if direction == "analog":
            raise PseudocodeError(
                line, f"ANALOG needs an analog input, and D{number} is "
                      f"digital-only on the {self.display}; "
                      f"use A0-A{self.analog_max}")
        return ArduinoPin(name, f"D{number}", direction, active_low, str(number))

    def write_pin(self, pin, high):
        return f"digitalWrite({pin.ref}, {'HIGH' if high else 'LOW'});"

    def toggle_pin(self, pin):
        return f"digitalWrite({pin.ref}, !digitalRead({pin.ref}));"

    def read_pin(self, pin):
        read = f"digitalRead({pin.ref})"
        return f"!{read}" if pin.active_low else read

    def read_analog(self, pin):
        return f"analogRead({pin.ref})"

    # ---- time -----------------------------------------------------------
    def delay(self, ms):
        return f"delay({ms});"

    def now(self):
        return "millis()"

    # ---- the shell ------------------------------------------------------
    def prologue(self, program):
        return [
            "#include <Arduino.h>",
            "",
            "/* No clock constant here on purpose: millis() and delay() are",
            " * already correct for whatever the board is actually clocked at,",
            " * so a CLOCK line in the pseudocode is carried for the other",
            " * targets and deliberately ignored on this one. */",
            "",
        ]

    def runtime(self, program, tasks):
        # Nothing to emit. The timebase, the blocking delay and the ADC are
        # all in the core already -- which is the whole reason this target is
        # cheap, and the reason it is a good check on the interface: a target
        # that needs no runtime at all still has to fit through it.
        return []

    def setup(self, program):
        out: list[str] = []
        for pin in program.pins.values():
            if pin.direction == "output":
                out.append(f"    pinMode({pin.ref}, OUTPUT);")
            elif pin.direction == "input":
                # An ACTIVE LOW input is a button wired to ground, which is
                # exactly what the internal pull-up is for. An active-high one
                # needs its own external pull-down, and enabling the pull-up
                # would fight it.
                mode = "INPUT_PULLUP" if pin.active_low else "INPUT"
                out.append(f"    pinMode({pin.ref}, {mode});")
            # An analog pin needs no pinMode: analogRead configures the mux.
        for pin in program.pins.values():
            if pin.direction == "output":
                level = "HIGH" if pin.active_low else "LOW"
                out.append(f"    digitalWrite({pin.ref}, {level});"
                           f"   /* {pin.name} off */")
        return out

    def start_scheduler(self, task_names):
        # loop() *is* the forever loop; wrapping another one inside it would
        # starve the core's own housekeeping (serialEventRun between calls).
        return [f"    {name}();" for name in task_names]

    def main(self, program, setup_lines, body_lines, task_names):
        out = ["void setup()", "{"] + setup_lines
        if body_lines:
            # A single script runs once, so it belongs in setup(). Scratch
            # semantics: the script is not restarted when it finishes.
            out.append("")
            out += body_lines
        out += ["}", "", "void loop()", "{"]
        out += (self.start_scheduler(task_names) if task_names else
                ["    /* the script ran once, in setup(); nothing repeats here */"])
        return out + ["}", ""]


def _stc(key, display, header, port_modes, aux_1t_bit, adc):
    return Stc8051Target(key, display, header, port_modes, aux_1t_bit, adc)


TARGETS = {
    "stc12c5a60s2": _stc("stc12c5a60s2", "STC12C5A60S2", "stc12.h", True, True, True),
    "stc12c5a16s2": _stc("stc12c5a16s2", "STC12C5A16S2", "stc12.h", True, True, True),
    "stc89c52rc": _stc("stc89c52rc", "STC89C52RC", "8052.h", False, False, False),
    "stc89c52": _stc("stc89c52", "STC89C52", "8052.h", False, False, False),
    # The STC15 borrows stc12.h deliberately: every register the EMITTER
    # touches (P0-P3, PxM0/PxM1, AUXR, Timer 0, P1ASF, the ADC block) sits at
    # the same address on the STC15F2K60S2. The famous divergences (Timer 2
    # at 0xD6/0xD7, S3CON...) are registers this generator never writes.
    # Keil TRANSLATION of arbitrary STC15 code is a different problem with
    # its own family shim.
    "stc15f2k60s2": _stc("stc15f2k60s2", "STC15F2K60S2", "stc12.h", True, True, True),

    # Both are ATmega328P boards and differ here only in how many analog pins
    # the package brings out: the Uno's header stops at A5, the Nano carries
    # A6 and A7 as well (input-only, which this generator never violates
    # because ANALOG is read-only by construction).
    "arduino-uno": ArduinoTarget("arduino-uno", "Arduino Uno", 13, 5),
    "arduino-nano": ArduinoTarget("arduino-nano", "Arduino Nano", 13, 7),
}


@dataclass
class Program:
    part: str = "stc12c5a60s2"
    clock: int = 11059200
    pins: dict = field(default_factory=dict)
    variables: list = field(default_factory=list)
    procedures: dict = field(default_factory=dict)
    # One entry per `WHEN started:` block. `body` mirrors whens[0] so older
    # call sites keep working; with several blocks the C back end emits a
    # cooperative scheduler instead of straight-line code.
    whens: list = field(default_factory=list)
    body: list = field(default_factory=list)
    locals_: set = field(default_factory=set)

    @property
    def uses_adc(self) -> bool:
        return any(pin.direction == "analog" for pin in self.pins.values())

    @property
    def target(self) -> Target:
        return TARGETS[self.part]


# ====================================================================== lexing

@dataclass
class Line:
    number: int
    indent: int
    text: str


def read_lines(source: str) -> list[Line]:
    out = []
    for number, raw in enumerate(source.splitlines(), 1):
        # `#` and `//` both comment, so neither Python nor C habits surprise you.
        stripped = re.sub(r"\s*(?:#|//).*$", "", raw)
        if not stripped.strip():
            continue
        lead = stripped[: len(stripped) - len(stripped.lstrip())]
        if "\t" in lead:
            raise PseudocodeError(number, "tabs in indentation; use spaces")
        out.append(Line(number, len(lead), stripped.strip()))
    return out


TOKEN_RE = re.compile(r"""\s*(?:
      (?P<number>\d+\.\d+|\d+)
    | (?P<op><=|>=|!=|<>|==|[-+*/%()<>=])
    | (?P<word>[A-Za-z_][A-Za-z0-9_]*)
    )""", re.X)


def tokenize(text: str, line: int) -> list[str]:
    tokens, pos = [], 0
    while pos < len(text):
        match = TOKEN_RE.match(text, pos)
        if not match or match.end() == pos:
            if text[pos:].strip():
                raise PseudocodeError(line, f"cannot parse {text[pos:].strip()!r}")
            break
        tokens.append(match.group().strip())
        pos = match.end()
    return [token for token in tokens if token]


# ================================================================= expressions

class ExprParser:
    def __init__(self, tokens, program: Program, line: int):
        self.tokens, self.pos, self.program, self.line = tokens, 0, program, line

    def peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def take(self):
        token = self.peek()
        self.pos += 1
        return token

    def parse(self, level: int = 0) -> Expr:
        if level >= len(PRECEDENCE):
            return self.atom()
        node = self.parse(level + 1)
        while True:
            token = self.peek()
            if token is None:
                return node
            op = SYNONYM.get(token, token.lower())
            if op not in PRECEDENCE[level]:
                return node
            self.take()
            node = Binary(op, node, self.parse(level + 1))

    def atom(self) -> Expr:
        token = self.take()
        if token is None:
            raise PseudocodeError(self.line, "expression ended early")
        if token == "(":
            inner = self.parse()
            if self.take() != ")":
                raise PseudocodeError(self.line, "missing ')'")
            return inner
        if token == "-":
            return Unary("-", self.atom())
        if token.lower() == "not":
            return Unary("not", self.atom())
        if re.fullmatch(r"\d+\.\d+|\d+", token):
            return Num(float(token))
        lowered = token.lower()
        if lowered in self.program.pins:
            return PinRef(lowered)
        if lowered in ("true", "on", "high"):
            return Num(1)
        if lowered in ("false", "off", "low"):
            return Num(0)
        if NAME_RE.match(token):
            if token not in self.program.locals_ and token not in self.program.variables:
                self.program.variables.append(token)
            return Var(token)
        raise PseudocodeError(self.line, f"unexpected {token!r}")


def expression(text: str, program: Program, line: int) -> Expr:
    parser = ExprParser(tokenize(text, line), program, line)
    node = parser.parse()
    if parser.peek() is not None:
        raise PseudocodeError(line, f"trailing {parser.peek()!r} in expression")
    return node


# ================================================================== statements

def parse_block(lines: list[Line], index: int, parent_indent: int,
                program: Program) -> tuple[list, int]:
    """Parse the block nested under `parent_indent` into a list of Stmt.

    The block's own indent is whatever its first line uses, so 2 spaces, 4
    spaces or any other width work -- only consistency within one block matters.
    """
    body: list = []
    if index >= len(lines) or lines[index].indent <= parent_indent:
        return body, index
    indent = lines[index].indent

    while index < len(lines):
        line = lines[index]
        if line.indent <= parent_indent:
            break
        if line.indent != indent:
            raise PseudocodeError(
                line.number,
                f"inconsistent indentation: expected {indent} spaces, got {line.indent}")
        text, lowered = line.text, line.text.lower()

        if lowered in ("forever:", "forever"):
            inner, index = parse_block(lines, index + 1, indent, program)
            body.append(Forever(inner))
            continue

        loop = re.fullmatch(r"(while|repeat\s+until)\s+(.+?)\s*:", lowered)
        if loop:
            keyword = loop.group(1)
            raw = text[len(keyword):].strip().rstrip(":").strip()
            cond = expression(raw, program, line.number)
            inner, index = parse_block(lines, index + 1, indent, program)
            body.append(Loop(cond, inner, until=keyword.startswith("repeat")))
            continue

        repeat = re.fullmatch(r"repeat\s+(?!until\b)(.+?)\s*:", lowered)
        if repeat:
            count = expression(text[len("repeat"):].rstrip(":").strip(),
                               program, line.number)
            inner, index = parse_block(lines, index + 1, indent, program)
            body.append(Repeat(count, inner))
            continue

        conditional = re.fullmatch(r"if\s+(.+?)\s+then\s*:", lowered)
        if conditional:
            raw = text[len("if"):].strip()
            raw = raw[: raw.lower().rindex("then")].strip()
            cond = expression(raw, program, line.number)
            inner, index = parse_block(lines, index + 1, indent, program)
            node = If(cond, inner)
            if (index < len(lines) and lines[index].indent == indent
                    and lines[index].text.lower() in ("else:", "else")):
                node.orelse, index = parse_block(lines, index + 1, indent, program)
            body.append(node)
            continue

        if lowered in ("else:", "else"):
            raise PseudocodeError(line.number, "ELSE without a matching IF")

        body.append(simple_statement(text, program, line.number))
        index += 1
    return body, index


def simple_statement(text: str, program: Program, line: int) -> Stmt:
    lowered = text.lower()

    def output_pin(name: str) -> str:
        pin = program.pins.get(name.lower())
        if pin is None:
            raise PseudocodeError(line, f"unknown pin {name!r}; declare it with PIN")
        if pin.direction != "output":
            raise PseudocodeError(
                line, f"{name!r} is an {pin.direction.upper()} and cannot be driven")
        return pin.name

    until = re.match(r"wait\s+until\s+(.+)$", text, re.I)
    if until:
        return WaitUntil(expression(until.group(1), program, line))

    wait = re.fullmatch(r"wait\s+(.+?)\s*(seconds?|secs?|s|ms|milliseconds?)", lowered)
    if wait:
        unit = "ms" if wait.group(2).startswith("m") else "seconds"
        return Wait(expression(wait.group(1), program, line), unit)

    turn = re.fullmatch(r"turn\s+(on|off)\s+(\w+)", lowered)
    if turn:
        pin = program.pins[output_pin(turn.group(2)).lower()]
        on = turn.group(1) == "on"
        return SetPin(pin.name, high=(not on) if pin.active_low else on, style="onoff")

    level = re.fullmatch(r"set\s+(\w+)\s+(high|low)", lowered)
    if level:
        return SetPin(output_pin(level.group(1)), high=(level.group(2) == "high"))

    assign = re.match(r"set\s+([A-Za-z_]\w*)\s+to\s+(.+)$", text, re.I)
    if assign:
        name = assign.group(1)
        if name.lower() in program.pins:
            raise PseudocodeError(line, f"{name!r} is a pin; use 'set {name} high/low'")
        value = expression(assign.group(2), program, line)
        if name not in program.variables and name not in program.locals_:
            program.variables.append(name)
        return SetVar(name, value)

    change = re.match(r"change\s+([A-Za-z_]\w*)\s+by\s+(.+)$", text, re.I)
    if change:
        name = change.group(1)
        delta = expression(change.group(2), program, line)
        if name not in program.variables and name not in program.locals_:
            program.variables.append(name)
        return ChangeVar(name, delta)

    toggle = re.fullmatch(r"toggle\s+(\w+)", lowered)
    if toggle:
        return Toggle(output_pin(toggle.group(1)))

    if lowered in ("stop", "stop all", "halt"):
        return Stop()

    call = re.match(r"([A-Za-z_]\w*)\s*(?:\((.*)\)|\s+(.*))?$", text)
    if call and call.group(1).lower() in program.procedures:
        procedure = program.procedures[call.group(1).lower()]
        raw_args = (call.group(2) or call.group(3) or "").strip()
        args = [expression(a, program, line) for a in split_arguments(raw_args)]
        if len(args) != len(procedure.params):
            raise PseudocodeError(
                line, f"{procedure.name!r} takes {len(procedure.params)} "
                      f"argument(s), got {len(args)}")
        return Call(procedure.name, args)

    raise PseudocodeError(line, f"do not understand {text!r}")


def split_arguments(text: str) -> list[str]:
    """Split on commas that are not inside parentheses."""
    if not text:
        return []
    parts, depth, current = [], 0, ""
    for char in text:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += char
    parts.append(current)
    return [part.strip() for part in parts if part.strip()]


# ===================================================================== parsing

DEFINE_RE = re.compile(r"define\s+(?:fast\s+)?([A-Za-z_]\w*)\s*(.*?):\s*$", re.I)
WHEN_RE = re.compile(r"when\s+(started|flag\s+clicked|powered\s+on)\s*:", re.I)
PIN_RE = re.compile(r"pin\s+(\w+)\s*=\s*(\S+)\s+(output|input|analog)"
                    r"(?:\s+active\s+(low|high))?", re.I)
CLOCK_RE = re.compile(r"clock\s+([\d_]+)\s*(hz|mhz)?", re.I)


def parse(source: str) -> Program:
    lines = read_lines(source)
    if not lines:
        raise PseudocodeError(1, "empty program")

    program = Program()
    index = 0

    device = re.fullmatch(r"device\s+([\w-]+)\s*:", lines[0].text, re.I)
    if device:
        program.part = device.group(1).lower()
        if program.part not in TARGETS:
            raise PseudocodeError(
                lines[0].number,
                f"unknown device {device.group(1)!r}; known: "
                + ", ".join(sorted(TARGETS)))
        index = 1

    # Pass one: register every DEFINE header, so a procedure may be called
    # before its definition appears -- the order people actually write in.
    for line in lines:
        header = DEFINE_RE.fullmatch(line.text)
        if header:
            name = header.group(1)
            params = re.findall(r"\(\s*([A-Za-z_]\w*)\s*\)", header.group(2))
            if name.lower() in program.procedures:
                raise PseudocodeError(line.number, f"procedure {name!r} defined twice")
            program.procedures[name.lower()] = Procedure(name, params)

    started = False
    while index < len(lines):
        line = lines[index]
        text, lowered = line.text, line.text.lower()

        clock = CLOCK_RE.fullmatch(lowered)
        if clock and not started:
            value = int(clock.group(1).replace("_", ""))
            program.clock = value * 1_000_000 if clock.group(2) == "mhz" else value
            index += 1
            continue

        pin = PIN_RE.fullmatch(lowered)
        if pin and not started:
            name, where, direction, active = pin.groups()
            if name in program.pins:
                raise PseudocodeError(line.number, f"pin {name!r} declared twice")
            # The target decides what that location token means, and whether
            # it can offer the requested direction there at all.
            program.pins[name] = program.target.resolve_pin(
                program, name, where, direction, active == "low", line.number)
            index += 1
            continue

        header = DEFINE_RE.fullmatch(text)
        if header:
            procedure = program.procedures[header.group(1).lower()]
            program.locals_ = set(procedure.params)
            procedure.body, index = parse_block(lines, index + 1, line.indent, program)
            program.locals_ = set()
            if not procedure.body:
                raise PseudocodeError(line.number,
                                      f"procedure {procedure.name!r} has an empty body")
            continue

        if WHEN_RE.fullmatch(lowered):
            started = True
            block, index = parse_block(lines, index + 1, line.indent, program)
            if not block:
                raise PseudocodeError(line.number, "'WHEN started:' block is empty")
            program.whens.append(block)
            continue

        raise PseudocodeError(
            line.number, f"do not understand {text!r}"
            + ("" if started else " (expected CLOCK, PIN, DEFINE or WHEN started:)"))

    if not started:
        raise PseudocodeError(lines[-1].number, "no 'WHEN started:' block")
    program.body = program.whens[0]

    if len(program.whens) > 1:
        # Each WHEN block becomes a cooperative task; a wait inside a
        # procedure would have to suspend the CALLER's state machine, which
        # single-level state machines cannot express. Scratch has the same
        # shape (scripts yield, custom blocks run to completion).
        def waits(body):
            for node in body:
                if isinstance(node, (Wait, WaitUntil)):
                    return True
                for inner in ("body", "orelse"):
                    if waits(getattr(node, inner, [])):
                        return True
            return False
        for procedure in program.procedures.values():
            if waits(procedure.body):
                raise PseudocodeError(
                    lines[0].number,
                    f"with several WHEN blocks, procedures must not wait -- "
                    f"{procedure.name!r} does; move the wait into the WHEN block")
    return program


# ========================================================= pseudocode back end

def expr_pseudo(node: Expr, parent_level: int = -1) -> str:
    """Render an expression, parenthesising only where precedence demands it."""
    if isinstance(node, Num):
        return node.text()
    if isinstance(node, (Var, PinRef)):
        return node.name
    if isinstance(node, Unary):
        inner = expr_pseudo(node.operand, UNARY_LEVEL)
        return f"not {inner}" if node.op == "not" else f"-{inner}"
    if isinstance(node, Binary):
        level = LEVEL[node.op]
        # The right operand binds one level tighter so that `a - (b - c)` keeps
        # its parentheses; without that, re-parsing would give `(a - b) - c`.
        text = (f"{expr_pseudo(node.left, level)} {node.op} "
                f"{expr_pseudo(node.right, level + 1)}")
        return f"({text})" if level < parent_level else text
    raise TypeError(node)


def stmts_pseudo(body: list, depth: int, active_low: dict) -> list[str]:
    pad = "  " * depth
    out = []
    for node in body:
        if isinstance(node, SetPin):
            if node.style == "onoff":
                on = (not node.high) if active_low.get(node.pin) else node.high
                out.append(f"{pad}turn {'on' if on else 'off'} {node.pin}")
            else:
                out.append(f"{pad}set {node.pin} {'high' if node.high else 'low'}")
        elif isinstance(node, Toggle):
            out.append(f"{pad}toggle {node.pin}")
        elif isinstance(node, Wait):
            out.append(f"{pad}wait {expr_pseudo(node.amount)} {node.unit}")
        elif isinstance(node, WaitUntil):
            out.append(f"{pad}wait until {expr_pseudo(node.cond)}")
        elif isinstance(node, SetVar):
            out.append(f"{pad}set {node.name} to {expr_pseudo(node.value)}")
        elif isinstance(node, ChangeVar):
            out.append(f"{pad}change {node.name} by {expr_pseudo(node.delta)}")
        elif isinstance(node, Forever):
            out.append(f"{pad}FOREVER:")
            out += stmts_pseudo(node.body, depth + 1, active_low)
        elif isinstance(node, Repeat):
            out.append(f"{pad}REPEAT {expr_pseudo(node.count)}:")
            out += stmts_pseudo(node.body, depth + 1, active_low)
        elif isinstance(node, Loop):
            keyword = "REPEAT UNTIL" if node.until else "WHILE"
            out.append(f"{pad}{keyword} {expr_pseudo(node.cond)}:")
            out += stmts_pseudo(node.body, depth + 1, active_low)
        elif isinstance(node, If):
            out.append(f"{pad}IF {expr_pseudo(node.cond)} THEN:")
            out += stmts_pseudo(node.body, depth + 1, active_low)
            if node.orelse:
                out.append(f"{pad}ELSE:")
                out += stmts_pseudo(node.orelse, depth + 1, active_low)
        elif isinstance(node, Call):
            args = ", ".join(expr_pseudo(a) for a in node.args)
            out.append(f"{pad}{node.name}{' ' + args if args else ''}")
        elif isinstance(node, Stop):
            out.append(f"{pad}stop")
        else:
            raise TypeError(node)
    return out


def emit_pseudocode(program: Program) -> str:
    """Walk the AST back to canonical pseudocode.

    Canonical rather than byte-identical to whatever was parsed: comments are
    gone, DEVICE and CLOCK are always written out, and the layout is
    normalised. What matters is that it is a *fixed point* -- parsing this and
    emitting again gives exactly the same text.
    """
    active_low = {pin.name: pin.active_low for pin in program.pins.values()}
    out = [f"DEVICE {program.part.upper()}:", f"  CLOCK {program.clock}"]
    if program.pins:
        out.append("")
        for pin in program.pins.values():
            polarity = " ACTIVE LOW" if pin.active_low else ""
            out.append(f"  PIN {pin.name} = {pin.where} "
                       f"{pin.direction.upper()}{polarity}")
    for procedure in program.procedures.values():
        out.append("")
        params = "".join(f" ({name})" for name in procedure.params)
        out.append(f"  DEFINE {procedure.name}{params}:")
        out += stmts_pseudo(procedure.body, 2, active_low)
    for block in program.whens:
        out += ["", "  WHEN started:"]
        out += stmts_pseudo(block, 2, active_low)
    return "\n".join(out) + "\n"


# ================================================================== C back end

@dataclass
class Emit:
    """What every walker in the C back end needs, gathered so that adding a
    target does not mean threading another parameter through six functions."""
    target: Target
    pins: dict
    procs: dict
    counter: list = field(default_factory=lambda: [0])


def expr_c(node: Expr, ctx: Emit, parent_level: int = -1) -> str:
    if isinstance(node, Num):
        return str(int(node.value))
    if isinstance(node, Var):
        return node.name
    if isinstance(node, PinRef):
        pin = ctx.pins[node.name]
        if pin.direction == "analog":
            return ctx.target.read_analog(pin)
        return ctx.target.read_pin(pin)
    if isinstance(node, Unary):
        inner = expr_c(node.operand, ctx, UNARY_LEVEL)
        return f"!({inner})" if node.op == "not" else f"-({inner})"
    if isinstance(node, Binary):
        level = LEVEL[node.op]
        text = (f"{expr_c(node.left, ctx, level)} {TO_C[node.op]} "
                f"{expr_c(node.right, ctx, level + 1)}")
        return f"({text})" if level < parent_level else text
    raise TypeError(node)


def ms_of(node: Wait, ctx: Emit) -> str:
    """A Wait in milliseconds, folded to a constant where it can be."""
    if isinstance(node.amount, Num):
        value = node.amount.value
        return str(int(round(value * 1000 if node.unit == "seconds" else value)))
    inner = expr_c(node.amount, ctx, UNARY_LEVEL)
    return inner if node.unit == "ms" else f"(unsigned int)(({inner}) * 1000)"


def stmts_c(body: list, depth: int, ctx: Emit) -> list[str]:
    pad = "    " * depth
    out = []
    for node in body:
        if isinstance(node, SetPin):
            out.append(pad + ctx.target.write_pin(ctx.pins[node.pin], node.high))
        elif isinstance(node, Toggle):
            out.append(pad + ctx.target.toggle_pin(ctx.pins[node.pin]))
        elif isinstance(node, Wait):
            out.append(pad + ctx.target.delay(ms_of(node, ctx)))
        elif isinstance(node, WaitUntil):
            out.append(f"{pad}while (!({expr_c(node.cond, ctx)})) ;")
        elif isinstance(node, SetVar):
            out.append(f"{pad}{node.name} = {expr_c(node.value, ctx)};")
        elif isinstance(node, ChangeVar):
            out.append(f"{pad}{node.name} += {expr_c(node.delta, ctx)};")
        elif isinstance(node, Forever):
            out.append(f"{pad}for (;;) {{")
            out += stmts_c(node.body, depth + 1, ctx)
            out.append(f"{pad}}}")
        elif isinstance(node, Repeat):
            ctx.counter[0] += 1
            var = f"_i{ctx.counter[0]}"
            out.append(f"{pad}{{ unsigned int {var};")
            out.append(f"{pad}  for ({var} = 0; {var} < "
                       f"({expr_c(node.count, ctx)}); {var}++) {{")
            out += stmts_c(node.body, depth + 2, ctx)
            out += [f"{pad}  }}", f"{pad}}}"]
        elif isinstance(node, Loop):
            test = expr_c(node.cond, ctx, UNARY_LEVEL if node.until else -1)
            if node.until:
                test = f"!({test})"      # REPEAT UNTIL c  ==  WHILE not c
            out.append(f"{pad}while ({test}) {{")
            out += stmts_c(node.body, depth + 1, ctx)
            out.append(f"{pad}}}")
        elif isinstance(node, If):
            out.append(f"{pad}if ({expr_c(node.cond, ctx)}) {{")
            out += stmts_c(node.body, depth + 1, ctx)
            if node.orelse:
                out.append(f"{pad}}} else {{")
                out += stmts_c(node.orelse, depth + 1, ctx)
            out.append(f"{pad}}}")
        elif isinstance(node, Call):
            args = ", ".join(expr_c(a, ctx) for a in node.args)
            out.append(f"{pad}{ctx.procs[node.name.lower()].c_name}({args});")
        elif isinstance(node, Stop):
            out.append(f"{pad}for (;;) ;   /* stop */")
        else:
            raise TypeError(node)
    return out


def has_wait(body: list) -> bool:
    for node in body:
        if isinstance(node, Wait):
            return True
        if has_wait(getattr(node, "body", [])) or has_wait(getattr(node, "orelse", [])):
            return True
    return False


def stmts_task(body: list, depth: int, ctx: Emit,
               task: str, states: list, statics: list) -> list[str]:
    """One task's statements as the interior of a Duff's-device state machine.

    The switch sits in the caller; case labels land inside whatever nesting
    the statements build, which C allows as long as no inner switch appears
    (we emit none). Every wait AND every loop back-edge is a numbered yield
    -- the latter is Scratch's own scheduling contract, and it is what makes
    a busy FOREVER loop unable to starve the other tasks.

    This lowering is what a target without `goto` -- MicroPython, say -- cannot
    use, and that is the reason the seam runs through statements and not only
    through primitives."""
    pad = "    " * depth
    out = []

    def yield_state():
        states[0] += 1
        return states[0]

    for node in body:
        if isinstance(node, SetPin):
            out.append(pad + ctx.target.write_pin(ctx.pins[node.pin], node.high))
        elif isinstance(node, Toggle):
            out.append(pad + ctx.target.toggle_pin(ctx.pins[node.pin]))
        elif isinstance(node, Wait):
            state = yield_state()
            out += [f"{pad}{task}_until = {ctx.target.now()} + ({ms_of(node, ctx)});",
                    f"{pad}{task}_state = {state};",
                    f"{pad}case {state}:",
                    f"{pad}if (({ctx.target.time_signed})"
                    f"({ctx.target.now()} - {task}_until) < 0) return;"]
        elif isinstance(node, WaitUntil):
            state = yield_state()
            out += [f"{pad}{task}_state = {state};",
                    f"{pad}case {state}:",
                    f"{pad}if (!({expr_c(node.cond, ctx)})) return;"]
        elif isinstance(node, SetVar):
            out.append(f"{pad}{node.name} = {expr_c(node.value, ctx)};")
        elif isinstance(node, ChangeVar):
            out.append(f"{pad}{node.name} += {expr_c(node.delta, ctx)};")
        elif isinstance(node, Forever):
            state = yield_state()
            out += [f"{pad}{task}_state = {state};", f"{pad}case {state}:"]
            out += stmts_task(node.body, depth, ctx, task, states, statics)
            out += [f"{pad}{task}_state = {state};", f"{pad}return;"]
        elif isinstance(node, Repeat):
            ctx.counter[0] += 1
            var = f"bw_i{ctx.counter[0]}"
            statics.append(var)
            state = yield_state()
            out += [f"{pad}{var} = ({expr_c(node.count, ctx)});",
                    f"{pad}{task}_state = {state};",
                    f"{pad}case {state}:",
                    f"{pad}if ({var}) {{"]
            out += stmts_task(node.body, depth + 1, ctx, task, states, statics)
            out += [f"{pad}    {var}--;",
                    f"{pad}    {task}_state = {state};",
                    f"{pad}    return;",
                    f"{pad}}}"]
        elif isinstance(node, Loop):
            test = expr_c(node.cond, ctx, UNARY_LEVEL if node.until else -1)
            if node.until:
                test = f"!({test})"
            state = yield_state()
            out += [f"{pad}{task}_state = {state};",
                    f"{pad}case {state}:",
                    f"{pad}if ({test}) {{"]
            out += stmts_task(node.body, depth + 1, ctx, task, states, statics)
            out += [f"{pad}    {task}_state = {state};",
                    f"{pad}    return;",
                    f"{pad}}}"]
        elif isinstance(node, If):
            out.append(f"{pad}if ({expr_c(node.cond, ctx)}) {{")
            out += stmts_task(node.body, depth + 1, ctx, task, states, statics)
            if node.orelse:
                out.append(f"{pad}}} else {{")
                out += stmts_task(node.orelse, depth + 1, ctx, task, states, statics)
            out.append(f"{pad}}}")
        elif isinstance(node, Call):
            args = ", ".join(expr_c(a, ctx) for a in node.args)
            out.append(f"{pad}{ctx.procs[node.name.lower()].c_name}({args});")
        elif isinstance(node, Stop):
            out += [f"{pad}{task}_state = 0xFFFF;   /* stop this script */",
                    f"{pad}return;"]
        else:
            raise TypeError(node)
    return out


def emit_c(program: Program) -> str:
    """Walk the AST to C, asking the target for everything chip-specific.

    What stays here is the portable part: declaration order, the shape of the
    scheduler, which statements become which control flow. What the target
    supplies is every line that names a register, a header or a timebase."""
    target = program.target
    ctx = Emit(target, {pin.name: pin for pin in program.pins.values()},
               program.procedures)
    tasks = len(program.whens) > 1

    out = [
        "/* Generated from BrickWright pseudocode by stc-compiler.",
        " * Hand edits will be lost; change the pseudocode instead. */",
    ]
    out += target.prologue(program)
    out += target.runtime(program, tasks)

    if program.variables:
        out.append("/* Variables (16-bit signed, like Scratch's integers). */")
        out += [f"static int {name} = 0;" for name in program.variables]
        out.append("")

    if ctx.procs:
        for procedure in ctx.procs.values():
            params = ", ".join(f"int {p}" for p in procedure.params) or "void"
            out.append(f"static void {procedure.c_name}({params});")
        out.append("")
        for procedure in ctx.procs.values():
            params = ", ".join(f"int {p}" for p in procedure.params) or "void"
            out += [f"/* DEFINE {procedure.name} */",
                    f"static void {procedure.c_name}({params})", "{",
                    *stmts_c(procedure.body, 1, ctx), "}", ""]

    task_names: list[str] = []
    if tasks:
        task_lines: list[str] = []
        statics: list[str] = []
        for number, block in enumerate(program.whens):
            task = f"bw_task{number}"
            task_names.append(task)
            states = [0]
            body = stmts_task(block, 1, ctx, task, states, statics)
            head = [f"static unsigned int {task}_state;"]
            if has_wait(block):
                head.append(f"static {target.time_type} {task}_until;")
            task_lines += head
            task_lines += [f"/* WHEN started: (script {number + 1}) */",
                           f"static void {task}(void)", "{",
                           f"    switch ({task}_state) {{",
                           "    case 0:",
                           *body,
                           "    }",
                           f"    {task}_state = 0xFFFF;   /* ran to the end */",
                           "}", ""]
        if statics:
            task_lines[0:0] = ["/* REPEAT counters live across yields. */",
                               *(f"static unsigned int {name};" for name in statics),
                               ""]
        out += task_lines

    # Exactly one of these is non-empty; the target decides what shell they go
    # in, because "the program starts here" is not `main()` everywhere.
    body_lines = [] if tasks else stmts_c(program.body, 1, ctx)
    out += target.main(program, target.setup(program), body_lines, task_names)
    return "\n".join(out)


# ======================================================================= facade

def transpile(source: str) -> tuple[str, Program]:
    program = parse(source)
    return emit_c(program), program


def decompile(source: str) -> str:
    """pseudocode -> AST -> canonical pseudocode. A fixed point by construction."""
    return emit_pseudocode(parse(source))


EXAMPLE = """DEVICE STC12C5A60S2:
  CLOCK 11059200

  PIN led1 = P1.0 OUTPUT ACTIVE LOW
  PIN led2 = P1.1 OUTPUT ACTIVE LOW

  WHEN started:
    set counter to 0
    FOREVER:
      REPEAT 6:
        turn on led1
        turn off led2
        wait 0.15 seconds
        turn off led1
        turn on led2
        wait 0.15 seconds
      change counter by 1
      IF counter > 2 THEN:
        turn on led1
        turn on led2
        wait 1 seconds
        turn off led1
        turn off led2
        wait 1 seconds
        set counter to 0
"""
