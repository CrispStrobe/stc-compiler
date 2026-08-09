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

# Which PCA module each pin carries, on the STC12C5A60S2 with AUXR1.PCA_P4
# clear. Setting that bit moves them to P4.2/P4.3, and other STC12 variants
# differ again -- so this is a per-target fact, not an 8051 one.
PCA_PINS = {(1, 3): 0, (1, 4): 1}

BW_BAUD = 9600          # the console rate; not settable from the dialect yet

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
class PortRef(Expr):
    port: str


@dataclass
class Index(Expr):
    """table[expr] -- a read from a constant lookup table in code space.

    A seven-segment font, an LED-matrix frame and a note table are all this
    shape, and none of them is expressible without it. The table is const and
    lives in flash, which is the abundant resource here; RAM is not.
    """
    table: str
    where: Expr


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
class SetPart(Stmt):
    part: str
    value: Expr


@dataclass
class SetPort(Stmt):
    port: str
    value: Expr


@dataclass
class Print(Stmt):
    """Write a line to the serial console.

    Either a literal or a number, never both and never concatenated: string
    building on a part with 256 bytes of RAM is a bigger feature than it looks,
    and two `print`s cost nothing.
    """
    text: str = ""              # a literal, when value is None
    value: Expr = None          # a number, when text is ""


@dataclass
class SetTone(Stmt):
    """Play a frequency on a tone pin, or silence it with 0.

    Not PWM: a tone needs a settable PERIOD, and every PWM path on this chip
    has a fixed carrier -- see STC12-PERIPHERAL-MODEL.md 5b for why the
    obvious PCA route gives 3.9 Hz. This is Timer 1 toggling the pin.
    """
    pin: str
    hz: Expr


@dataclass
class SetPwm(Stmt):
    """Set a PWM pin's duty, as a percentage of time the load is ON.

    The AST stores what was written -- brightness -- not the compare value.
    Polarity and the hardware's inverted comparator are the target's problem,
    so decompiling gives back the same sentence on an active-low pin as on an
    active-high one.
    """
    pin: str
    value: Expr


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
    direction: str              # "output" | "input" | "analog" | "pwm" | "tone"
    active_low: bool = False


@dataclass
class Port:
    """A whole 8-bit port, written at once.

    Not sugar for eight pins. A seven-segment digit or an LED-matrix column has
    to land as ONE store, or the display shows the intermediate states -- which
    is visible as ghosting rather than as a bug report.
    """
    name: str
    port: int
    direction: str              # "output" | "input"
    active_low: bool = False

    @property
    def sfr(self) -> str:
        return f"P{self.port}"


@dataclass
class ShiftPart:
    """A 74HC595: eight outputs for three pins.

    Modelled as a PORT that costs three pins instead of eight, so it takes the
    same `set ... to ...` and the same polarity. A user who has run out of pins
    should not have to learn a second vocabulary to say the same thing.

    Admitted to the parts library because its correctness depends on the ORDER
    of edges and not their duration -- the part is specified into the tens of
    megahertz and has no minimum clock period an 8051 could violate. See
    docs/PARTS-MODEL.md.
    """
    name: str
    kind: str                   # "74hc595"
    data: tuple                 # (port, bit)
    clock: tuple
    latch: tuple
    active_low: bool = False

    def sfr(self, which) -> str:
        port, bit = getattr(self, which)
        return f"P{port}_{bit}"

    @property
    def claimed(self) -> list:
        return [self.data, self.clock, self.latch]


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

    @property
    def pca_module(self) -> int:
        # CCP0/PWM0 is P1.3 and CCP1/PWM1 is P1.4 on the STC12C5A60S2.
        # Other STC12 variants put them elsewhere (the STC12C5201AD uses
        # P3.7/P3.5), so this belongs to the target, not to "8051".
        return PCA_PINS[(self.port, self.bit)]


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


class Stc8051Target(Target):
    """The 8051 families, which differ from each other only in three flags.

    An STC12C5A60S2 drops into an STC89C52 socket pin-for-pin, but the 1T core
    runs software delay loops 6-12x too fast. Our generated code never
    busy-waits -- every delay and every scheduler tick is Timer 0 at FOSC/12,
    which both families count identically -- so the same pseudocode is
    timing-correct on either chip.
    """

    def __init__(self, key: str, display: str, header: str,
                 port_modes: bool, aux_1t_bit: bool, adc: bool, pwm: bool = False):
        self.key = key
        self.display = display
        self.header = header        # the SDCC header with this family's registers
        self.port_modes = port_modes  # PxM0/PxM1 exist (STC12); STC89 is quasi-bidi
        self.aux_1t_bit = aux_1t_bit  # AUXR.7 selects T0 1T mode and must be cleared
        self.adc = adc                # 10-bit ADC on P1 (STC12 only)
        self.pwm = pwm                # PCA capture/compare modules with PWM mode
        # Where UART1's baud rate comes from. The STC12 has a dedicated
        # baud-rate timer; the STC89 has to spend Timer 1 on it, which is the
        # same Timer 1 a TONE pin wants -- so on that family the two features
        # are mutually exclusive, and saying so is better than a silent
        # fight over TMOD.
        self.baud_from_brt = port_modes

    # ---- pins -----------------------------------------------------------
    def resolve_pin(self, program, name, where, direction, active_low, line):
        match = PORT_RE.match(where)
        if not match:
            raise PseudocodeError(
                line, f"{where.upper()} is not a pin on the {self.display}; "
                      "use P0.0 to P4.7")
        port, bit = int(match.group(1)), int(match.group(2))

        # Two names for one physical pin is always a mistake, and nothing
        # downstream would notice: program.pins is keyed by name, so both
        # declarations survive and quietly fight over the same register.
        for other in program.pins.values():
            if getattr(other, "port", None) == port and getattr(other, "bit", None) == bit:
                raise PseudocodeError(
                    line, f"P{port}.{bit} is already declared as {other.name!r} "
                          f"({other.direction.upper()}); one pin cannot be two things")
        # And the other way round: a PORT writes all eight bits at once, so a
        # PIN inside it would be clobbered by every port write.
        for prev in program.parts.values():
            if (port, bit) in prev.claimed:
                raise PseudocodeError(
                    line, f"P{port}.{bit} is claimed by the part {prev.name!r}")
        for whole in program.ports.values():
            if whole.port == port:
                raise PseudocodeError(
                    line, f"P{port} is already declared as the whole port {whole.name!r}; "
                          f"a PORT write covers all eight bits and would clobber "
                          f"P{port}.{bit}")

        if direction == "tone":
            # Any GPIO will do -- software owns the toggle -- but there is only
            # one Timer 1, so there is only one tone.
            for other in program.pins.values():
                if other.direction == "tone":
                    raise PseudocodeError(
                        line, f"only one TONE pin is possible ({other.name!r} already "
                              f"has it): the tone is Timer 1, and there is one of those")
        if direction == "pwm":
            if not self.pwm:
                raise PseudocodeError(
                    line, f"PWM needs the PCA, and the {program.part} has none")
            if (port, bit) not in PCA_PINS:
                pins = ", ".join(f"P{p}.{b} (module {m})"
                                 for (p, b), m in sorted(PCA_PINS.items()))
                raise PseudocodeError(
                    line, f"PWM is only available on the PCA pins: {pins}. "
                          f"{where.upper()} has no PCA module. Note those pins are "
                          f"also ADC channels, so a pin cannot do both.")
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

    def write_print(self, node) -> str:
        if node.value is None:
            return f'bw_print("{node.text}");'
        return f"bw_print_num({node.value});"

    def write_tone(self, pin, hz: str) -> str:
        return f"tone_set({hz});"

    def write_pwm(self, pin, value: str) -> str:
        """Duty, as the percentage of time the LOAD is on.

        pwm_set() takes the percentage of time the PIN is HIGH, so an
        active-low load -- which is every LED in this toolchain, because a
        quasi-bidirectional pin sinks 20 mA and sources 230 uA -- inverts
        here. Emitted as a visible `100 - x` rather than folded away, so the
        generated C still reads like the pseudocode did.
        """
        if pin.active_low:
            return f"pwm_set({pin.pca_module}, 100 - ({value}));"
        return f"pwm_set({pin.pca_module}, {value});"

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
        for part in program.parts.values():
            out += [
                f"/* {part.name}: a 74HC595, eight outputs for three pins.",
                " *",
                " * Admitted to the parts library because its correctness depends on the",
                " * ORDER of the edges and not on their duration: the part is specified",
                " * into the tens of megahertz and has no minimum clock period an 8051",
                " * could violate, so there is no delay here to get wrong. Data is sampled",
                " * on the rising edge of the shift clock, and the latch transfers on its",
                " * own rising edge. docs/PARTS-MODEL.md.",
                " *",
                " * MSB first, so the byte reads left to right on the outputs. */",
                f"static void bw_part_{part.name}(unsigned char value)",
                "{",
                "    unsigned char i;",
                f"    {part.sfr('clock')} = 0;",
                f"    {part.sfr('latch')} = 0;",
                "    for (i = 0; i < 8; i++) {",
                f"        {part.sfr('data')} = (value & 0x80) ? 1 : 0;",
                "        value = (unsigned char)(value << 1);",
                f"        {part.sfr('clock')} = 1;",
                f"        {part.sfr('clock')} = 0;",
                "    }",
                f"    {part.sfr('latch')} = 1;      /* transfer to the outputs */",
                f"    {part.sfr('latch')} = 0;",
                "}",
                "",
            ]
        if program.tables:
            out += [
                "/* Lookup tables live in code space: flash is the abundant resource",
                " * here and RAM is not. `const __code` keeps them out of the 256 bytes",
                " * that matter. */",
            ]
            for name, values in program.tables.items():
                body = ", ".join(f"0x{v:02X}" for v in values)
                out += [f"static const __code unsigned char bw_tab_{name}[] "
                        f"= {{ {body} }};"]
            out += [
                "",
                "/* A computed index is clamped rather than trusted. Reading past a",
                " * table means reading a random byte of flash and, on a display,",
                " * showing it -- which looks like data rather than like a fault. A",
                " * constant index is checked at compile time and costs nothing. */",
                "static unsigned char bw_clamp(int i, unsigned char last)",
                "{",
                "    if (i < 0) return 0;",
                "    if (i > (int)last) return last;",
                "    return (unsigned char)i;",
                "}",
                "",
            ]
        if program.uses_uart:
            out += [
                "/* Serial console on UART1, 8N1 at " + str(BW_BAUD) + " baud.",
                " *",
                " * P3.0/P3.1 are also the ISP pins, so you cannot hold a terminal open",
                " * while flashing -- and the debug monitor owns this same UART, which is",
                " * why a program that prints cannot currently run under it.",
                " *",
                " * Blocking on TI is deliberate. A ring buffer would need RAM this part",
                " * does not have to spare, and a dropped diagnostic is worse than a slow",
                " * one: at " + str(BW_BAUD) + " baud a character costs about "
                + f"{1000000 * 10 // BW_BAUD} us. */",
                "static void bw_putc(char c)",
                "{",
                "    SBUF = c;",
                "    while (!TI)",
                "        ;",
                "    TI = 0;",
                "}",
                "",
                "static void bw_print(const char *s)",
                "{",
                "    while (*s)",
                "        bw_putc(*s++);",
                "    bw_putc('\\r');",
                "    bw_putc('\\n');",
                "}",
                "",
                "static void bw_print_num(int v)",
                "{",
                "    unsigned char digits[6];",
                "    unsigned char n = 0;",
                "    unsigned int u;",
                "    if (v < 0) { bw_putc('-'); u = (unsigned int)(-v); }",
                "    else       { u = (unsigned int)v; }",
                "    do { digits[n++] = (unsigned char)('0' + u % 10); u /= 10; } while (u);",
                "    while (n)",
                "        bw_putc((char)digits[--n]);",
                "    bw_putc('\\r');",
                "    bw_putc('\\n');",
                "}",
                "",
            ]
        tone = program.tone_pin
        if tone is not None:
            idle = 1 if tone.active_low else 0
            out += [
                "/* Tone on " + tone.name + ". Timer 1 in mode 1 toggles the pin, so the",
                " * frequency is FOSC/24/(65536 - reload) and the whole audible band is",
                " * reachable -- roughly 7 Hz upward. The hardware clock outputs (T1CLKO",
                " * and friends) divide an 8-BIT reload and bottom out at 1800 Hz, which",
                " * is a beeper rather than a tone; and clocking the PCA from Timer 0 gives",
                " * 3.9 Hz, because Timer 0 is already the millisecond tick. See",
                " * STC12-PERIPHERAL-MODEL.md 5b.",
                " *",
                " * This costs Timer 1 outright. The debug monitor wants it too, as the",
                " * wall clock behind skew_ms, so a program with a tone cannot also be run",
                " * under the monitor. */",
                "#define BW_TONE_NUM (FOSC_HZ / 24UL)",
                "",
                "static unsigned char bw_tone_h, bw_tone_l;",
                "",
                "static void bw_tone_isr(void) __interrupt(3)",
                "{",
                "    TH1 = bw_tone_h;               /* mode 1 is not auto-reload */",
                "    TL1 = bw_tone_l;",
                f"    {tone.sfr} = !{tone.sfr};",
                "}",
                "",
                "static void tone_set(unsigned long hz)",
                "{",
                "    unsigned long div;",
                "    if (hz == 0) {                 /* silence, and park the pin */",
                "        TR1 = 0;",
                "        ET1 = 0;",
                f"        {tone.sfr} = {idle};",
                "        return;",
                "    }",
                "    div = (BW_TONE_NUM + (hz >> 1)) / hz;   /* round, do not truncate: */",
                "                                       /* truncating puts 1000 Hz at */",
                "                                       /* 1001.7 rather than 999.6   */",
                "    if (div < 1UL)     div = 1UL;",
                "    if (div > 65535UL) div = 65535UL;",
                "    div = 65536UL - div;",
                "    bw_tone_h = (unsigned char)(div >> 8);",
                "    bw_tone_l = (unsigned char)(div & 0xFF);",
                "    TH1 = bw_tone_h;",
                "    TL1 = bw_tone_l;",
                "    ET1 = 1;",
                "    TR1 = 1;",
                "}",
                "",
            ]
        if program.uses_pwm:
            out += [
                "/* PCA PWM. The comparator is 9 bits, {EPCnH,CCAPnH} against (0,CL),",
                " * and it drives the pin LOW while CL is BELOW the compare value -- so a",
                " * LARGER value is a LONGER low time and the duty as a fraction HIGH is",
                " * (256 - value)/256. Getting that backwards inverts every brightness and",
                " * looks entirely plausible doing it.",
                " *",
                " * Writing CCAPnH rather than CCAPnL is deliberate: the hardware reloads",
                " * CCAPnH into CCAPnL when CL wraps, so an update cannot glitch mid-period.",
                " * The 9th bit (EPCnH) is what expresses 0% and 100%, which an 8-bit",
                " * compare cannot. Datasheet 10.3.4. */",
                "static void pwm_set(unsigned char module, unsigned int percent_high)",
                "{",
                "    unsigned int v;",
                "    if (percent_high > 100) percent_high = 100;",
                "    v = 256 - ((percent_high * 256 + 50) / 100);",
                "    if (module == 0) {",
                "        CCAP0H = (unsigned char)v;",
                "        if (v > 255) PCA_PWM0 |= 0x02; else PCA_PWM0 &= (unsigned char)~0x02;",
                "    } else {",
                "        CCAP1H = (unsigned char)v;",
                "        if (v > 255) PCA_PWM1 |= 0x02; else PCA_PWM1 &= (unsigned char)~0x02;",
                "    }",
                "}",
                "",
            ]
        return out

    def setup(self, program):
        out: list[str] = []
        outputs: dict = {}
        for pin in program.pins.values():
            # PWM pins are outputs as far as the port mode goes; the PCA
            # drives the level, but the pin still has to be able to drive.
            if pin.direction in ("output", "pwm"):
                outputs[pin.port] = outputs.get(pin.port, 0) | pin.mask
        for port in program.ports.values():
            if port.direction == "output":
                outputs[port.port] = outputs.get(port.port, 0) | 0xFF
        for part in program.parts.values():
            for where in part.claimed:
                outputs[where[0]] = outputs.get(where[0], 0) | (1 << where[1])
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

        if program.uses_uart:
            out += ["", "    SCON = 0x50;                   /* UART mode 1, 8-bit, RX on */"]
            if self.baud_from_brt:
                div = program.clock // (32 * BW_BAUD)
                out += [f"    BRT  = {256 - div};"
                        f"                     /* {BW_BAUD} baud from the BRT */",
                        "    AUXR |= 0x15;                  /* BRTR, BRTx12, S1BRS */"]
            else:
                div = program.clock // (12 * 32 * BW_BAUD)
                out += [f"    TH1  = TL1 = {256 - div};"
                        f"               /* {BW_BAUD} baud from Timer 1 */",
                        "    TMOD = (TMOD & 0x0F) | 0x20;   /* Timer 1, mode 2 */",
                        "    TR1  = 1;"]
            out.append("    TI = 0;  RI = 0;")

        pwm_pins = [p for p in program.pins.values() if p.direction == "pwm"]
        if pwm_pins:
            out += ["",
                    "    CCON = 0x00;                   /* PCA off while configuring */",
                    "    CL = 0;  CH = 0;",
                    "    CMOD = 0x00;                   /* CPS=000: PCA clock = FOSC/12 */"]
            for pin in sorted(pwm_pins, key=lambda p: p.pca_module):
                out.append(f"    CCAPM{pin.pca_module} = 0x42;"
                           f"                /* ECOM|PWM: {pin.name} */")
            # Start at "off", which is 100% high on an active-low load.
            for pin in sorted(pwm_pins, key=lambda p: p.pca_module):
                out.append("    " + self.write_pwm(pin, "0")
                           + f"   /* {pin.name} off */")
            out.append("    CCON = 0x40;                   /* CR: run the PCA counter */")

        out.append("")
        if self.aux_1t_bit:
            if program.tone_pin is not None:
                out.append("    AUXR &= ~0xC0;                 /* Timer 0 AND Timer 1 at FOSC/12 */")
            else:
                out.append("    AUXR &= ~0x80;                 /* Timer 0 at FOSC/12 */")
        out.append("    TMOD  = (TMOD & 0xF0) | 0x01;  /* Timer 0, mode 1 */")
        if program.tone_pin is not None:
            out += ["    TMOD  = (TMOD & 0x0F) | 0x10;  /* Timer 1, mode 1: the tone */",
                    "    PT1   = 1;                     /* the tone outranks the tick:",
                    "                                    * jitter here is audible */",
                    "    tone_set(0);                   /* silent until asked */"]
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


def _stc(key, display, header, port_modes, aux_1t_bit, adc, pwm=False):
    return Stc8051Target(key, display, header, port_modes, aux_1t_bit, adc, pwm)


TARGETS = {
    "stc12c5a60s2": _stc("stc12c5a60s2", "STC12C5A60S2", "stc12.h", True, True, True, True),
    "stc12c5a16s2": _stc("stc12c5a16s2", "STC12C5A16S2", "stc12.h", True, True, True, True),
    "stc89c52rc": _stc("stc89c52rc", "STC89C52RC", "8052.h", False, False, False),
    "stc89c52": _stc("stc89c52", "STC89C52", "8052.h", False, False, False),
    # The STC15 borrows stc12.h deliberately: every register the EMITTER
    # touches (P0-P3, PxM0/PxM1, AUXR, Timer 0, P1ASF, the ADC block) sits at
    # the same address on the STC15F2K60S2. The famous divergences (Timer 2
    # at 0xD6/0xD7, S3CON...) are registers this generator never writes.
    # Keil TRANSLATION of arbitrary STC15 code is a different problem with
    # its own family shim.
    "stc15f2k60s2": _stc("stc15f2k60s2", "STC15F2K60S2", "stc12.h", True, True, True, True),
}


@dataclass
class Program:
    part: str = "stc12c5a60s2"
    clock: int = 11059200
    pins: dict = field(default_factory=dict)
    variables: list = field(default_factory=list)
    procedures: dict = field(default_factory=dict)
    parts: dict = field(default_factory=dict)    # name -> ShiftPart
    tables: dict = field(default_factory=dict)   # name -> list[int], in flash
    ports: dict = field(default_factory=dict)    # name -> Port, whole-byte I/O
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
    def uses_pwm(self) -> bool:
        return any(pin.direction == "pwm" for pin in self.pins.values())

    @property
    def uses_uart(self) -> bool:
        def walk(body):
            for node in body:
                if isinstance(node, Print):
                    return True
                for attr in ("body", "then", "otherwise"):
                    inner = getattr(node, attr, None)
                    if inner and walk(inner):
                        return True
            return False
        # whens are plain statement lists; procedures carry theirs on .body.
        return (walk(self.body)
                or any(walk(w) for w in self.whens)
                or any(walk(getattr(pr, "body", [])) for pr in self.procedures.values()))

    @property
    def tone_pin(self):
        for pin in self.pins.values():
            if pin.direction == "tone":
                return pin
        return None

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
      (?P<number>0[xX][0-9A-Fa-f]+|0[bB][01]+|\d+\.\d+|\d+)
    | (?P<op><=|>=|!=|<>|==|[-+*/%()<>=\[\]])
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
        if re.fullmatch(r"0[xX][0-9A-Fa-f]+", token):
            return Num(float(int(token, 16)))
        if re.fullmatch(r"0[bB][01]+", token):
            # A seven-segment font is written in binary or it is written wrong.
            return Num(float(int(token[2:], 2)))
        if re.fullmatch(r"\d+\.\d+|\d+", token):
            return Num(float(token))
        lowered = token.lower()
        if lowered in self.program.pins:
            return PinRef(lowered)
        if lowered in ("true", "on", "high"):
            return Num(1)
        if lowered in ("false", "off", "low"):
            return Num(0)
        if lowered in self.program.ports:
            return PortRef(lowered)
        if lowered in self.program.tables:
            if self.take() != "[":
                raise PseudocodeError(
                    self.line, f"{token!r} is a TABLE; read it as {token}[<index>]")
            where = self.parse()
            if self.take() != "]":
                raise PseudocodeError(self.line, f"missing ']' after {token}[")
            return Index(lowered, where)
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
        if pin.direction == "tone":
            return pin.name          # "turn off <tone>" is silence; see below
        if pin.direction != "output":
            what = pin.direction.upper()
            article = "an" if what[0] in "AEIOU" else "a"
            raise PseudocodeError(
                line, f"{name!r} is {article} {what} pin and cannot be driven"
                      + (f"; use 'set {name} to <n> percent'" if what == "PWM" else ""))
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
        if pin.direction == "tone":
            if turn.group(1) == "on":
                raise PseudocodeError(
                    line, f"{pin.name!r} is a TONE pin and has no 'on'; "
                          f"use 'set {pin.name} to <n> hz'")
            return SetTone(pin.name, Num(0))
        on = turn.group(1) == "on"
        return SetPin(pin.name, high=(not on) if pin.active_low else on, style="onoff")

    level = re.fullmatch(r"set\s+(\w+)\s+(high|low)", lowered)
    if level:
        return SetPin(output_pin(level.group(1)), high=(level.group(2) == "high"))

    into = re.match(r"set\s+(\w+)\s+to\s+(.+)$", text.strip(), re.I)
    if into and into.group(1).lower() in program.parts:
        return SetPart(into.group(1).lower(),
                       expression(into.group(2), program, line))
    if into and into.group(1).lower() in program.ports:
        port = program.ports[into.group(1).lower()]
        if port.direction != "output":
            raise PseudocodeError(
                line, f"{port.name!r} is an INPUT port and cannot be written")
        return SetPort(port.name, expression(into.group(2), program, line))

    say = re.match(r'print\s+"([^"]*)"\s*$', text.strip(), re.I)
    if say:
        return Print(text=say.group(1))
    say = re.match(r"print\s+(.+)$", text.strip(), re.I)
    if say:
        return Print(value=expression(say.group(1), program, line))

    hertz = re.fullmatch(r"set\s+(\w+)\s+to\s+(.+?)\s*(?:hz|hertz)", text.strip(), re.I)
    if hertz:
        name = hertz.group(1)
        pin = program.pins.get(name.lower())
        if pin is None:
            raise PseudocodeError(line, f"unknown pin {name!r}; declare it with PIN")
        if pin.direction != "tone":
            raise PseudocodeError(
                line, f"{name!r} is a {pin.direction.upper()} pin; only a TONE pin "
                      f"takes a frequency")
        value = expression(hertz.group(2), program, line)
        if isinstance(value, Num):
            hz = value.value
            # 8 Hz to 460 kHz at 11.0592 MHz. Out of range would be CLAMPED at
            # run time, which sounds like a plausible wrong note rather than
            # like an error -- so catch the constant case here.
            lo, hi = program.clock / 24 / 65535, program.clock / 24
            if hz != 0 and not (lo <= hz <= hi):
                raise PseudocodeError(
                    line, f"{hz:g} Hz is outside what Timer 1 can make at "
                          f"CLOCK {program.clock} ({lo:.0f} Hz to {hi:.0f} Hz); "
                          f"it would be clamped and sound like the wrong note")
        return SetTone(pin.name, value)

    duty = re.fullmatch(r"set\s+(\w+)\s+to\s+(.+?)\s*(?:percent|%)", text.strip(), re.I)
    if duty:
        name = duty.group(1)
        pin = program.pins.get(name.lower())
        if pin is None:
            raise PseudocodeError(line, f"unknown pin {name!r}; declare it with PIN")
        if pin.direction != "pwm":
            what = pin.direction.upper()
            raise PseudocodeError(
                line, f"{name!r} is {'an' if what[0] in 'AEIOU' else 'a'} {what} pin; "
                      f"only a PWM pin takes a percentage")
        return SetPwm(pin.name, expression(duty.group(2), program, line))

    assign = re.match(r"set\s+([A-Za-z_]\w*)\s+to\s+(.+)$", text, re.I)
    if assign:
        name = assign.group(1)
        if name.lower() in program.pins:
            direction = program.pins[name.lower()].direction
            how = ("set {0} to <n> percent" if direction == "pwm"
                   else "set {0} high/low").format(name)
            raise PseudocodeError(line, f"{name!r} is a pin; use '{how}'")
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
PIN_RE = re.compile(r"pin\s+(\w+)\s*=\s*(\S+)\s+(output|input|analog|pwm|tone)"
                    r"(?:\s+active\s+(low|high))?", re.I)
PART_RE = re.compile(r"part\s+(\w+)\s*=\s*74hc595\s+data\s+P([0-4])\.([0-7])\s+"
                     r"clock\s+P([0-4])\.([0-7])\s+latch\s+P([0-4])\.([0-7])"
                     r"(?:\s+active\s+(low|high))?", re.I)
PORT_DECL_RE = re.compile(r"port\s+(\w+)\s*=\s*P([0-4])\s+(output|input)"
                          r"(?:\s+active\s+(low|high))?", re.I)
TABLE_RE = re.compile(r"table\s+(\w+)\s*=\s*(.+)$", re.I)
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

        part = PART_RE.fullmatch(lowered)
        if part and not started:
            (name, dp, db, cp, cb, lp, lb, active) = part.groups()
            if name in program.parts or name in program.ports or name in program.pins:
                raise PseudocodeError(line.number, f"{name!r} declared twice")
            claims = [(int(dp), int(db)), (int(cp), int(cb)), (int(lp), int(lb))]
            if len(set(claims)) != 3:
                raise PseudocodeError(
                    line.number, f"{name!r} names the same pin twice; data, clock and "
                                 f"latch must be three different pins")
            for where in claims:
                for other in program.pins.values():
                    if (getattr(other, "port", None), getattr(other, "bit", None)) == where:
                        raise PseudocodeError(
                            line.number, f"P{where[0]}.{where[1]} is already declared as "
                                         f"{other.name!r}; a PART claims its pins")
                for whole in program.ports.values():
                    if whole.port == where[0]:
                        raise PseudocodeError(
                            line.number, f"P{where[0]}.{where[1]} is inside the whole port "
                                         f"{whole.name!r}, which would clobber it")
                for prev in program.parts.values():
                    if where in prev.claimed:
                        raise PseudocodeError(
                            line.number, f"P{where[0]}.{where[1]} is already claimed by "
                                         f"{prev.name!r}")
            program.parts[name] = ShiftPart(name, "74hc595", claims[0], claims[1],
                                            claims[2], active == "low")
            index += 1
            continue

        port = PORT_DECL_RE.fullmatch(lowered)
        if port and not started:
            name, number, direction, active = port.groups()
            number = int(number)
            if name in program.ports or name in program.pins:
                raise PseudocodeError(line.number, f"{name!r} declared twice")
            # A PORT and a PIN on the same port would fight: writing the byte
            # clobbers the bit, and neither declaration would look wrong.
            for other in program.pins.values():
                if getattr(other, "port", None) == number:
                    raise PseudocodeError(
                        line.number,
                        f"P{number} is already used one bit at a time, by {other.name!r} "
                        f"({other.where}); a PORT writes all eight at once and would "
                        f"clobber it")
            for other in program.ports.values():
                if other.port == number:
                    raise PseudocodeError(
                        line.number, f"P{number} is already declared as {other.name!r}")
            program.ports[name] = Port(name, number, direction, active == "low")
            index += 1
            continue

        table = TABLE_RE.fullmatch(text.strip())
        if table and not started:
            name = table.group(1)
            if name.lower() in program.tables:
                raise PseudocodeError(line.number, f"table {name!r} declared twice")
            values = []
            for item in table.group(2).split(","):
                item = item.strip()
                try:
                    values.append(int(item, 0) if not item.startswith(("0b", "0B"))
                                  else int(item[2:], 2))
                except ValueError:
                    raise PseudocodeError(
                        line.number, f"{item!r} is not a constant; a TABLE holds "
                                     f"numbers only, and lives in flash")
            if not values:
                raise PseudocodeError(line.number, f"table {name!r} is empty")
            if any(v < 0 or v > 255 for v in values):
                raise PseudocodeError(
                    line.number, f"table {name!r} holds bytes: 0 to 255")
            program.tables[name.lower()] = values
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
    # One Timer 1, two claimants. On a part whose baud rate comes from Timer 1
    # -- the STC89 -- a serial console and a TONE pin both want it, and they
    # want it in different MODES, so the loser is decided by whichever line of
    # setup runs last. Refuse instead. The STC12 is fine: its baud comes from
    # the dedicated BRT, leaving Timer 1 for the tone.
    if (program.uses_uart and program.tone_pin is not None
            and not program.target.baud_from_brt):
        raise PseudocodeError(
            0, f"on the {program.part} the serial console and a TONE pin both need "
               f"Timer 1, in different modes -- {program.tone_pin.name!r} cannot sound "
               f"while the program prints. The STC12 has a dedicated baud-rate timer "
               f"and can do both.")

    return program


# ========================================================= pseudocode back end

def expr_pseudo(node: Expr, parent_level: int = -1) -> str:
    """Render an expression, parenthesising only where precedence demands it."""
    if isinstance(node, Num):
        return node.text()
    if isinstance(node, PortRef):
        return node.port
    if isinstance(node, Index):
        return f"{node.table}[{expr_pseudo(node.where)}]"
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
        if isinstance(node, SetPart):
            out.append(f"{pad}set {node.part} to {expr_pseudo(node.value)}")
        elif isinstance(node, SetPort):
            out.append(f"{pad}set {node.port} to {expr_pseudo(node.value)}")
        elif isinstance(node, Print):
            out.append(f'{pad}print "{node.text}"' if node.value is None
                       else f"{pad}print {expr_pseudo(node.value)}")
        elif isinstance(node, SetTone):
            if isinstance(node.hz, Num) and node.hz.value == 0:
                out.append(f"{pad}turn off {node.pin}")
            else:
                out.append(f"{pad}set {node.pin} to {expr_pseudo(node.hz)} hz")
        elif isinstance(node, SetPwm):
            out.append(f"{pad}set {node.pin} to {expr_pseudo(node.value)} percent")
        elif isinstance(node, SetPin):
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
    if program.tables:
        out.append("")
        for name, values in program.tables.items():
            out.append(f"  TABLE {name} = " + ", ".join(f"0x{v:02X}" for v in values))
    if program.parts:
        out.append("")
        for part in program.parts.values():
            polarity = " ACTIVE LOW" if part.active_low else ""
            d, c, l = part.data, part.clock, part.latch
            out.append(f"  PART {part.name} = 74HC595 DATA P{d[0]}.{d[1]} "
                       f"CLOCK P{c[0]}.{c[1]} LATCH P{l[0]}.{l[1]}{polarity}")
    if program.ports:
        out.append("")
        for port in program.ports.values():
            polarity = " ACTIVE LOW" if port.active_low else ""
            out.append(f"  PORT {port.name} = P{port.port} "
                       f"{port.direction.upper()}{polarity}")
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
    program: "Program" = None    # ports and tables are program-level
    counter: list = field(default_factory=lambda: [0])


def expr_c(node: Expr, ctx: Emit, parent_level: int = -1) -> str:
    if isinstance(node, Num):
        return str(int(node.value))
    if isinstance(node, PortRef):
        port = ctx.program.ports[node.port]
        raw = port.sfr
        return f"(unsigned char)~{raw}" if port.active_low else raw
    if isinstance(node, Index):
        table = ctx.program.tables[node.table]
        where = expr_c(node.where, ctx)
        # A constant index is checked here and costs nothing at run time. A
        # computed one is clamped, because the alternative is reading a random
        # byte of flash and showing it on a display -- which looks like data.
        if isinstance(node.where, Num):
            i = int(node.where.value)
            if not 0 <= i < len(table):
                raise PseudocodeError(
                    0, f"{node.table}[{i}] is outside the table "
                       f"(0 to {len(table) - 1})")
            return f"bw_tab_{node.table}[{i}]"
        return f"bw_tab_{node.table}[bw_clamp({where}, {len(table) - 1})]"
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
        elif isinstance(node, SetPwm):
            out.append(pad + ctx.target.write_pwm(ctx.pins[node.pin],
                                                  expr_c(node.value, ctx)))
        elif isinstance(node, SetTone):
            out.append(pad + ctx.target.write_tone(ctx.pins[node.pin],
                                                   expr_c(node.hz, ctx)))
        elif isinstance(node, Print):
            rendered = Print(text=node.text,
                             value=None if node.value is None else expr_c(node.value, ctx))
            out.append(pad + ctx.target.write_print(rendered))
        elif isinstance(node, SetPart):
            part = ctx.program.parts[node.part]
            value = expr_c(node.value, ctx)
            if part.active_low:
                out.append(f"{pad}bw_part_{part.name}((unsigned char)~({value}));")
            else:
                out.append(f"{pad}bw_part_{part.name}((unsigned char)({value}));")
        elif isinstance(node, SetPort):
            port = ctx.program.ports[node.port]
            value = expr_c(node.value, ctx)
            if port.active_low:
                out.append(f"{pad}{port.sfr} = (unsigned char)~({value});")
            else:
                out.append(f"{pad}{port.sfr} = (unsigned char)({value});")
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
        elif isinstance(node, SetPwm):
            out.append(pad + ctx.target.write_pwm(ctx.pins[node.pin],
                                                  expr_c(node.value, ctx)))
        elif isinstance(node, SetTone):
            out.append(pad + ctx.target.write_tone(ctx.pins[node.pin],
                                                   expr_c(node.hz, ctx)))
        elif isinstance(node, Print):
            rendered = Print(text=node.text,
                             value=None if node.value is None else expr_c(node.value, ctx))
            out.append(pad + ctx.target.write_print(rendered))
        elif isinstance(node, SetPart):
            part = ctx.program.parts[node.part]
            value = expr_c(node.value, ctx)
            if part.active_low:
                out.append(f"{pad}bw_part_{part.name}((unsigned char)~({value}));")
            else:
                out.append(f"{pad}bw_part_{part.name}((unsigned char)({value}));")
        elif isinstance(node, SetPort):
            port = ctx.program.ports[node.port]
            value = expr_c(node.value, ctx)
            if port.active_low:
                out.append(f"{pad}{port.sfr} = (unsigned char)~({value});")
            else:
                out.append(f"{pad}{port.sfr} = (unsigned char)({value});")
        elif isinstance(node, Toggle):
            out.append(pad + ctx.target.toggle_pin(ctx.pins[node.pin]))
        elif isinstance(node, Wait):
            state = yield_state()
            out += [f"{pad}{task}_until = {ctx.target.now()} + ({ms_of(node, ctx)});",
                    f"{pad}{task}_state = {state};",
                    f"{pad}case {state}:",
                    f"{pad}if ((int)({ctx.target.now()} - {task}_until) < 0) return;"]
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
               program.procedures, program)
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
                head.append(f"static unsigned int {task}_until;")
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

    out += ["void main(void)", "{"]
    out += target.setup(program)
    if tasks:
        out += target.start_scheduler(task_names)
    else:
        out.append("")
        out += stmts_c(program.body, 1, ctx)
    out += ["}", ""]
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
