"""
stc_pseudocode — BrickWright-style pseudocode ⇄ C for the STC12 / 8051.

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
    name: str
    port: int
    bit: int
    direction: str              # "output" | "input" | "analog"
    active_low: bool = False

    @property
    def sfr(self) -> str:
        return f"P{self.port}_{self.bit}"

    @property
    def mask(self) -> int:
        return 1 << self.bit

    @property
    def adc_channel(self) -> int:
        return self.bit         # ADC channel n is on P1.n


@dataclass
class Procedure:
    name: str
    params: list
    body: list = field(default_factory=list)

    @property
    def c_name(self) -> str:
        return "bw_" + re.sub(r"\W", "_", self.name)


@dataclass
class Program:
    part: str = "stc12c5a60s2"
    clock: int = 11059200
    pins: dict = field(default_factory=dict)
    variables: list = field(default_factory=list)
    procedures: dict = field(default_factory=dict)
    body: list = field(default_factory=list)
    locals_: set = field(default_factory=set)

    @property
    def uses_adc(self) -> bool:
        return any(pin.direction == "analog" for pin in self.pins.values())


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
PIN_RE = re.compile(r"pin\s+(\w+)\s*=\s*(p[0-4]\.[0-7])\s+(output|input|analog)"
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
            port, bit = PORT_RE.match(where).groups()
            if direction == "analog" and port != "1":
                raise PseudocodeError(
                    line.number, "ANALOG is only available on P1.0-P1.7 "
                                 f"(ADC0-ADC7), not {where.upper()}")
            program.pins[name] = Pin(name, int(port), int(bit), direction,
                                     active_low=(active == "low"))
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
            if started:
                raise PseudocodeError(line.number, "only one WHEN block is supported")
            started = True
            program.body, index = parse_block(lines, index + 1, line.indent, program)
            continue

        raise PseudocodeError(
            line.number, f"do not understand {text!r}"
            + ("" if started else " (expected CLOCK, PIN, DEFINE or WHEN started:)"))

    if not started:
        raise PseudocodeError(lines[-1].number, "no 'WHEN started:' block")
    if not program.body:
        raise PseudocodeError(lines[-1].number, "'WHEN started:' block is empty")
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
            out.append(f"  PIN {pin.name} = P{pin.port}.{pin.bit} "
                       f"{pin.direction.upper()}{polarity}")
    for procedure in program.procedures.values():
        out.append("")
        params = "".join(f" ({name})" for name in procedure.params)
        out.append(f"  DEFINE {procedure.name}{params}:")
        out += stmts_pseudo(procedure.body, 2, active_low)
    out += ["", "  WHEN started:"]
    out += stmts_pseudo(program.body, 2, active_low)
    return "\n".join(out) + "\n"


# ================================================================== C back end

def expr_c(node: Expr, pins: dict, parent_level: int = -1) -> str:
    if isinstance(node, Num):
        return str(int(node.value))
    if isinstance(node, Var):
        return node.name
    if isinstance(node, PinRef):
        pin = pins[node.name]
        if pin.direction == "analog":
            return f"adc_read({pin.adc_channel})"
        return f"!{pin.sfr}" if pin.active_low else pin.sfr
    if isinstance(node, Unary):
        inner = expr_c(node.operand, pins, UNARY_LEVEL)
        return f"!({inner})" if node.op == "not" else f"-({inner})"
    if isinstance(node, Binary):
        level = LEVEL[node.op]
        text = (f"{expr_c(node.left, pins, level)} {TO_C[node.op]} "
                f"{expr_c(node.right, pins, level + 1)}")
        return f"({text})" if level < parent_level else text
    raise TypeError(node)


def ms_of(node: Wait, pins: dict) -> str:
    """A Wait in milliseconds, folded to a constant where it can be."""
    if isinstance(node.amount, Num):
        value = node.amount.value
        return str(int(round(value * 1000 if node.unit == "seconds" else value)))
    inner = expr_c(node.amount, pins, UNARY_LEVEL)
    return inner if node.unit == "ms" else f"(unsigned int)(({inner}) * 1000)"


def stmts_c(body: list, depth: int, pins: dict, procs: dict, counter: list) -> list[str]:
    pad = "    " * depth
    out = []
    for node in body:
        if isinstance(node, SetPin):
            out.append(f"{pad}{pins[node.pin].sfr} = {1 if node.high else 0};")
        elif isinstance(node, Toggle):
            sfr = pins[node.pin].sfr
            out.append(f"{pad}{sfr} = !{sfr};")
        elif isinstance(node, Wait):
            out.append(f"{pad}delay_ms({ms_of(node, pins)});")
        elif isinstance(node, WaitUntil):
            out.append(f"{pad}while (!({expr_c(node.cond, pins)})) ;")
        elif isinstance(node, SetVar):
            out.append(f"{pad}{node.name} = {expr_c(node.value, pins)};")
        elif isinstance(node, ChangeVar):
            out.append(f"{pad}{node.name} += {expr_c(node.delta, pins)};")
        elif isinstance(node, Forever):
            out.append(f"{pad}for (;;) {{")
            out += stmts_c(node.body, depth + 1, pins, procs, counter)
            out.append(f"{pad}}}")
        elif isinstance(node, Repeat):
            counter[0] += 1
            var = f"_i{counter[0]}"
            out.append(f"{pad}{{ unsigned int {var};")
            out.append(f"{pad}  for ({var} = 0; {var} < "
                       f"({expr_c(node.count, pins)}); {var}++) {{")
            out += stmts_c(node.body, depth + 2, pins, procs, counter)
            out += [f"{pad}  }}", f"{pad}}}"]
        elif isinstance(node, Loop):
            test = expr_c(node.cond, pins, UNARY_LEVEL if node.until else -1)
            if node.until:
                test = f"!({test})"      # REPEAT UNTIL c  ==  WHILE not c
            out.append(f"{pad}while ({test}) {{")
            out += stmts_c(node.body, depth + 1, pins, procs, counter)
            out.append(f"{pad}}}")
        elif isinstance(node, If):
            out.append(f"{pad}if ({expr_c(node.cond, pins)}) {{")
            out += stmts_c(node.body, depth + 1, pins, procs, counter)
            if node.orelse:
                out.append(f"{pad}}} else {{")
                out += stmts_c(node.orelse, depth + 1, pins, procs, counter)
            out.append(f"{pad}}}")
        elif isinstance(node, Call):
            args = ", ".join(expr_c(a, pins) for a in node.args)
            out.append(f"{pad}{procs[node.name.lower()].c_name}({args});")
        elif isinstance(node, Stop):
            out.append(f"{pad}for (;;) ;   /* stop */")
        else:
            raise TypeError(node)
    return out


def emit_c(program: Program) -> str:
    pins = {pin.name: pin for pin in program.pins.values()}
    procs = program.procedures
    counter = [0]

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

    if program.variables:
        out.append("/* Variables (16-bit signed, like Scratch's integers). */")
        out += [f"static int {name} = 0;" for name in program.variables]
        out.append("")

    if procs:
        for procedure in procs.values():
            params = ", ".join(f"int {p}" for p in procedure.params) or "void"
            out.append(f"static void {procedure.c_name}({params});")
        out.append("")
        for procedure in procs.values():
            params = ", ".join(f"int {p}" for p in procedure.params) or "void"
            out += [f"/* DEFINE {procedure.name} */",
                    f"static void {procedure.c_name}({params})", "{",
                    *stmts_c(procedure.body, 1, pins, procs, counter), "}", ""]

    out += ["void main(void)", "{"]

    outputs: dict = {}
    for pin in program.pins.values():
        if pin.direction == "output":
            outputs[pin.port] = outputs.get(pin.port, 0) | pin.mask
    for port in sorted(outputs):
        mask = outputs[port]
        out += [f"    P{port}M1 &= ~0x{mask:02X};   /* push-pull */",
                f"    P{port}M0 |=  0x{mask:02X};"]
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

    out += ["",
            "    AUXR &= ~0x80;                 /* Timer 0 at FOSC/12 */",
            "    TMOD  = (TMOD & 0xF0) | 0x01;  /* Timer 0, mode 1 */",
            ""]
    out += stmts_c(program.body, 1, pins, procs, counter)
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
