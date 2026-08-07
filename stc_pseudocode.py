"""
stc_pseudocode — BrickWright-style pseudocode → C for the STC12 / 8051.

The dialect deliberately follows the conventions already used by
sb3-creator's pseudocode (`SPRITE Name:` / `WHEN flag clicked:` / `REPEAT n:` /
`IF x > y THEN:` / `set v to n`): UPPERCASE for structure and control flow,
lowercase for statements, and indentation for nesting. Someone who can read a
BrickWright project can read this.

    DEVICE STC12C5A60S2:
      CLOCK 11059200
      PIN led1 = P1.0 OUTPUT ACTIVE LOW
      PIN led2 = P1.1 OUTPUT ACTIVE LOW
      PIN button = P3.2 INPUT

      WHEN started:
        set counter to 0
        FOREVER:
          turn on led1
          turn off led2
          wait 0.15 seconds
          turn off led1
          turn on led2
          wait 0.15 seconds

The emitted C is the same shape as the hand-written examples in
CrispStrobe/stc12c5a60s2-lab, so the two stay comparable: Timer 0 for the
millisecond base, PxM1/PxM0 for port modes, and active-low LED wiring.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# --------------------------------------------------------------------- model

PORT_RE = re.compile(r"^P([0-4])\.([0-7])$", re.I)
NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class PseudocodeError(Exception):
    """Carries a line number so the caller can point at the offending line."""

    def __init__(self, line: int, message: str):
        self.line = line
        super().__init__(f"line {line}: {message}")


@dataclass
class Pin:
    name: str
    port: int
    bit: int
    direction: str          # "output" | "input"
    active_low: bool = False

    @property
    def sfr(self) -> str:
        return f"P{self.port}_{self.bit}"

    @property
    def mask(self) -> int:
        return 1 << self.bit


@dataclass
class Program:
    part: str = "stc12c5a60s2"
    clock: int = 11059200
    pins: dict[str, Pin] = field(default_factory=dict)
    variables: list[str] = field(default_factory=list)
    body: list = field(default_factory=list)


# -------------------------------------------------------------------- lexing

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
        if "\t" in stripped[: len(stripped) - len(stripped.lstrip())]:
            raise PseudocodeError(number, "tabs in indentation; use spaces")
        out.append(Line(number, len(stripped) - len(stripped.lstrip()), stripped.strip()))
    return out


# --------------------------------------------------------------- expressions
# Precedence climbing over the same operator set the BrickWright pseudocode
# uses. `=` is comparison here, as it is there — assignment is `set ... to ...`.

BINARY = [
    (("or",), "||"),
    (("and",), "&&"),
    (("=", "==", "!=", "<>", "<=", ">=", "<", ">"), None),
    (("+", "-"), None),
    (("*", "/", "%"), None),
]
COMPARE = {"=": "==", "==": "==", "!=": "!=", "<>": "!=",
           "<=": "<=", ">=": ">=", "<": "<", ">": ">"}

TOKEN_RE = re.compile(r"""
    \s*(?:
      (?P<number>\d+\.\d+|\d+)
    | (?P<op><=|>=|!=|<>|==|[-+*/%()<>=])
    | (?P<word>[A-Za-z_][A-Za-z0-9_.]*)
    )""", re.X)


def tokenize_expr(text: str, line: int) -> list[str]:
    tokens, pos = [], 0
    while pos < len(text):
        match = TOKEN_RE.match(text, pos)
        if not match or match.end() == pos:
            if text[pos:].strip():
                raise PseudocodeError(line, f"cannot parse {text[pos:].strip()!r}")
            break
        tokens.append(match.group().strip())
        pos = match.end()
    return [t for t in tokens if t]


class ExprParser:
    def __init__(self, tokens: list[str], program: Program, line: int):
        self.tokens, self.pos, self.program, self.line = tokens, 0, program, line

    def peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def take(self):
        token = self.peek()
        self.pos += 1
        return token

    def parse(self, level: int = 0) -> str:
        if level >= len(BINARY):
            return self.atom()
        left = self.parse(level + 1)
        operators, fixed = BINARY[level]
        while True:
            token = self.peek()
            if token is None or token.lower() not in operators:
                return left
            self.take()
            right = self.parse(level + 1)
            c_op = fixed or COMPARE.get(token, token)
            left = f"({left} {c_op} {right})"

    def atom(self) -> str:
        token = self.take()
        if token is None:
            raise PseudocodeError(self.line, "expression ended early")
        if token == "(":
            inner = self.parse()
            if self.take() != ")":
                raise PseudocodeError(self.line, "missing ')'")
            return f"({inner})"
        if token == "-":
            return f"(-{self.atom()})"
        if token.lower() == "not":
            return f"(!{self.atom()})"
        if re.fullmatch(r"\d+\.\d+|\d+", token):
            return str(int(float(token))) if "." not in token else token
        lowered = token.lower()
        if lowered in self.program.pins:
            pin = self.program.pins[lowered]
            # Reading an active-low input: "pressed" means the pin is low.
            return f"(!{pin.sfr})" if pin.active_low else pin.sfr
        if lowered in ("true", "on", "high"):
            return "1"
        if lowered in ("false", "off", "low"):
            return "0"
        if NAME_RE.match(token):
            if token not in self.program.variables:
                self.program.variables.append(token)
            return token
        raise PseudocodeError(self.line, f"unexpected {token!r}")


def expression(text: str, program: Program, line: int) -> str:
    parser = ExprParser(tokenize_expr(text, line), program, line)
    value = parser.parse()
    if parser.peek() is not None:
        raise PseudocodeError(line, f"trailing {parser.peek()!r} in expression")
    return value


# ------------------------------------------------------------------ statements

def parse_block(lines: list[Line], index: int, parent_indent: int,
                program: Program) -> tuple[list[str], int]:
    """Emit C for the block nested under `parent_indent`.

    The block's own indent is whatever its first line uses, so 2 spaces, 4
    spaces or any other consistent width all work -- only consistency within
    one block matters.
    """
    body: list[str] = []
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
        text = line.text
        lowered = text.lower()

        # --- FOREVER: -----------------------------------------------------
        if lowered in ("forever:", "forever"):
            inner, index = parse_block(lines, index + 1, indent, program)
            body += ["for (;;) {", *indent_all(inner), "}"]
            continue

        # --- REPEAT n: ----------------------------------------------------
        repeat = re.fullmatch(r"repeat\s+(.+?)\s*:", lowered)
        if repeat:
            count = expression(text[len("repeat"):].rstrip(":").strip(), program, line.number)
            inner, index = parse_block(lines, index + 1, indent, program)
            var = f"_i{indent}"
            body += [f"{{ unsigned int {var}; for ({var} = 0; {var} < ({count}); {var}++) {{",
                     *indent_all(inner), "} }"]
            continue

        # --- IF cond THEN: / ELSE: ---------------------------------------
        cond = re.fullmatch(r"if\s+(.+?)\s+then\s*:", lowered)
        if cond:
            raw = text[len("if"):].strip()
            raw = raw[: raw.lower().rindex("then")].strip()
            test = expression(raw, program, line.number)
            inner, index = parse_block(lines, index + 1, indent, program)
            body += [f"if ({test}) {{", *indent_all(inner)]
            if (index < len(lines) and lines[index].indent == indent
                    and lines[index].text.lower() in ("else:", "else")):
                otherwise, index = parse_block(lines, index + 1, indent, program)
                body += ["} else {", *indent_all(otherwise), "}"]
            else:
                body += ["}"]
            continue

        if lowered in ("else:", "else"):
            raise PseudocodeError(line.number, "ELSE without a matching IF")

        body.append(simple_statement(text, program, line.number))
        index += 1
    return body, index


def indent_all(lines: list[str]) -> list[str]:
    return ["    " + line for line in lines]


def simple_statement(text: str, program: Program, line: int) -> str:
    lowered = text.lower()

    def pin_of(name: str) -> Pin:
        pin = program.pins.get(name.lower())
        if pin is None:
            raise PseudocodeError(line, f"unknown pin {name!r}; declare it with PIN")
        if pin.direction != "output":
            raise PseudocodeError(line, f"{name!r} is an INPUT and cannot be driven")
        return pin

    # wait <n> seconds | wait <n> ms
    wait = re.fullmatch(r"wait\s+(.+?)\s*(seconds?|secs?|s|ms|milliseconds?)", lowered)
    if wait:
        amount, unit = wait.group(1), wait.group(2)
        try:
            value = float(amount)
            ms = int(round(value if unit.startswith("m") else value * 1000))
            return f"delay_ms({ms});"
        except ValueError:
            expr = expression(amount, program, line)
            return (f"delay_ms({expr});" if unit.startswith("m")
                    else f"delay_ms((unsigned int)(({expr}) * 1000));")

    # turn on/off <pin>
    turn = re.fullmatch(r"turn\s+(on|off)\s+(\w+)", lowered)
    if turn:
        pin = pin_of(turn.group(2))
        on = turn.group(1) == "on"
        level = (0 if on else 1) if pin.active_low else (1 if on else 0)
        return f"{pin.sfr} = {level};"

    # set <pin> high/low  |  set <var> to <expr>
    setp = re.fullmatch(r"set\s+(\w+)\s+(high|low)", lowered)
    if setp:
        return f"{pin_of(setp.group(1)).sfr} = {1 if setp.group(2) == 'high' else 0};"

    setv = re.match(r"set\s+([A-Za-z_]\w*)\s+to\s+(.+)$", text, re.I)
    if setv:
        name = setv.group(1)
        if name.lower() in program.pins:
            raise PseudocodeError(line, f"{name!r} is a pin; use 'set {name} high/low'")
        if name not in program.variables:
            program.variables.append(name)
        return f"{name} = {expression(setv.group(2), program, line)};"

    change = re.match(r"change\s+([A-Za-z_]\w*)\s+by\s+(.+)$", text, re.I)
    if change:
        name = change.group(1)
        if name not in program.variables:
            program.variables.append(name)
        return f"{name} += {expression(change.group(2), program, line)};"

    toggle = re.fullmatch(r"toggle\s+(\w+)", lowered)
    if toggle:
        sfr = pin_of(toggle.group(1)).sfr
        return f"{sfr} = !{sfr};"

    if lowered in ("stop", "stop all", "halt"):
        return "for (;;) ;   /* stop */"

    raise PseudocodeError(line, f"do not understand {text!r}")


# ---------------------------------------------------------------- the parser

def parse(source: str) -> Program:
    lines = read_lines(source)
    if not lines:
        raise PseudocodeError(1, "empty program")

    program = Program()
    index = 0

    # Optional `DEVICE <part>:` wrapper, mirroring `SPRITE Name:`.
    base_indent = lines[0].indent
    device = re.fullmatch(r"device\s+([\w-]+)\s*:", lines[0].text, re.I)
    if device:
        program.part = device.group(1).lower()
        index = 1
        base_indent = lines[1].indent if len(lines) > 1 else 0

    started = False
    while index < len(lines):
        line = lines[index]
        text, lowered = line.text, line.text.lower()

        clock = re.fullmatch(r"clock\s+([\d_]+)\s*(hz|mhz)?", lowered)
        if clock and not started:
            value = int(clock.group(1).replace("_", ""))
            program.clock = value * 1_000_000 if clock.group(2) == "mhz" else value
            index += 1
            continue

        pin = re.fullmatch(
            r"pin\s+(\w+)\s*=\s*(p[0-4]\.[0-7])\s+(output|input)"
            r"(?:\s+active\s+(low|high))?", lowered)
        if pin and not started:
            name, where, direction, active = pin.groups()
            if name in program.pins:
                raise PseudocodeError(line.number, f"pin {name!r} declared twice")
            port, bit = PORT_RE.match(where).groups()
            program.pins[name] = Pin(name, int(port), int(bit), direction,
                                     active_low=(active == "low"))
            index += 1
            continue

        if re.fullmatch(r"when\s+(started|flag\s+clicked|powered\s+on)\s*:", lowered):
            if started:
                raise PseudocodeError(line.number, "only one WHEN block is supported")
            started = True
            program.body, index = parse_block(lines, index + 1, line.indent, program)
            continue

        raise PseudocodeError(
            line.number,
            f"do not understand {text!r}"
            + ("" if started else " (expected CLOCK, PIN or WHEN started:)"))

    if not started:
        raise PseudocodeError(lines[-1].number, "no 'WHEN started:' block")
    if not program.body:
        raise PseudocodeError(lines[-1].number, "'WHEN started:' block is empty")
    return program


# ------------------------------------------------------------------- emitter

def emit_c(program: Program) -> str:
    out = [
        "/* Generated from BrickWright pseudocode by stc-compiler.",
        " * Hand edits will be lost; change the pseudocode instead. */",
        "#include <stc12.h>",
        "",
        f"#define FOSC_HZ {program.clock}UL",
        "",
        "/* Timer 0, mode 1, clocked at FOSC/12 -- accuracy depends only on FOSC. */",
        "#define T0_RELOAD (65536UL - (FOSC_HZ / 12UL / 1000UL))",
        "",
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

    if program.variables:
        out.append("/* Variables (16-bit signed, like Scratch's integers). */")
        out += [f"static int {name} = 0;" for name in program.variables]
        out.append("")

    out += ["void main(void)", "{"]

    # Port modes. Outputs go push-pull so a "high" is a real high, not a weak
    # pull-up; inputs stay quasi-bidirectional so they can still be read.
    outputs: dict[int, int] = {}
    for pin in program.pins.values():
        if pin.direction == "output":
            outputs[pin.port] = outputs.get(pin.port, 0) | pin.mask
    for port in sorted(outputs):
        mask = outputs[port]
        out += [f"    P{port}M1 &= ~0x{mask:02X};   /* push-pull */",
                f"    P{port}M0 |=  0x{mask:02X};"]

    # Safe initial state: everything off, using each pin's own polarity.
    for pin in program.pins.values():
        if pin.direction == "output":
            out.append(f"    {pin.sfr} = {1 if pin.active_low else 0};"
                       f"   /* {pin.name} off */")

    out += ["",
            "    AUXR &= ~0x80;                 /* Timer 0 at FOSC/12 */",
            "    TMOD  = (TMOD & 0xF0) | 0x01;  /* Timer 0, mode 1 */",
            ""]
    out += indent_all(program.body)
    out += ["}", ""]
    return "\n".join(out)


def transpile(source: str) -> tuple[str, Program]:
    program = parse(source)
    return emit_c(program), program


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
