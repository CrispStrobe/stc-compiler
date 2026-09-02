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


def _c_string(text: str) -> str:
    """Escape a literal for a C string, quotes and all.

    The dialect's `print "..."` regex already forbids a quote inside the text,
    but not a backslash -- and `print "x \\"` put a lone backslash at the end of
    the emitted literal, where it escaped the CLOSING quote and left the
    generated C unterminated. Escaping properly is cheaper than reasoning
    about which characters the parser happens to exclude today.
    """
    out = []
    for char in text:
        if char in ("\\", '"'):
            out.append("\\" + char)
        elif char == "\n":
            out.append("\\n")
        elif char == "\t":
            out.append("\\t")
        elif ord(char) < 0x20 or ord(char) > 0x7E:
            out.append(f"\\x{ord(char):02x}")
        else:
            out.append(char)
    return "".join(out)

PORT_RE = re.compile(r"^P([0-5])\.([0-7])$", re.I)
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
class KeypadRef(Expr):
    """Reading a KEYPAD4X4 PART: the scanned key 0..15, or -1 for none."""
    part: str


@dataclass
class MatrixPixelRef(Expr):
    """`pixel X Y is on` on a MATRIX8X8: true iff that pixel's level != 0."""
    part: str
    x: Expr
    y: Expr


@dataclass
class Randint(Expr):
    low: Expr
    high: Expr

@dataclass
class ControllerAxis(Expr):
    axis: str           # "dx" | "dy"

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
NOT_LEVEL = LEVEL["="]          # `not` parses its operand at comparison level

TO_C = {"or": "||", "and": "&&", "=": "==", "!=": "!=",
        "<": "<", ">": ">", "<=": "<=", ">=": ">=",
        "+": "+", "-": "-", "*": "*", "/": "/", "%": "%"}
SYNONYM = {"==": "=", "<>": "!="}
WORD_OPS = {"mod": "%"}         # spelled-out operators; sb3-creator's
                                # dialect writes `a mod b`, and the two
                                # front ends must accept the same programs


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
    # The source line, so a target whose clock cannot express this wait can point
    # at it. Defaults to 0 for hand-built nodes; every parsed Wait carries the
    # real line.
    #
    # `compare=False` because this is provenance, not meaning. The round-trip
    # tests assert that decompiling and re-parsing yields an equal AST, and two
    # programs that differ only in which line a wait sits on are the same
    # program — without this, adding the field failed 20 round-trips that had
    # nothing wrong with them.
    line: int = field(default=0, compare=False)


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


# ---- MATRIX8X8 drawing verbs. All write the RAM frame buffer only; the
# Timer-0 ISR scans it onto the panel. `part` is the MATRIX8X8's name.

@dataclass
class MatrixClear(Stmt):
    part: str


@dataclass
class MatrixSetPixel(Stmt):
    part: str
    x: Expr
    y: Expr
    # "light" / "clear" (full / off), "on" / "off" (set ... to on|off), or
    # "brightness" (level carries the Expr). `level` is None except for
    # "brightness". Kept so decompiling gives back the same sentence.
    style: str
    level: Expr = None


@dataclass
class MatrixDrawRow(Stmt):
    part: str
    y: Expr
    bits: Expr


@dataclass
class MatrixImage(Stmt):
    part: str
    table: str


@dataclass
class MatrixScroll(Stmt):
    part: str
    direction: str              # "left" | "right" | "up" | "down"


@dataclass
class MatrixBrightness(Stmt):
    part: str
    level: Expr


@dataclass
class ShowNumber(Stmt):
    display: str
    value: Expr


@dataclass
class ShowDigit(Stmt):
    display: str
    digit: Expr
    value: Expr


@dataclass
class SetDigitSegments(Stmt):
    display: str
    digit: Expr
    segments: Expr


@dataclass
class ClearDisplay(Stmt):
    display: str


@dataclass
class TurnOnLed(Stmt):
    bank: str
    index: Expr


@dataclass
class TurnOffLed(Stmt):
    bank: str
    index: Expr


@dataclass
class SetLeds(Stmt):
    bank: str
    value: Expr


@dataclass
class LightOnlyLed(Stmt):
    bank: str
    index: Expr


# ---- Arcade game-engine statements -----------------------------------

@dataclass
class ArcadeCreate(Stmt):
    sprite: str
    kind: str

@dataclass
class ArcadePlace(Stmt):
    sprite: str
    x: Expr
    y: Expr

@dataclass
class ArcadeMove(Stmt):
    sprite: str
    vx: Expr
    vy: Expr

@dataclass
class ArcadeSetFlag(Stmt):
    sprite: str
    flag: str           # "stayinscreen" | "destroyonwall"

@dataclass
class ArcadeScore(Stmt):
    delta: Expr

@dataclass
class ArcadeGameOver(Stmt):
    win: bool

@dataclass
class ArcadeOnOverlap(Stmt):
    kind_a: str
    kind_b: str
    body: list

@dataclass
class ArcadeTilemap(Stmt):
    name: str
    cols: Expr
    rows: Expr
    tile_size: Expr

@dataclass
class ArcadeSetTile(Stmt):
    tilemap: str
    col: Expr
    row: Expr
    tile_index: Expr

@dataclass
class ArcadeTileWall(Stmt):
    tilemap: str
    tile_index: Expr

@dataclass
class ArcadeSetFrame(Stmt):
    sprite: str
    frame: Expr


# ---- Display-peripheral statements (LCD / TFT / OLED / RGB) ---------

@dataclass
class LcdPrint(Stmt):
    display: str
    text: str = None        # string literal  (one of text/value is set)
    value: Expr = None      # expression

@dataclass
class LcdCursor(Stmt):
    display: str
    row: Expr
    col: Expr

@dataclass
class LcdClear(Stmt):
    display: str

@dataclass
class TftPixel(Stmt):
    display: str
    x: Expr
    y: Expr
    r: Expr
    g: Expr
    b: Expr

@dataclass
class TftFill(Stmt):
    display: str
    x: Expr
    y: Expr
    w: Expr
    h: Expr
    r: Expr
    g: Expr
    b: Expr

@dataclass
class TftClear(Stmt):
    display: str

@dataclass
class TftPrint(Stmt):
    display: str
    text: str = None
    value: Expr = None

@dataclass
class TftCursor(Stmt):
    display: str
    row: Expr
    col: Expr

@dataclass
class OledPixel(Stmt):
    display: str
    x: Expr
    y: Expr
    value: Expr

@dataclass
class OledClear(Stmt):
    display: str

@dataclass
class OledPrint(Stmt):
    display: str
    text: str = None
    value: Expr = None

@dataclass
class OledCursor(Stmt):
    display: str
    row: Expr
    col: Expr

@dataclass
class RgbSet(Stmt):
    led: str
    r: Expr
    g: Expr
    b: Expr


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
    port: int                   # opaque target-side identity, for clash checks
    direction: str              # "output" | "input"
    active_low: bool = False
    # The location token as the target canonicalised it -- "P2" on an 8051,
    # "D" on an AVR. Same job as Pin.where: it is what the pseudocode back end
    # writes, so the round trip does not depend on how `port` is numbered.
    where: str = ""
    # Where the byte is written and where it is read. Separate because they
    # are separate registers on an AVR: writing PORTB drives the pins, reading
    # PORTB gives you back the latch, and only PINB gives the actual levels.
    # On an 8051 both are the one SFR, which is why these default to it.
    write_sfr: str = ""
    read_sfr: str = ""

    @property
    def label(self) -> str:
        return self.where or f"P{self.port}"

    @property
    def sfr(self) -> str:
        return self.write_sfr or f"P{self.port}"

    @property
    def read(self) -> str:
        return self.read_sfr or self.sfr


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
    data: Pin                   # three OUTPUT pins, resolved by the target
    clock: Pin
    latch: Pin
    active_low: bool = False

    @property
    def claimed(self) -> list:
        return [self.data, self.clock, self.latch]

    @property
    def claimed_where(self) -> list:
        """The three locations as the target spells them, for clash checks."""
        return [pin.where for pin in self.claimed]


@dataclass
class KeypadPart:
    """A 4x4 matrix keypad: sixteen keys for eight pins.

    Modelled as a read-only value — the scanned key 0..15 (row-major from
    the top-left), or -1 while nothing is pressed — because that is what a
    program wants to know. The scan drives one row low at a time and reads
    the columns, which is invisible from the outside and cheap enough to
    run on every read.

    Measured precedent: the Prechin A2's keypad (rows P1.7..P1.4, cols
    P1.3..P1.0) was mapped on real silicon by src/06-matrix89 and consumed
    by src/09-keyshow89 in the stc12c5a60s2-lab repo, 2026-08-17. The C
    this PART emits is that verified scanner.

    Two-key caveat, inherited from the classic scan: two keys pressed in
    the SAME COLUMN short a driven-low row into an idle-high one. On
    quasi-bidirectional 8051 ports the weak pull-up limits that current by
    construction, which is why the part is admitted for the 8051 family
    first; push-pull targets need row tri-stating before they can opt in.
    """
    name: str
    kind: str                   # "keypad4x4"
    rows: list                  # 4 pins, top row first — driven low one at a time
    cols: list                  # 4 pins, left column first — read

    @property
    def claimed(self) -> list:
        return self.rows + self.cols

    @property
    def claimed_where(self) -> list:
        return [pin.where for pin in self.claimed]


@dataclass
class MatrixPart:
    """An 8x8 LED dot matrix: rows through a 74HC595, columns on a whole port.

    Measured on Prechin A2 silicon (docs/BOARD-PRECHIN-A2.md): the 595 selects
    the physical ROWS active HIGH with Q7 = top, and the port's eight bits sink
    the COLUMNS active LOW with bit 7 = left. Both orientations are baked into
    the emitted scan, so image bytes read top-down / MSB-left -- a literal looks
    like the picture.

    Multiplexed, so it cannot be scanned in a user loop without monopolising the
    program. Instead it CLAIMS its eleven pins (three 595 + eight columns) and
    the Timer-0 ISR is the SOLE writer of the 595 and the column port: one row
    per tick, 8 rows -> a full frame every 8 ms = 125 Hz. That claim is also what
    closes the 8051 read-modify-write hazard on the shared port latch -- mainline
    only ever writes the RAM frame buffer, never the port. See docs/A2-BOARD-SUPPORT.md.
    """
    name: str
    kind: str                   # "matrix8x8"
    data: Pin                   # 595 SER
    clock: Pin                  # 595 SCLK
    latch: Pin                  # 595 RCLK
    col_port: "Port"            # the whole column port (active-low sinks)
    columns: list               # its eight pins, for the claim + setup()
    active_low: bool = False    # unused: the ISR owns column polarity directly

    @property
    def claimed(self) -> list:
        return [self.data, self.clock, self.latch] + self.columns

    @property
    def claimed_where(self) -> list:
        return [pin.where for pin in self.claimed]


@dataclass
class SevenSegPart:
    """SEVENSEG8: 8-digit 7-seg via 74HC245 (segments on a port) + 74HC138
    (3 address pins for digit select). ISR-driven multiplexed refresh."""
    name: str
    kind: str = "sevenseg8"
    seg_port: int = 0
    sel_pins: list = field(default_factory=list)
    common_anode: bool = False

    @property
    def claimed(self) -> list:
        return list(self.sel_pins)

    @property
    def claimed_where(self) -> list:
        return [pin.where for pin in self.sel_pins]


@dataclass
class LedBankPart:
    """LEDBANK8: 8 LEDs on a port. Writes go through an ISR-owned shadow byte."""
    name: str
    kind: str = "ledbank8"
    led_port: int = 0
    active_low: bool = False
    led_port_where: str = ""

    @property
    def claimed(self) -> list:
        return []

    @property
    def claimed_where(self) -> list:
        return []


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

    # Which of the later peripheral features this target can emit. Two target
    # families were built in parallel on this interface -- one adding
    # architectures (Arduino, AVR), one adding peripherals (PWM, tone, serial,
    # whole-port I/O, tables, parts) -- and they met here. A target that does
    # not list a feature refuses it BY NAME at the declaration or statement
    # that asked for it, rather than failing with an AttributeError three
    # layers down inside the emitter, which is what would otherwise happen the
    # first time somebody wrote `set led to 50 percent` for an Arduino.
    supports: frozenset = frozenset()

    # Whether the serial baud rate comes from somewhere OTHER than the timer a
    # TONE pin needs. It is an 8051 contention: on the STC89 the baud rate is
    # Timer 1 and so is the tone, in different modes, so a program cannot both
    # print and sound a note. The STC12 has a dedicated baud-rate timer and is
    # fine. Any target whose console and tone do not share one timer -- which
    # is every non-8051 one -- leaves this True and the check passes.
    baud_from_brt = True

    # Set when the DEVICE name is real and understood but no code generator
    # exists for it yet -- the reason, in the words the person at the keyboard
    # needs. A name with no generator has to refuse at the DEVICE line: the
    # alternative is what the EATER6502 did until 2026-09-02, which was to
    # borrow the nearest generator and emit confident, well-formed code for
    # entirely the wrong architecture.
    pseudocode_gap: str | None = None

    # Which compiler turns this target's output into an image. Transpiling is
    # free; compiling is not, and the hosted service vendors SDCC only. Saying
    # so here lets the caller refuse clearly instead of handing Arduino C++ to
    # `sdcc -mmcs51` and reporting whatever it makes of that.
    toolchain = "sdcc-mcs51"

    # What to tell someone whose device transpiles here but cannot be built
    # here. Target-specific, because "use the other device" is good advice for
    # an Arduino and nonsense for a micro:bit.
    compile_hint = ""

    # What CLOCK means when the program does not say. 11.0592 MHz is an 8051
    # crystal chosen because it divides into exact UART baud rates -- it is
    # not a sensible default for a board that has never seen one, and
    # inheriting it made `DEVICE ATMEGA328P:` without a CLOCK line fail with a
    # complaint about millisecond division that never named the real problem.
    default_clock = 11059200

    # Which ISP protocol the part's bootloader speaks, where that is a
    # question at all. The browser flasher implements "stc12" only, and the
    # STC15 and STC89 families are genuinely different protocols rather than
    # dialects of it -- so naming this lets the page refuse up front instead
    # of failing after the user has already power-cycled the board.
    isp_protocol = None

    # Extension for the generated source when it is handed back as a file.
    # Not always "c": Arduino core source wants .ino so the IDE opens it as a
    # sketch, and a micro:bit target emits Python.
    source_extension = "c"

    # The C type a millisecond count lives in, and its signed counterpart for
    # the scheduler's wraparound-safe deadline compare. 16 bits is right for a
    # Timer-0 counter we increment ourselves; a target whose clock is the
    # core's `millis()` gets a 32-bit one whether it wants it or not, and
    # casting that to a 16-bit int would make every deadline past 32 s wrong.
    time_type = "unsigned int"
    time_signed = "int"

    # The shortest wait this target can actually produce, in milliseconds, and
    # the name to blame in the refusal. Every target's delay primitive counts
    # whole milliseconds today, so the floor is 1 -- but it is stated here
    # rather than assumed in the portable layer because it is precisely a fact
    # about the target's clock, and a board with a microsecond delay would set
    # it lower. A wait under the floor is refused by name: rounding it up
    # invents time the user did not ask for, and rounding it down (which is
    # what unguarded arithmetic does) deletes the wait entirely and leaves a
    # program that compiles, flashes, and does something else.
    wait_floor_ms = 1.0
    wait_floor_reason = "the millisecond delay"

    # ---- pins -----------------------------------------------------------
    def shift_helper(self, part) -> list[str]:
        """The 74HC595 bit-banger, in terms of this target's pin writes.

        Nothing about shifting a byte out on three pins is chip-specific --
        the part is specified into the tens of megahertz and has no minimum
        clock period any of these cores could violate, so only the ORDER of
        the edges matters. That is why one implementation serves every target
        that can write a pin at all.
        """
        return [
            f"/* {part.kind.upper()}: eight outputs for three pins. Data is",
            " * sampled on the rising edge of the shift clock, and the latch",
            " * transfers on its own rising edge.",
            " *",
            " * MSB first, so the byte reads left to right on the outputs. */",
            f"static void bw_part_{part.name}(unsigned char value)",
            "{",
            "    unsigned char i;",
            "    " + self.write_pin(part.clock, False),
            "    " + self.write_pin(part.latch, False),
            "    for (i = 0; i < 8; i++) {",
            "        if (value & 0x80) { " + self.write_pin(part.data, True) + " }",
            "        else { " + self.write_pin(part.data, False) + " }",
            "        value = (unsigned char)(value << 1);",
            "        " + self.write_pin(part.clock, True),
            "        " + self.write_pin(part.clock, False),
            "    }",
            "    " + self.write_pin(part.latch, True)
            + "      /* transfer to the outputs */",
            "    " + self.write_pin(part.latch, False),
            "}",
            "",
        ]

    def resolve_port(self, program, name, where, direction, active_low,
                     line: int) -> "Port":
        """Turn a whole-port declaration's location token into a Port.

        Only reached by targets whose `supports` includes "port"; the others
        are refused by name before this is called.
        """
        raise NotImplementedError

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

    # A target whose language cannot express the shared lowering overrides
    # this and emits the whole program itself. MicroPython is the case: no
    # goto, so the Duff's-device state machines are unavailable and the
    # cooperative tasks become generators instead. Targets that leave it None
    # get the C back end below.
    emit = None

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
    supports = frozenset({"pwm", "tone", "print", "port", "table", "part",
                          "keypad", "matrix"})

    """The 8051 families, which differ from each other only in three flags.

    An STC12C5A60S2 drops into an STC89C52 socket pin-for-pin, but the 1T core
    runs software delay loops 6-12x too fast. Our generated code never
    busy-waits -- every delay and every scheduler tick is Timer 0 at FOSC/12,
    which both families count identically -- so the same pseudocode is
    timing-correct on either chip.
    """

    def __init__(self, key: str, display: str, header: str,
                 port_modes: bool, aux_1t_bit: bool, adc: bool, pwm: bool = False,
                 p5: bool = False):
        self.key = key
        self.display = display
        self.header = header        # the SDCC header with this family's registers
        self.port_modes = port_modes  # PxM0/PxM1 exist (STC12); STC89 is quasi-bidi
        self.aux_1t_bit = aux_1t_bit  # AUXR.7 selects T0 1T mode and must be cleared
        self.adc = adc                # 10-bit ADC on P1 (STC12 only)
        self.pwm = pwm                # PCA capture/compare modules with PWM mode
        self.p5 = p5                  # port 5 exists (STC15; P5.4/P5.5 on DIP-40)
        # Where UART1's baud rate comes from. The STC12 has a dedicated
        # baud-rate timer; the STC89 has to spend Timer 1 on it, which is the
        # same Timer 1 a TONE pin wants -- so on that family the two features
        # are mutually exclusive, and saying so is better than a silent
        # fight over TMOD.
        self.baud_from_brt = port_modes
        # The same classification stcgal uses, which is by model NAME rather
        # than by magic: STC12C5A60S2 and STC12C5A16S2 speak "stc12", the
        # STC15 and STC89 families do not.
        if re.match(r"stc(89|90)(c|le)\d", key):
            self.isp_protocol = "stc89"
        elif re.match(r"(stc|iap|irc)15\D", key):
            self.isp_protocol = "stc15"
        elif re.match(r"(stc|iap)(10|11|12)\D", key):
            self.isp_protocol = "stc12"

    # ---- pins -----------------------------------------------------------
    def resolve_port(self, program, name, where, direction, active_low, line):
        match = re.fullmatch(r"p([0-4])", where, re.I)
        if not match:
            raise PseudocodeError(
                line, f"{where.upper()} is not a port on the {self.display}; "
                      "use P0 to P4")
        number = int(match.group(1))
        # One SFR both ways on an 8051: reading P2 gives the pins, writing it
        # drives them.
        return Port(name, number, direction, active_low, where=f"P{number}",
                    write_sfr=f"P{number}", read_sfr=f"P{number}")

    def resolve_pin(self, program, name, where, direction, active_low, line):
        match = PORT_RE.match(where)
        if not match:
            raise PseudocodeError(
                line, f"{where.upper()} is not a pin on the {self.display}; "
                      "use P0.0 to P4.7")
        port, bit = int(match.group(1)), int(match.group(2))

        # P5 is an STC15 port (STC15-PERIPHERAL-MODEL.md par.3). On parts
        # without it the pin does not exist; on the STC15 DIP-40 only
        # P5.4 (RST-shared) and P5.5 are bonded -- the RBS15667 console's
        # buzzer is P5.5, which is why this stopped being hypothetical.
        if port == 5:
            if not getattr(self, "p5", False):
                raise PseudocodeError(
                    line, f"P5.{bit} does not exist on the {self.display}; "
                          "port 5 is an STC15 feature (STC15-PERIPHERAL-MODEL.md)")
            if bit not in (4, 5):
                raise PseudocodeError(
                    line, f"P5.{bit} is not bonded on the DIP-40; "
                          "only P5.4 and P5.5 reach pins")

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
            if f"P{port}.{bit}" in prev.claimed_where:
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
            return f'bw_print("{_c_string(node.text)}");'
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
        supplement = []
        if self.p5:
            # The STC15 supplement -- everything the STC15 has that SDCC's
            # stc12.h does not declare, emitted for EVERY STC15 program so
            # the header story is complete, never patched per feature.
            # Deduped against the shipped stc12.h (SDCC 4.5.0): it already
            # carries P5/P5M0/P5M1 at the STC15's addresses but stops the
            # sbits at P5_3. Addresses: STC15-PERIPHERAL-MODEL.md par.3.
            # sb3-creator emits the identical block; this file is the
            # reference implementation, so the two must not drift.
            supplement = [
                "/* STC15 supplement -- registers stc12.h lacks (STC15-PERIPHERAL-MODEL.md) */",
                "__sbit __at (0xCC) P5_4;      /* DIP-40 pin 17, RST-shared */",
                "__sbit __at (0xCD) P5_5;      /* DIP-40 pin 19 */",
                "__sbit __at (0xCE) P5_6;      /* not bonded on DIP-40 */",
                "__sbit __at (0xCF) P5_7;      /* not bonded on DIP-40 */",
                "__sfr  __at (0xD6) T2H;       /* Timer 2 -- the UART1 baud source */",
                "__sfr  __at (0xD7) T2L;",
                "__sfr  __at (0xBA) P_SW2;     /* peripheral pin switch 2 */",
                "__sfr  __at (0xAA) WKTCL;     /* wake-up timer */",
                "__sfr  __at (0xAB) WKTCH;",
                "__sfr  __at (0xDC) CCAPM2;    /* third PCA/CCP channel */",
                "__sfr  __at (0xEC) CCAP2L;",
                "__sfr  __at (0xFC) CCAP2H;",
                "__sfr  __at (0xF4) PCA_PWM2;",
                "#define P_SW1    AUXR1        /* STC15 name for 0xA2 */",
                "#define INT_CLKO WAKE_CLKO    /* STC15 name for 0x8F */",
                "",
            ]
        return [
            f"#include <{self.header}>",
            "",
            *supplement,
            f"#define FOSC_HZ {program.clock}UL",
            "",
            "/* Timer 0, mode 1, clocked at FOSC/12 -- accuracy depends only on",
            " * FOSC, and every supported family counts this mode identically, so",
            " * the same program is timing-correct on a 12T STC89 and a 1T STC12",
            " * or STC15. Nothing in the generated code ever busy-waits. */",
            "#define T0_RELOAD (65536UL - (FOSC_HZ / 12UL / 1000UL))",
            "",
        ]

    # ---- MATRIX8X8: frame buffer, ISR scan hook, drawing helpers --------
    #
    # Packing (documented once, here): the 8x8 frame is BIT-PLANE packed,
    # MATRIX_PLANES planes of 8 row-bytes. Plane p, row y is bw_scr_<name>[p*8+y];
    # within a byte bit(7-x) is column x (bit7 = left). A pixel's brightness LEVEL
    # is the little-endian bits across the planes -- 2 planes = 4 levels
    # (MATRIX_LEVELS: 0 off .. 3 full), 16 bytes total. Widening MATRIX_PLANES to 4
    # gives 16 levels / 32 bytes without touching any verb. Threshold rendering
    # (this landing) lights a pixel iff its level != 0, which is exactly the OR of
    # the plane bytes -- one instruction, no per-pixel loop, in the ISR.

    def _matrix_state(self, matrices) -> list[str]:
        """Buffer, cursor, dim and row-select table -- emitted BEFORE bw_tick,
        which reads them. #defines are emitted once, not per matrix."""
        out = [
            "/* MATRIX8X8 brightness depth. 2 planes = 4 levels; the whole",
            " * surface (buffer size, every verb) is written in terms of these,",
            " * so widening to 4 planes / 16 levels is a one-line change. */",
            "#define MATRIX_PLANES 2",
            "#define MATRIX_LEVELS 4              /* 1 << MATRIX_PLANES */",
            "",
            "/* Clamp a (signed) level to 0..MATRIX_LEVELS-1. */",
            "static unsigned char bw_scr_level(int v)",
            "{",
            "    if (v < 0) return 0;",
            "    if (v > MATRIX_LEVELS - 1) return MATRIX_LEVELS - 1;",
            "    return (unsigned char)v;",
            "}",
            "",
        ]
        for part in matrices:
            n = part.name
            out += [
                f"/* {n}: 8x8 bit-plane frame buffer (see the packing note above).",
                " * The Timer-0 ISR is the SOLE writer of the 595 and the column",
                " * port; mainline only ever writes this RAM. */",
                f"static unsigned char bw_scr_{n}[8 * MATRIX_PLANES];",
                f"static unsigned char bw_scr_{n}_scan;                 "
                "/* row cursor 0..7 */",
                f"static unsigned char bw_scr_{n}_phase;                "
                "/* BCM phase 0..MATRIX_LEVELS-2 */",
                f"static unsigned char bw_scr_{n}_dim = MATRIX_LEVELS - 1;  "
                "/* global brightness */",
                "/* Row select, active-high, Q7 = top: row y is 595 output Q(7-y)",
                " * == bit (0x80 >> y). A table so the ISR shifts no variable. */",
                f"static const __code unsigned char bw_scr_{n}_rowbit[8] =",
                "    { 0x80, 0x40, 0x20, 0x10, 0x08, 0x04, 0x02, 0x01 };",
                "",
            ]
        return out

    def _matrix_scan(self, part) -> list[str]:
        """The per-tick scan, spliced into bw_tick AFTER bw_ms++. One row per
        tick, table-driven, no mul/div."""
        n = part.name
        data, clock, latch = part.data.sfr, part.clock.sfr, part.latch.sfr
        col = part.col_port.sfr
        return [
            "",
            f"    /* MATRIX8X8 '{n}': advance one row (8 rows -> 125 Hz). The 595",
            "     * selects the row active-high (Q7=top); columns are active-low. */",
            "    {",
            "        unsigned char bw_lit, bw_rb, bw_i;",
            f"        {col} = 0xFF;                          "
            "/* blank columns during the row change */",
            f"        bw_rb = bw_scr_{n}_rowbit[bw_scr_{n}_scan];",
            f"        {latch} = 0;",
            "        for (bw_i = 0; bw_i < 8; bw_i++) {   /* clock the byte in, MSB first */",
            f"            {data} = (bw_rb & 0x80) ? 1 : 0;",
            "            bw_rb = (unsigned char)(bw_rb << 1);",
            f"            {clock} = 1; {clock} = 0;",
            "        }",
            f"        {latch} = 1; {latch} = 0;              "
            "/* transfer to the 595 outputs */",
            "        /* Grayscale by bit-plane phase render (BCM). Over a cycle of",
            "         * MATRIX_LEVELS-1 phases a pixel of level L is lit in L of them,",
            "         * so its duty is L/(MATRIX_LEVELS-1): 0, 1/3, 2/3, 1 for the 4",
            "         * levels. The phase mask says 'level > phase', read straight off",
            "         * the two bit-planes p0 (LSB) and p1 (MSB):",
            "         *   phase 0: level>=1 = p0 | p1",
            "         *   phase 1: level>=2 = p1",
            "         *   phase 2: level>=3 = p0 & p1",
            f"         * The global dim caps every pixel at min(level, bw_scr_{n}_dim):",
            "         * a phase renders only while dim > phase. Table-free, no mul/div;",
            "         * still one row per tick. (The masks are 2-plane specific -- a",
            "         * widen to 4 planes/16 levels generalizes them to a level compare.) */",
            f"        if (bw_scr_{n}_dim > bw_scr_{n}_phase) {{",
            f"            unsigned char bw_p0 = bw_scr_{n}[bw_scr_{n}_scan];",
            f"            unsigned char bw_p1 = bw_scr_{n}[bw_scr_{n}_scan + 8];",
            f"            if (bw_scr_{n}_phase == 0) bw_lit = (unsigned char)(bw_p0 | bw_p1);",
            f"            else if (bw_scr_{n}_phase == 1) bw_lit = bw_p1;",
            "            else bw_lit = (unsigned char)(bw_p0 & bw_p1);",
            "        } else {",
            "            bw_lit = 0;",
            "        }",
            f"        {col} = (unsigned char)~bw_lit;        "
            "/* active-low columns: lit -> 0 */",
            "        /* Advance the row; a completed frame steps the BCM phase. The",
            "         * phase cycle is MATRIX_LEVELS-1 frames long (3 frames = 24 ms",
            "         * = ~42 Hz grayscale cycle at 8 ms/frame; the anti-flicker timer",
            "         * choice is a bench decision, this is the duty-correct reference). */",
            f"        bw_scr_{n}_scan++;",
            f"        if (bw_scr_{n}_scan >= 8) {{",
            f"            bw_scr_{n}_scan = 0;",
            f"            bw_scr_{n}_phase++;",
            f"            if (bw_scr_{n}_phase >= MATRIX_LEVELS - 1) bw_scr_{n}_phase = 0;",
            "        }",
            "    }",
        ]

    def _matrix_helpers(self, part) -> list[str]:
        """The drawing verbs' C helpers -- all write the RAM frame buffer only.
        Emitted with the other per-part helpers, after bw_tick."""
        n = part.name
        return [
            f"/* Drawing verbs for MATRIX8X8 '{n}'. All write the RAM frame buffer;",
            " * the Timer-0 ISR scans it. x = column 0..7 (left->right), y = row",
            " * 0..7 (top->bottom). bit7 of a row byte is the LEFT column, matching",
            " * the image literals and the column wiring. */",
            f"static void bw_scr_{n}_clear(void)",
            "{",
            "    unsigned char i;",
            f"    for (i = 0; i < 8 * MATRIX_PLANES; i++) bw_scr_{n}[i] = 0;",
            "}",
            "",
            f"static void bw_scr_{n}_setpx(unsigned char x, unsigned char y, "
            "unsigned char level)",
            "{",
            "    unsigned char m, p;",
            "    if (x > 7 || y > 7) return;",
            "    m = (unsigned char)(0x80 >> x);            /* bit7 = left */",
            "    for (p = 0; p < MATRIX_PLANES; p++) {",
            f"        if (level & 1) bw_scr_{n}[y + (unsigned char)(p << 3)] |=  m;",
            f"        else           bw_scr_{n}[y + (unsigned char)(p << 3)] &= "
            "(unsigned char)~m;",
            "        level = (unsigned char)(level >> 1);",
            "    }",
            "}",
            "",
            f"static unsigned char bw_scr_{n}_getpx(unsigned char x, unsigned char y)",
            "{",
            "    unsigned char m, p, level = 0;",
            "    if (x > 7 || y > 7) return 0;",
            "    m = (unsigned char)(0x80 >> x);",
            "    for (p = 0; p < MATRIX_PLANES; p++)",
            f"        if (bw_scr_{n}[y + (unsigned char)(p << 3)] & m) "
            "level |= (unsigned char)(1 << p);",
            "    return level;",
            "}",
            "",
            "/* A whole row from an 8-bit image byte: bit7 = left, 1 -> full, 0 -> off. */",
            f"static void bw_scr_{n}_row(unsigned char y, unsigned char bits)",
            "{",
            "    unsigned char p;",
            "    if (y > 7) return;",
            f"    for (p = 0; p < MATRIX_PLANES; p++) "
            f"bw_scr_{n}[y + (unsigned char)(p << 3)] = bits;",
            "}",
            "",
            "/* Blit 8 image bytes, top row first (the heart demo, one call). */",
            f"static void bw_scr_{n}_image(const __code unsigned char *img)",
            "{",
            "    unsigned char y;",
            f"    for (y = 0; y < 8; y++) bw_scr_{n}_row(y, img[y]);",
            "}",
            "",
            "/* Shift the whole frame one pixel; the vacated edge clears.",
            " * 0 left, 1 right, 2 up, 3 down. Left is toward x=0 == toward the",
            " * MSB, so a row byte shifts left. */",
            f"static void bw_scr_{n}_scroll(unsigned char dir)",
            "{",
            "    unsigned char p, y, base;",
            "    for (p = 0; p < MATRIX_PLANES; p++) {",
            "        base = (unsigned char)(p << 3);",
            "        if (dir == 0)",
            "            for (y = 0; y < 8; y++)",
            f"                bw_scr_{n}[base + y] = (unsigned char)(bw_scr_{n}[base + y] << 1);",
            "        else if (dir == 1)",
            "            for (y = 0; y < 8; y++)",
            f"                bw_scr_{n}[base + y] = (unsigned char)(bw_scr_{n}[base + y] >> 1);",
            "        else if (dir == 2) {",
            f"            for (y = 0; y < 7; y++) bw_scr_{n}[base + y] = bw_scr_{n}[base + y + 1];",
            f"            bw_scr_{n}[base + 7] = 0;",
            "        } else {",
            f"            for (y = 7; y != 0; y--) bw_scr_{n}[base + y] = bw_scr_{n}[base + y - 1];",
            f"            bw_scr_{n}[base] = 0;",
            "        }",
            "    }",
            "}",
            "",
        ]

    def _sevenseg_isr_lines(self, program):
        lines = []
        for part in program.parts.values():
            if not isinstance(part, SevenSegPart):
                continue
            ss = part
            a, b, c = ss.sel_pins
            seg_write = f"P{ss.seg_port}"
            lines += [
                f"    /* {ss.name}: advance one digit */",
                f"    {seg_write} = 0x00;           /* blank during switch */",
                f"    {a.sfr} = bw_{ss.name}_cur & 0x01 ? 1 : 0;",
                f"    {b.sfr} = bw_{ss.name}_cur & 0x02 ? 1 : 0;",
                f"    {c.sfr} = bw_{ss.name}_cur & 0x04 ? 1 : 0;",
            ]
            if ss.common_anode:
                lines.append(f"    {seg_write} = (unsigned char)"
                             f"~bw_{ss.name}_fb[bw_{ss.name}_cur];")
            else:
                lines.append(f"    {seg_write} = "
                             f"bw_{ss.name}_fb[bw_{ss.name}_cur];")
            lines.append(
                f"    bw_{ss.name}_cur = (bw_{ss.name}_cur + 1) & 0x07;")
        return lines

    def _ledbank_isr_lines(self, program):
        lines = []
        for part in program.parts.values():
            if not isinstance(part, LedBankPart):
                continue
            lb = part
            port_sfr = f"P{lb.led_port}"
            if lb.active_low:
                lines.append(f"    {port_sfr} = (unsigned char)"
                             f"~bw_{lb.name}_shadow;  /* LEDs active low */")
            else:
                lines.append(f"    {port_sfr} = bw_{lb.name}_shadow;")
        return lines

    def runtime(self, program, tasks):
        out = []
        matrices = [p for p in program.parts.values() if isinstance(p, MatrixPart)]
        has_sevenseg = program.has_sevenseg
        has_ledbank = program.has_ledbank
        needs_isr = tasks or matrices or has_sevenseg or has_ledbank

        if matrices:
            out += self._matrix_state(matrices)

        if has_sevenseg:
            out += [
                "/* 7-segment font: 0-9, A-F. Common-cathode segment encoding:",
                " *   bit 0 = a (top), 1 = b (upper-right), 2 = c (lower-right),",
                " *   3 = d (bottom), 4 = e (lower-left), 5 = f (upper-left),",
                " *   6 = g (middle), 7 = dp (decimal point). */",
                "static const __code unsigned char bw_7seg_font[16] = {",
                "    0x3F, 0x06, 0x5B, 0x4F, 0x66, 0x6D, 0x7D, 0x07,",
                "    0x7F, 0x6F, 0x77, 0x7C, 0x39, 0x5E, 0x79, 0x71",
                "};",
                "",
            ]
            for part in program.parts.values():
                if isinstance(part, SevenSegPart):
                    out += [
                        f"/* {part.name}: 8-digit frame buffer and scan cursor. */",
                        f"static unsigned char bw_{part.name}_fb[8];",
                        f"static unsigned char bw_{part.name}_cur;",
                        "",
                    ]

        if has_ledbank:
            for part in program.parts.values():
                if isinstance(part, LedBankPart):
                    out += [
                        f"/* {part.name}: LED shadow byte — the ISR is the sole "
                        f"port writer. */",
                        f"static unsigned char bw_{part.name}_shadow;",
                        "",
                    ]

        if needs_isr:
            tick = [
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
            ]
            for part in matrices:
                tick += self._matrix_scan(part)
            tick += self._sevenseg_isr_lines(program)
            tick += self._ledbank_isr_lines(program)
            tick += [
                "}",
                "",
            ]
            if tasks:
                tick += [
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
                # ISR parts without cooperative tasks: delay uses bw_ms counter.
                tick += [
                    "static void delay_ms(unsigned int ms)",
                    "{",
                    "    unsigned int start;",
                    "    ET0 = 0; start = bw_ms; ET0 = 1;",
                    "    while (ms--) {",
                    "        for (;;) {",
                    "            unsigned int now;",
                    "            ET0 = 0; now = bw_ms; ET0 = 1;",
                    "            if (now != start) break;",
                    "        }",
                    "        ET0 = 0; start = bw_ms; ET0 = 1;",
                    "    }",
                    "}",
                    "",
                ]
            out += tick
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
            if isinstance(part, MatrixPart):
                out += self._matrix_helpers(part)
                continue
            if isinstance(part, SevenSegPart):
                n = part.name
                out += [
                    f"/* {n}: show a decimal number right-aligned across 8 digits. */",
                    f"static void bw_{n}_show_number(int n)",
                    "{",
                    "    unsigned char i, neg = 0;",
                    "    unsigned int u;",
                    f"    for (i = 0; i < 8; i++) bw_{n}_fb[i] = 0x00;",
                    "    if (n < 0) { neg = 1; u = (unsigned int)(-n); }",
                    "    else       { u = (unsigned int)n; }",
                    "    i = 7;",
                    "    do {",
                    f"        bw_{n}_fb[i] = bw_7seg_font[u % 10];",
                    "        u /= 10;",
                    "        if (i == 0) break;",
                    "        i--;",
                    "    } while (u);",
                    "    if (neg && i > 0)",
                    f"        bw_{n}_fb[i - 1] = 0x40;  /* minus = segment g */",
                    "}",
                    "",
                    f"static void bw_{n}_show_digit(unsigned char d, unsigned char v)",
                    "{",
                    "    if (d > 7) return;",
                    f"    bw_{n}_fb[d] = bw_7seg_font[v & 0x0F];",
                    "}",
                    "",
                    f"static void bw_{n}_set_segments(unsigned char d, unsigned char segs)",
                    "{",
                    "    if (d > 7) return;",
                    f"    bw_{n}_fb[d] = segs;",
                    "}",
                    "",
                    f"static void bw_{n}_clear(void)",
                    "{",
                    "    unsigned char i;",
                    f"    for (i = 0; i < 8; i++) bw_{n}_fb[i] = 0x00;",
                    "}",
                    "",
                ]
                continue
            if isinstance(part, LedBankPart):
                n = part.name
                out += [
                    f"/* {n}: LED helpers — writes go through the shadow byte. */",
                    f"static void bw_{n}_on(unsigned char n)",
                    "{",
                    f"    if (n > 7) return;",
                    f"    bw_{n}_shadow |= (unsigned char)(1 << n);",
                    "}",
                    "",
                    f"static void bw_{n}_off(unsigned char n)",
                    "{",
                    f"    if (n > 7) return;",
                    f"    bw_{n}_shadow &= (unsigned char)~(1 << n);",
                    "}",
                    "",
                    f"static void bw_{n}_set(unsigned char pattern)",
                    "{",
                    f"    bw_{n}_shadow = pattern;",
                    "}",
                    "",
                    f"static void bw_{n}_only(unsigned char n)",
                    "{",
                    f"    bw_{n}_shadow = (n > 7) ? 0 : (unsigned char)(1 << n);",
                    "}",
                    "",
                ]
                continue
            if isinstance(part, KeypadPart):
                out += [
                    f"/* {part.name}: a 4x4 matrix keypad, sixteen keys for eight pins.",
                    " *",
                    " * The scan drives one row low and reads the columns — the",
                    " * scanner verified on Prechin A2 silicon (06-matrix89 mapped it,",
                    " * 09-keyshow89 consumed it). Idle rows sit quasi-high, so the",
                    " * two-keys-in-one-column short is current-limited by the port's",
                    " * weak pull-up. The nops respect the 1T core's 4-clock I/O",
                    " * read-back (a 12T core just wastes two cycles). */",
                    f"static signed char bw_part_{part.name}_read(void)",
                    "{",
                ]
                for r, rpin in enumerate(part.rows):
                    out.append(f"    {rpin.sfr} = 0;")
                    out.append("    __asm__(\"nop\"); __asm__(\"nop\");")
                    for c, cpin in enumerate(part.cols):
                        out.append(f"    if (!{cpin.sfr}) {{ {rpin.sfr} = 1; "
                                   f"return {r * 4 + c}; }}")
                    out.append(f"    {rpin.sfr} = 1;")
                out += [
                    "    return -1;",
                    "}",
                    "",
                ]
                continue
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
                f"    {part.clock.sfr} = 0;",
                f"    {part.latch.sfr} = 0;",
                "    for (i = 0; i < 8; i++) {",
                f"        {part.data.sfr} = (value & 0x80) ? 1 : 0;",
                "        value = (unsigned char)(value << 1);",
                f"        {part.clock.sfr} = 1;",
                f"        {part.clock.sfr} = 0;",
                "    }",
                f"    {part.latch.sfr} = 1;      /* transfer to the outputs */",
                f"    {part.latch.sfr} = 0;",
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
            for claimed in part.claimed:
                outputs[claimed.port] = outputs.get(claimed.port, 0) | claimed.mask
            if isinstance(part, SevenSegPart):
                outputs[part.seg_port] = outputs.get(part.seg_port, 0) | 0xFF
            if isinstance(part, LedBankPart):
                outputs[part.led_port] = outputs.get(part.led_port, 0) | 0xFF
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

    def main(self, program, setup_lines, body_lines, task_names):
        has_isr_parts = (program.has_matrix or program.has_sevenseg
                         or program.has_ledbank)
        out = ["void main(void)", "{"] + setup_lines
        if task_names:
            out += self.start_scheduler(task_names)
        elif has_isr_parts:
            out += [
                "",
                "    TL0 = (unsigned char)(T0_RELOAD & 0xFF);",
                "    TH0 = (unsigned char)(T0_RELOAD >> 8);",
                "    ET0 = 1;                       /* millisecond tick */",
                "    EA  = 1;",
                "    TR0 = 1;",
                "",
            ]
            out += body_lines
        else:
            out.append("")
            out += body_lines
        return out + ["}", ""]


ARDUINO_PIN_RE = re.compile(r"^(?:d(\d{1,2})|a(\d{1,2})|(\d{1,2}))$", re.I)

# The three timers bring six compare outputs to the header. Everything else is
# digital-only, exactly as the PCA pins are on the STC12.
ARDUINO_PWM_PINS = {3, 5, 6, 9, 10, 11}
# tone() is Timer 2, and Timer 2 is what drives PWM on D3 and D11.
ARDUINO_TONE_STEALS = {3, 11}


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
    default_clock = 16000000

    # PORT stays out: eight bits of one register is exactly what the core
    # hides behind digitalWrite, and reaching past it would give up the
    # portability that is the reason to emit core C++ at all. A PART needs
    # only three pins the core is happy to drive.
    supports = frozenset({"pwm", "tone", "print", "table", "part"})
    compile_hint = ("DEVICE ATMEGA328P: is the same board without the Arduino "
                    "core, and that one does compile here.")
    source_extension = "ino"

    def __init__(self, key: str, display: str, digital_max: int, analog_max: int,
                 analog_only: frozenset = frozenset()):
        self.key = key
        self.display = display
        self.digital_max = digital_max
        self.analog_max = analog_max
        # Analog inputs with no digital buffer behind them. On the Nano's
        # TQFP/QFN package, ADC6 and ADC7 are exactly that: digitalWrite to
        # one does nothing at all, quietly, which is the worst way for a pin
        # to be wrong.
        self.analog_only = analog_only

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
            # An analog pin is usually a perfectly good digital one -- but not
            # always, and where it is not, saying so beats a pin that reads as
            # working and does nothing.
            if direction != "analog" and number in self.analog_only:
                raise PseudocodeError(
                    line, f"A{number} on the {self.display} is analog-IN only: "
                          f"the package brings out the ADC channel without a "
                          f"digital buffer, so it cannot be an "
                          f"{direction.upper()}. Use A0-A5 or a D pin.")
            return ArduinoPin(name, f"A{number}", direction, active_low,
                              f"A{number}")

        number = int(digital if digital is not None else bare)
        if number > self.digital_max:
            raise PseudocodeError(
                line, f"the {self.display} has D0-D{self.digital_max}, "
                      f"not D{number}")
        if direction == "pwm" and number not in ARDUINO_PWM_PINS:
            usable = ", ".join(f"D{n}" for n in sorted(ARDUINO_PWM_PINS))
            raise PseudocodeError(
                line, f"PWM on the {self.display} is only on the timer compare "
                      f"outputs: {usable}. D{number} is digital-only.")
        if direction == "tone":
            # tone() drives one pin at a time, and it is Timer 2 -- the same
            # timer behind PWM on D3 and D11. Both are silent breakage if left
            # to run: a second tone replaces the first, and the PWM pin simply
            # stops fading.
            for other in program.pins.values():
                if other.direction == "tone":
                    raise PseudocodeError(
                        line, f"only one TONE pin ({other.name!r} already has "
                              f"it): the Arduino core plays one tone at a time")
                if other.direction == "pwm" and other.ref.isdigit() \
                        and int(other.ref) in ARDUINO_TONE_STEALS:
                    raise PseudocodeError(
                        line, f"a TONE pin and PWM on D{other.ref} both need "
                              f"Timer 2 -- {other.name!r} would stop fading "
                              f"while a note sounds. Move it to D5, D6, D9 "
                              f"or D10.")
        if direction == "pwm" and number in ARDUINO_TONE_STEALS:
            for other in program.pins.values():
                if other.direction == "tone":
                    raise PseudocodeError(
                        line, f"PWM on D{number} and the TONE pin "
                              f"{other.name!r} both need Timer 2. Use D5, D6, "
                              f"D9 or D10 for PWM instead.")
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

    def write_pwm(self, pin, value: str) -> str:
        # analogWrite takes 0-255 as the proportion of time the PIN is high,
        # while the AST stores the percentage of time the LOAD is on. The
        # outer parentheses matter: `100 - x * 255 / 100` binds the wrong way.
        duty = f"(100 - ({value}))" if pin.active_low else f"({value})"
        return f"analogWrite({pin.ref}, ({duty} * 255) / 100);"

    def write_tone(self, pin, hz: str) -> str:
        # Through a helper because 0 Hz means silence and the frequency is an
        # expression, so the choice is a run-time one and this hook may only
        # return a single statement.
        return f"bw_tone({pin.ref}, {hz});"

    def write_print(self, node) -> str:
        if node.value is None:
            return f'Serial.println("{_c_string(node.text)}");'
        return f"Serial.println({node.value});"

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
        # Almost nothing to emit: the timebase, the blocking delay, the ADC,
        # tone() and Serial are all in the core already. What is left is the
        # handful of helpers the shared walkers expect by name.
        out: list[str] = []
        if program.tables:
            out += [
                "/* Lookup tables. `const` on an AVR still costs RAM -- the",
                " * Harvard split means a plain const array is copied out of",
                " * flash at startup -- but PROGMEM would need pgm_read_byte at",
                " * every use, and the index expression is shared with the",
                " * other targets. A font is tens of bytes; a big table is the",
                " * case that would need a target hook for reads. */",
            ]
            for name, values in program.tables.items():
                body = ", ".join(f"0x{v:02X}" for v in values)
                out.append(f"static const unsigned char bw_tab_{name}[] "
                           f"= {{ {body} }};")
            out += [
                "",
                "/* A computed index is clamped rather than trusted: reading",
                " * past a table gives a plausible-looking wrong byte. */",
                "static unsigned char bw_clamp(int i, unsigned char last)",
                "{",
                "    if (i < 0) return 0;",
                "    if (i > (int)last) return last;",
                "    return (unsigned char)i;",
                "}",
                "",
            ]
        if program.tone_pin is not None:
            out += [
                "/* 0 Hz means silence, and the frequency is an expression, so",
                " * the choice has to be made at run time. */",
                "static void bw_tone(unsigned char pin, unsigned int hz)",
                "{",
                "    if (hz) tone(pin, hz); else noTone(pin);",
                "}",
                "",
            ]
        for part in program.parts.values():
            out += self.shift_helper(part)
        return out

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
            elif pin.direction == "pwm":
                out.append(f"    pinMode({pin.ref}, OUTPUT);")
            # analog needs no pinMode (analogRead configures the mux), and
            # neither does tone (tone() drives the pin itself).
        for part in program.parts.values():
            for claimed in part.claimed:
                out.append(f"    pinMode({claimed.ref}, OUTPUT);"
                           f"   /* {part.name} */")
        for pin in program.pins.values():
            if pin.direction == "output":
                level = "HIGH" if pin.active_low else "LOW"
                out.append(f"    digitalWrite({pin.ref}, {level});"
                           f"   /* {pin.name} off */")
            elif pin.direction == "pwm":
                # Start at the off end, whichever end that is.
                out.append("    " + self.write_pwm(pin, "0")
                           + f"   /* {pin.name} off */")
        if program.uses_uart:
            out.append(f"    Serial.begin({BW_BAUD});")
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


# ------------------------------------------------------------------ bare AVR

# The ATmega328P as the board silkscreen labels it. D8-D13 are port B, D0-D7
# port D, A0-A5 port C -- an ordering that looks arbitrary because it is: it
# follows the physical layout of the DIP package, not the ports.
AVR_328P_PINS = {
    **{f"D{n}": ("D", n) for n in range(8)},
    **{f"D{8 + n}": ("B", n) for n in range(6)},
    **{f"A{n}": ("C", n) for n in range(6)},
}
AVR_328P_BY_PORT = {location: label for label, location in AVR_328P_PINS.items()}

AVR_PIN_RE = re.compile(r"^(?:([da])(\d{1,2})|p([a-l])(\d))$", re.I)

# Timer 0 prescalers, smallest first, with their CS02:CS00 bits. The tick wants
# an EXACT millisecond, so the emitter picks the first prescaler that divides
# the clock evenly into 1 kHz and still fits an 8-bit compare register.
# Which timer drives which compare output, and therefore which pins can do
# PWM at all. D5 and D6 are OC0B/OC0A -- Timer 0 -- and Timer 0 is the
# millisecond tick, so they are NOT offered: taking them would silently stop
# every wait in the program.
AVR_PWM_PINS = {
    9:  ("OCR1A", "TCCR1A", "COM1A1", 1),
    10: ("OCR1B", "TCCR1A", "COM1B1", 1),
    11: ("OCR2A", "TCCR2A", "COM2A1", 2),
    3:  ("OCR2B", "TCCR2A", "COM2B1", 2),
}
AVR_TICK_PINS = {5: "OC0B", 6: "OC0A"}
# The tone is Timer 1 in CTC mode toggling OC1A, which is D9 and only D9.
AVR_TONE_PIN = 9

AVR_PRESCALERS = [(1, "_BV(CS00)"), (8, "_BV(CS01)"),
                  (64, "_BV(CS01) | _BV(CS00)"), (256, "_BV(CS02)"),
                  (1024, "_BV(CS02) | _BV(CS00)")]


@dataclass
class AvrPin(Pin):
    """A port letter and a bit, plus the ADC channel where there is one."""
    port: str = ""
    bit: int = 0
    channel: int | None = None


class AvrTarget(Target):
    """ATmega parts compiled by avr-gcc, with no Arduino core underneath.

    Same boards as the Arduino target -- an ATmega328P *is* an Uno -- and
    deliberately not the same output. The core's digitalWrite looks up the
    port in a PROGMEM table and checks whether it has to disable a PWM channel,
    on every call, at runtime. This generator already knows the pin at emit
    time, so the same statement becomes one instruction:

        turn on led   ->   PORTB |= _BV(PB5);

    That is the identical discipline the 8051 target uses, it removes the
    LGPL-licensed core from the output, and it is what lets this service
    compile the result with a ~25 MB vendored toolchain instead of a 250 MB
    one. Pins are still written the way the board is labelled (`D13`, `A0`),
    because that is what the silkscreen says; `PB5` is accepted too.
    """

    toolchain = "avr-gcc"
    default_clock = 16000000       # what an Uno, a Nano and a Pro Mini run at

    # Everything the 8051 has. PORT is PORTB/PORTC/PORTD, and a PART is three
    # ordinary output pins bit-banged in the right order.
    supports = frozenset({"pwm", "tone", "print", "table", "port", "part"})

    # Our own tick, so we choose the width -- but 16 bits would wrap every 65 s
    # and these deadlines are compared against a free-running counter, so it is
    # the same 32-bit choice millis() forces on the Arduino target.
    time_type = "unsigned long"
    time_signed = "long"

    # ---- Timer 0, which is NOT the same register set across the family -----
    #
    # Every wait in a generated program is measured against Timer 0 in CTC
    # mode at 1 kHz, so getting these names wrong does not degrade -- it
    # fails to compile, or worse, on a part where the name happens to exist
    # at a different address, it silently programs the wrong thing.
    #
    #   ATmega328P/168P   TCCR0A/WGM01 + TCCR0B/prescaler, mask in TIMSK0
    #   ATtiny85          identical, except the mask register is TIMSK
    #   ATtiny88          no TCCR0B and no WGM01 at all: the ATtiny48/88
    #                     Timer 0 puts the prescaler AND the CTC enable
    #                     (CTC0, bit 3) in the single TCCR0A
    #
    # Verified against the vendored avr-libc headers (avr/io<part>.h), which
    # are the same ones the compile will use.
    timsk = "TIMSK0"        # ATtiny85 spells it TIMSK
    tick_ctc_bit = "WGM01"  # ATtiny88 spells it CTC0
    tick_split = True       # False: one TCCR0A holds prescaler and CTC both

    def tick_setup(self, compare: int, prescaler_bits: str) -> list[str]:
        """Timer 0 in CTC mode at exactly 1 kHz, spelled for this part."""
        pad = " " * max(1, 20 - len(str(compare)))
        if self.tick_split:
            return [f"    TCCR0A = _BV({self.tick_ctc_bit});           /* CTC */",
                    f"    OCR0A  = {compare};{pad}/* 1 kHz */",
                    f"    {self.timsk} = _BV(OCIE0A);",
                    f"    TCCR0B = {prescaler_bits};"]
        # One register for both. Order matters: loading OCR0A and unmasking
        # before the clock select starts the timer means the first compare
        # cannot be missed.
        return [f"    OCR0A  = {compare};{pad}/* 1 kHz */",
                f"    {self.timsk} = _BV(OCIE0A);",
                f"    TCCR0A = _BV({self.tick_ctc_bit}) | {prescaler_bits};"
                f"   /* CTC + prescaler, one register */"]

    def __init__(self, key: str, display: str, mcu: str, flash: int):
        self.key = key
        self.display = display
        self.mcu = mcu              # what avr-gcc wants for -mmcu
        self.flash = flash          # bytes, for the size check after linking

    # ---- pins -----------------------------------------------------------
    def resolve_pin(self, program, name, where, direction, active_low, line):
        match = AVR_PIN_RE.match(where)
        label = None
        if match:
            kind, number, port, bit = match.groups()
            if kind:
                label = f"{kind.upper()}{int(number)}"
            else:
                label = AVR_328P_BY_PORT.get((port.upper(), int(bit)))
        if label is None or label not in AVR_328P_PINS:
            raise PseudocodeError(
                line, f"{where.upper()} is not a pin on the {self.display}; "
                      "use D0-D13, A0-A5, or the port name (PB5)")

        port, bit = AVR_328P_PINS[label]
        channel = int(label[1:]) if label[0] == "A" else None
        if direction == "analog" and channel is None:
            raise PseudocodeError(
                line, f"ANALOG needs an analog input, and {label} is "
                      f"digital-only on the {self.display}; use A0-A5")

        digital = int(label[1:]) if label[0] == "D" else None
        if direction == "pwm":
            if digital in AVR_TICK_PINS:
                raise PseudocodeError(
                    line, f"{label} is {AVR_TICK_PINS[digital]}, which is "
                          f"Timer 0 -- and Timer 0 is the millisecond tick "
                          f"every wait in the program is measured against. "
                          f"Use D9, D10, D11 or D3.")
            if digital not in AVR_PWM_PINS:
                usable = ", ".join(f"D{n}" for n in sorted(AVR_PWM_PINS))
                raise PseudocodeError(
                    line, f"PWM on the {self.display} is only on the timer "
                          f"compare outputs: {usable}. {label} is "
                          f"digital-only.")
            for other in program.pins.values():
                if other.direction == "tone" and AVR_PWM_PINS[digital][3] == 1:
                    raise PseudocodeError(
                        line, f"PWM on {label} and the TONE pin "
                              f"{other.name!r} both need Timer 1. Use D11 or "
                              f"D3 for PWM instead -- those are Timer 2.")
        if direction == "tone":
            if digital != AVR_TONE_PIN:
                raise PseudocodeError(
                    line, f"the tone is Timer 1 toggling OC1A, so it can only "
                          f"be D{AVR_TONE_PIN} on the {self.display}, "
                          f"not {label}")
            for other in program.pins.values():
                if other.direction == "tone":
                    raise PseudocodeError(
                        line, f"only one TONE pin ({other.name!r} already has "
                              f"it): there is one Timer 1")
                if other.direction == "pwm" and other.where[0] == "D" \
                        and AVR_PWM_PINS.get(int(other.where[1:]), (0, 0, 0, 0))[3] == 1:
                    raise PseudocodeError(
                        line, f"a TONE pin and PWM on {other.where} both need "
                              f"Timer 1 -- {other.name!r} would stop fading "
                              f"while a note sounds. Move it to D11 or D3.")
        return AvrPin(name, label, direction, active_low, port, bit, channel)

    def _bit(self, pin) -> str:
        return f"_BV(P{pin.port}{pin.bit})"

    def write_pin(self, pin, high):
        if high:
            return f"PORT{pin.port} |= {self._bit(pin)};"
        return f"PORT{pin.port} &= (unsigned char)~{self._bit(pin)};"

    def toggle_pin(self, pin):
        # Writing a one to a PINx bit toggles PORTxn in hardware (datasheet
        # 14.2.2) -- one instruction, and no read-modify-write to be
        # interrupted halfway through.
        return f"PIN{pin.port} = {self._bit(pin)};"

    def read_pin(self, pin):
        read = f"(PIN{pin.port} & {self._bit(pin)})"
        return f"!{read}" if pin.active_low else read

    def read_analog(self, pin):
        return f"adc_read({pin.channel})"

    def resolve_port(self, program, name, where, direction, active_low, line):
        match = re.fullmatch(r"(?:port)?([b-d])", where, re.I)
        if not match:
            raise PseudocodeError(
                line, f"{where.upper()} is not a port on the {self.display}; "
                      "use B, C or D (PORTB, PORTC, PORTD)")
        letter = match.group(1).upper()
        # PORTx is the output latch and PINx the actual pin levels. Reading
        # PORTx back would return what was last written, which on an input
        # port is the pull-up configuration and not the world.
        # `port` is the letter itself, so it compares equal to an AvrPin's
        # `.port` when checking whether a pin sits inside a declared port.
        return Port(name, letter, direction, active_low, where=letter,
                    write_sfr=f"PORT{letter}", read_sfr=f"PIN{letter}")

    def _pwm(self, pin):
        return AVR_PWM_PINS[int(pin.where[1:])]

    def write_pwm(self, pin, value: str) -> str:
        # The compare register holds the proportion of time the PIN is high;
        # the AST stores the percentage of time the LOAD is on. Same number
        # only on an active-high pin, and the outer parentheses matter.
        register = self._pwm(pin)[0]
        duty = f"(100 - ({value}))" if pin.active_low else f"({value})"
        return f"{register} = ({duty} * 255) / 100;"

    def write_tone(self, pin, hz: str) -> str:
        return f"bw_tone({hz});"

    def write_print(self, node) -> str:
        if node.value is None:
            return f'bw_print("{_c_string(node.text)}");'
        return f"bw_print_num({node.value});"

    # ---- time -----------------------------------------------------------
    def delay(self, ms):
        return f"delay_ms({ms});"

    def now(self):
        return "bw_now()"

    def _tick(self, program) -> tuple[int, str, int]:
        """Compare value, CS bits and divisor for an exact 1 kHz tick."""
        for divisor, bits in AVR_PRESCALERS:
            counts = program.clock / (divisor * 1000)
            if counts.is_integer() and 1 <= counts <= 256:
                return int(counts) - 1, bits, divisor
        raise PseudocodeError(
            1, f"{program.clock} Hz cannot be divided into an exact "
               f"millisecond by Timer 0 on the {self.display}; use a clock "
               f"like 16 MHz, 8 MHz or 1 MHz")

    # ---- the shell ------------------------------------------------------
    def prologue(self, program):
        return [
            "#include <avr/io.h>",
            "#include <avr/interrupt.h>",
            "",
            f"#define F_CPU {program.clock}UL",
            "",
        ]

    def runtime(self, program, tasks):
        compare, _bits, divisor = self._tick(program)
        out = [
            "/* Timer 0 in CTC mode, one interrupt per millisecond. Nothing",
            " * here busy-waits on the clock, so a wait costs no accuracy, and",
            f" * the tick is exact rather than near: {program.clock} / {divisor}"
            f" / {compare + 1} = 1000 Hz. */",
            "static volatile unsigned long bw_ms;",
            "",
            "ISR(TIMER0_COMPA_vect)",
            "{",
            "    bw_ms++;",
            "}",
            "",
            "/* A 32-bit read is four instructions on an 8-bit core; hold the",
            " * tick off rather than risk tearing across the increment. */",
            "static unsigned long bw_now(void)",
            "{",
            "    unsigned long t;",
            "    unsigned char sreg = SREG;",
            "    cli();",
            "    t = bw_ms;",
            "    SREG = sreg;",
            "    return t;",
            "}",
            "",
        ]
        if not tasks:
            out += [
                "static void delay_ms(unsigned int ms)",
                "{",
                "    unsigned long until = bw_now() + ms;",
                "    while ((long)(bw_now() - until) < 0) ;",
                "}",
                "",
            ]
        if program.tables:
            out += [
                "/* Lookup tables. A plain `const` array on an AVR is copied",
                " * from flash into RAM at startup -- the Harvard split means",
                " * `[]` cannot read flash directly. PROGMEM would avoid that,",
                " * but the index expression is shared with the other targets",
                " * and would have to become a target hook to say pgm_read_byte.",
                " * A font costs tens of bytes; a big table is the case that",
                " * would justify the hook. */",
            ]
            for name, values in program.tables.items():
                body = ", ".join(f"0x{v:02X}" for v in values)
                out.append(f"static const unsigned char bw_tab_{name}[] "
                           f"= {{ {body} }};")
            out += [
                "",
                "/* A computed index is clamped rather than trusted: reading",
                " * past a table gives a plausible-looking wrong byte. */",
                "static unsigned char bw_clamp(int i, unsigned char last)",
                "{",
                "    if (i < 0) return 0;",
                "    if (i > (int)last) return last;",
                "    return (unsigned char)i;",
                "}",
                "",
            ]

        if program.tone_pin is not None:
            out += [
                "/* Tone: Timer 1 in CTC mode toggling OC1A, so the frequency",
                " * is F_CPU/(2*8*(OCR1A+1)) and the whole audible band is",
                " * reachable. Toggling in hardware costs no interrupts and",
                " * does not drift, which a software square wave would while",
                " * the scheduler is busy elsewhere.",
                " *",
                " * This takes Timer 1 outright, which is why PWM on D9 and",
                " * D10 is refused in the same program. */",
                "static void bw_tone(unsigned int hz)",
                "{",
                "    if (hz) {",
                "        OCR1A  = (unsigned int)(F_CPU / 16UL / (unsigned long)hz - 1UL);",
                "        TCCR1A = _BV(COM1A0);          /* toggle OC1A on match */",
                "        TCCR1B = _BV(WGM12) | _BV(CS11);",
                "    } else {",
                "        TCCR1A = 0;                    /* release the pin */",
                "        TCCR1B = 0;",
                "        PORTB &= (unsigned char)~_BV(PB1);",
                "    }",
                "}",
                "",
            ]

        if program.uses_uart:
            out += [
                "/* Serial console on USART0, 8N1. Blocking on UDRE0 is",
                " * deliberate: a ring buffer costs RAM this part has little of,",
                " * and a dropped diagnostic is worse than a slow one. */",
                "static void bw_putc(char c)",
                "{",
                "    while (!(UCSR0A & _BV(UDRE0))) ;",
                "    UDR0 = (unsigned char)c;",
                "}",
                "",
                "static void bw_print(const char *s)",
                "{",
                "    while (*s) bw_putc(*s++);",
                "    bw_putc('\\r');",
                "    bw_putc('\\n');",
                "}",
                "",
                "static void bw_print_num(int v)",
                "{",
                "    char buffer[7];",
                "    unsigned char i = 0;",
                "    unsigned int u;",
                "    if (v < 0) { bw_putc('-'); u = (unsigned int)(-v); }",
                "    else u = (unsigned int)v;",
                "    do { buffer[i++] = (char)('0' + (u % 10)); u /= 10; } while (u);",
                "    while (i) bw_putc(buffer[--i]);",
                "    bw_putc('\\r');",
                "    bw_putc('\\n');",
                "}",
                "",
            ]

        for part in program.parts.values():
            out += self.shift_helper(part)

        if program.uses_adc:
            out += [
                "/* 10-bit ADC, polled, AVcc as reference. The prescaler is set",
                " * once in main(); this only selects the channel and waits. */",
                "static unsigned int adc_read(unsigned char channel)",
                "{",
                "    ADMUX = (unsigned char)(_BV(REFS0) | (channel & 0x0F));",
                "    ADCSRA |= _BV(ADSC);",
                "    while (ADCSRA & _BV(ADSC)) ;",
                "    return ADC;",
                "}",
                "",
            ]
        return out

    def setup(self, program):
        out: list[str] = []
        for pin in program.pins.values():
            if pin.direction == "output":
                out.append(f"    DDR{pin.port} |= {self._bit(pin)};"
                           f"   /* {pin.name} */")
            elif pin.direction == "input":
                out.append(f"    DDR{pin.port} &= (unsigned char)~{self._bit(pin)};")
                if pin.active_low:
                    # A button to ground; the internal pull-up is what holds
                    # the pin high while it is not pressed.
                    out.append(f"    PORT{pin.port} |= {self._bit(pin)};"
                               f"   /* {pin.name} pull-up */")
            elif pin.direction in ("pwm", "tone"):
                # Both drive the pin from a timer, and a compare output only
                # reaches the pad if the pin is configured as an output.
                out.append(f"    DDR{pin.port} |= {self._bit(pin)};"
                           f"   /* {pin.name} ({pin.direction}) */")
            else:
                out.append(f"    DDR{pin.port} &= (unsigned char)~{self._bit(pin)};"
                           f"   /* {pin.name} analog in */")
        for pin in program.pins.values():
            if pin.direction == "output":
                out.append("    " + self.write_pin(pin, pin.active_low)
                           + f"   /* {pin.name} off */")

        for part in program.parts.values():
            for claimed in part.claimed:
                out.append(f"    DDR{claimed.port} |= {self._bit(claimed)};"
                           f"   /* {part.name} */")

        for whole in program.ports.values():
            letter = whole.port
            if whole.direction == "output":
                out.append(f"    DDR{letter} = 0xFF;"
                           f"               /* {whole.name} */")
            else:
                out.append(f"    DDR{letter} = 0x00;")
                if whole.active_low:
                    out.append(f"    PORT{letter} = 0xFF;"
                               f"              /* {whole.name} pull-ups */")

        pwm_pins = [p for p in program.pins.values() if p.direction == "pwm"]
        if pwm_pins:
            out.append("")
            timers = {}
            for pin in pwm_pins:
                _reg, control, com, timer = self._pwm(pin)
                timers.setdefault(timer, (control, []))[1].append(com)
            for timer in sorted(timers):
                control, coms = timers[timer]
                enable = " | ".join(f"_BV({c})" for c in sorted(coms))
                # Fast PWM, 8-bit, /64 -- about 980 Hz on Timer 2 and 490 Hz
                # on Timer 1, which is what the Arduino core picks too and is
                # well above anything an LED or a motor driver cares about.
                if timer == 1:
                    out += [f"    {control} = {enable} | _BV(WGM10);",
                            "    TCCR1B = _BV(WGM12) | _BV(CS11) | _BV(CS10);"]
                else:
                    out += [f"    {control} = {enable} | _BV(WGM20) | _BV(WGM21);",
                            "    TCCR2B = _BV(CS22);"]
            for pin in pwm_pins:
                out.append("    " + self.write_pwm(pin, "0")
                           + f"   /* {pin.name} off */")

        if program.uses_uart:
            out += ["",
                    "    /* USART0, 8N1. UBRR0 is the divisor for the baud",
                    "     * rate, derived from F_CPU at compile time. */",
                    f"    UBRR0 = (unsigned int)(F_CPU / 16UL / {BW_BAUD}UL - 1UL);",
                    "    UCSR0B = _BV(TXEN0);",
                    "    UCSR0C = _BV(UCSZ01) | _BV(UCSZ00);"]

        if program.uses_adc:
            out += ["",
                    "    ADCSRA = _BV(ADEN) | _BV(ADPS2) | _BV(ADPS1) | _BV(ADPS0);"]

        compare, bits, _divisor = self._tick(program)
        out += ["", *self.tick_setup(compare, bits), "    sei();"]
        return out

    def start_scheduler(self, task_names):
        return ["", "    for (;;) {",
                *(f"        {name}();" for name in task_names),
                "    }"]

    def main(self, program, setup_lines, body_lines, task_names):
        out = ["int main(void)", "{"] + setup_lines
        if task_names:
            out += self.start_scheduler(task_names)
        else:
            out.append("")
            out += body_lines
            # main() must not fall off the end on a bare-metal part: there is
            # no exit(), and returning lands in avr-libc's infinite loop by
            # luck rather than intent.
            out += ["", "    for (;;) ;"]
        return out + ["}", ""]


def _stc(key, display, header, port_modes, aux_1t_bit, adc, pwm=False, p5=False):
    return Stc8051Target(key, display, header, port_modes, aux_1t_bit, adc, pwm, p5)


class PortBitAvrTarget(AvrTarget):
    """AVR or 6502 parts that use port-letter+bit pin naming (PA0, PB7).

    Unlike AvrTarget (which maps D0-D13/A0-A5 to the ATmega328P pinout),
    this target accepts any port letter A-L with any bit 0-7 directly.
    Used for ATtiny88, ATtiny85, eater6502.
    """

    def __init__(self, key: str, display: str, mcu: str, flash: int,
                 ports: str = "ABCD", default_clock: int = 8000000,
                 timsk: str = "TIMSK0", tick_ctc_bit: str = "WGM01",
                 tick_split: bool = True, uart: bool = True):
        super().__init__(key, display, mcu, flash)
        self._ports = frozenset(ports.upper())
        self.default_clock = default_clock
        # See AvrTarget's Timer-0 note: the tiny parts diverge here, and the
        # divergence is a compile error rather than a wrong number, which is
        # the only reason it stayed hidden as long as it did.
        self.timsk = timsk
        self.tick_ctc_bit = tick_ctc_bit
        self.tick_split = tick_split
        # No USART on the tiny parts, so `print` has nothing to write to.
        # Dropping the claim makes it a parse error naming the board, which
        # is what every other unsupported feature here does -- rather than
        # an avr-gcc error about UCSR0A, which reads as our bug.
        if not uart:
            self.supports = self.supports - {"print"}

    def resolve_pin(self, program, name, where, direction, active_low, line):
        match = AVR_PIN_RE.match(where)
        if not match:
            raise PseudocodeError(
                line, f"{where.upper()} is not a pin on the {self.display}; "
                      f"use P{'/P'.join(sorted(self._ports))}0-7")
        kind, number, port, bit = match.groups()
        if kind:
            raise PseudocodeError(
                line, f"{self.display} uses port names (PB0, PD7), "
                      f"not Arduino numbers ({where.upper()})")
        port = port.upper()
        bit = int(bit)
        if port not in self._ports:
            raise PseudocodeError(
                line, f"Port {port} does not exist on the {self.display}; "
                      f"known ports: {', '.join(sorted(self._ports))}")
        label = f"P{port}{bit}"
        channel = None
        return AvrPin(name, label, direction, active_low, port, bit, channel)


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
    "stc15f2k60s2": _stc("stc15f2k60s2", "STC15F2K60S2", "stc12.h", True, True, True, True, p5=True),

    # Both are ATmega328P boards and differ here only in how many analog pins
    # the package brings out: the Uno's header stops at A5, the Nano carries
    # A6 and A7 as well (input-only, which this generator never violates
    # because ANALOG is read-only by construction).
    "arduino-uno": ArduinoTarget("arduino-uno", "Arduino Uno", 13, 5),
    # A6 and A7 exist on the Nano and are analog-in only -- the TQFP package
    # brings out the ADC channels without a digital buffer.
    "arduino-nano": ArduinoTarget("arduino-nano", "Arduino Nano", 13, 7,
                                  analog_only=frozenset({6, 7})),

    # The same silicon as an Uno/Nano/Pro Mini, emitted without the Arduino
    # core -- which is the form this service can actually compile. Pins keep
    # the board's own labels, so a program moves between `arduino-uno` and
    # `atmega328p` unchanged and only the generated C differs.
    "atmega328p": AvrTarget("atmega328p", "ATmega328P", "atmega328p", 32768),
    "atmega168p": AvrTarget("atmega168p", "ATmega168P", "atmega168p", 16384),
}


@dataclass
class Program:
    part: str = "stc12c5a60s2"
    clock: int = 11059200
    # What to call the generated file. Empty means "main", which is what every
    # program was called before NAME existed. The Arduino IDE in particular
    # wants a sketch folder named after the sketch, so `blink.ino` in `blink/`
    # beats `main.ino` in `main/`.
    name: str = ""
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
    # Index-aligned with `whens`: None for `WHEN started:`, or (pin, edge) for
    # an event hat. Kept parallel rather than folded into `whens` so that every
    # existing walker over `whens` keeps working unchanged.
    when_hats: list = field(default_factory=list)
    body: list = field(default_factory=list)
    locals_: set = field(default_factory=set)

    @property
    def has_matrix(self) -> bool:
        """A MATRIX8X8 refreshes itself in the Timer-0 ISR, so its presence
        forces the cooperative-scheduler code path (the ISR that scans it)
        even for a single WHEN block that would otherwise run straight-line."""
        return any(isinstance(p, MatrixPart) for p in self.parts.values())

    @property
    def has_sevenseg(self) -> bool:
        return any(isinstance(p, SevenSegPart) for p in self.parts.values())

    @property
    def has_ledbank(self) -> bool:
        return any(isinstance(p, LedBankPart) for p in self.parts.values())

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
    | (?P<op><=|>=|!=|<>|==|[-+*/%()<>=\[\],])
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
        # `not` binds looser than comparisons and tighter than and/or —
        # Python's precedence, because `IF not k = shown` must mean
        # `not (k = shown)`. The old atom-level `not` parsed it as
        # `(not k) = shown`, which compared a boolean to a number and
        # made a running program silently do nothing (found on the A2
        # bench, 14-a2-keyshow, 2026-08-17). atom() keeps its own `not`
        # for the degenerate spots this level never reaches.
        if (level == NOT_LEVEL and self.peek() is not None
                and self.peek().lower() == "not"):
            self.take()
            return Unary("not", self.parse(level))
        node = self.parse(level + 1)
        while True:
            token = self.peek()
            if token is None:
                return node
            op = SYNONYM.get(token, WORD_OPS.get(token.lower(), token.lower()))
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
        if token.lower() == "read":
            # sb3-creator's dialect spells a pin read as `read <pin>`; ours
            # is the bare pin name. The oracle accepts what the other
            # implementation emits, or parity is a fiction: `read x` is
            # exactly PinRef(x) (polarity applied like any pin read).
            nxt = self.peek()
            if nxt is not None and nxt.lower() in self.program.pins:
                self.take()
                return PinRef(nxt.lower())
            # `read` not followed by a pin falls through to being a
            # variable name, as before.
            if NAME_RE.match(token):
                if token not in self.program.locals_ and token not in self.program.variables:
                    self.program.variables.append(token)
                return Var(token)
        if token.lower() == "a" and [t.lower() for t in
                self.tokens[self.pos:self.pos + 3]] == ["key", "is", "pressed"]:
            # `a key is pressed` -- sugar over the sole KEYPAD4X4, desugared
            # to `<pad> >= 0` so no new AST shape (and no new lowering in any
            # back end) is needed. sb3-creator prints the desugared form back,
            # so the canonical fixed point is `keys >= 0`.
            self.pos += 3
            pad = sole_keypad(self.program, self.line)
            return Binary(">=", KeypadRef(pad.name), Num(0))
        if (token.lower() == "key"
                and self.pos + 2 < len(self.tokens)
                and re.fullmatch(r"\d+", self.tokens[self.pos])
                and self.tokens[self.pos + 1].lower() == "is"
                and self.tokens[self.pos + 2].lower() in ("pressed", "released")):
            # `key N is pressed` / `is released` -- one specific key held (or
            # not). Guarded by the full four-token shape so a VARIABLE named
            # `key` (the keyshow example has one) still parses as a variable.
            n = int(self.take())
            self.take()
            state = self.take().lower()
            if n > 15:
                raise PseudocodeError(
                    self.line, f"key {n} does not exist; a KEYPAD4X4 has keys 0..15")
            pad = sole_keypad(self.program, self.line)
            ref = Binary("=", KeypadRef(pad.name), Num(n))
            return Unary("not", ref) if state == "released" else ref
        if token.lower() == "pixel":
            # `pixel X Y is on` / `is off` -- a boolean over the sole MATRIX8X8.
            # X and Y are atoms (a number, a name or a parenthesised group), so
            # the trailing `is on` is not swallowed as part of them.
            part = sole_matrix(self.program, self.line)
            x = self.atom()
            y = self.atom()
            ref = MatrixPixelRef(part.name, x, y)
            nxt = self.peek()
            if nxt is not None and nxt.lower() == "is":
                self.take()
                state = self.take()
                if state is None or state.lower() not in ("on", "off"):
                    raise PseudocodeError(
                        self.line, "expected 'on' or 'off' after 'pixel X Y is'")
                return Unary("not", ref) if state.lower() == "off" else ref
            return ref
        if token.lower() == "randint" and self.peek() == "(":
            require(self.program, "game", self.line, "randint(...)")
            self.take()  # consume '('
            low = self.parse()
            if self.peek() == ",":
                self.take()
            high = self.parse()
            if self.take() != ")":
                raise PseudocodeError(self.line, "missing ')' after randint")
            return Randint(low, high)
        if token.lower() == "controller" and self.peek() is not None and self.peek().lower() in ("dx", "dy"):
            require(self.program, "game", self.line, "the controller reporter")
            axis = self.take().lower()
            return ControllerAxis(axis)
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
        if (lowered in self.program.parts
                and isinstance(self.program.parts[lowered], KeypadPart)):
            return KeypadRef(lowered)
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

        arc_overlap = re.fullmatch(
            r"arcade\s+on\s+overlap\s+(\w+)\s+(\w+)\s*:", text, re.I)
        if arc_overlap:
            require(program, "game", line.number, "ARCADE ON OVERLAP")
            inner, index = parse_block(lines, index + 1, indent, program)
            body.append(ArcadeOnOverlap(arc_overlap.group(1),
                                         arc_overlap.group(2), inner))
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


def require(program: Program, feature: str, line: int, what: str) -> None:
    """Refuse a feature the target cannot emit, naming both."""
    if feature not in program.target.supports:
        raise PseudocodeError(
            line, f"{what} is not available on the {program.target.display}. "
                  f"Devices that have it: "
                  + ", ".join(sorted({t.display for t in TARGETS.values()
                                      if feature in t.supports})))


def sole_matrix(program: Program, line: int) -> "MatrixPart":
    """The one MATRIX8X8 in the program, for the verbs that do not name it
    (`light pixel`, `draw row`). Naming which screen would be noise on a board
    with a single matrix, which is every A2-class board."""
    screens = [p for p in program.parts.values() if isinstance(p, MatrixPart)]
    if not screens:
        raise PseudocodeError(
            line, "no MATRIX8X8 screen is declared; add a "
                  "'PART <name> = MATRIX8X8 ROWS 74HC595 ... COLUMNS <port>' line")
    if len(screens) > 1:
        raise PseudocodeError(
            line, "several MATRIX8X8 screens are declared; this verb does not say "
                  "which one to draw on")
    return screens[0]


def sole_keypad(program: Program, line: int) -> "KeypadPart":
    """The one KEYPAD4X4, for the phrases that do not name it (`WHEN key N
    pressed`, `a key is pressed`). Same rule as sole_matrix: every A2-class
    board has exactly one keypad, so naming it would be noise."""
    pads = [p for p in program.parts.values() if isinstance(p, KeypadPart)]
    if not pads:
        raise PseudocodeError(
            line, "no KEYPAD4X4 is declared; add a "
                  "'PART <name> = KEYPAD4X4 ROWS ... COLS ...' line")
    if len(pads) > 1:
        raise PseudocodeError(
            line, "several KEYPAD4X4 parts are declared; this phrase does not "
                  "say which one it means")
    return pads[0]


def _named_matrix(program: Program, name: str, line: int):
    """The MATRIX8X8 called `name`, or None if `name` is not a matrix. Used by
    the verbs that DO carry the screen name (clear/scroll/show image/brightness)."""
    part = program.parts.get(name.lower())
    return part if isinstance(part, MatrixPart) else None


def matrix_statement(text: str, program: Program, line: int):
    """Parse a MATRIX8X8 drawing verb, or return None if this is not one.

    The pixel/row verbs address the sole screen implicitly; clear/scroll/show
    image/brightness carry its name. Coordinates and the byte/level are ordinary
    expressions."""
    lowered = text.lower()

    px = re.fullmatch(r"(light|clear)\s+pixel\s+(\S+)\s+(\S+)", text, re.I)
    if px:
        part = sole_matrix(program, line)
        return MatrixSetPixel(part.name,
                              expression(px.group(2), program, line),
                              expression(px.group(3), program, line),
                              style=px.group(1).lower())

    onoff = re.fullmatch(r"set\s+pixel\s+(\S+)\s+(\S+)\s+to\s+(on|off)", text, re.I)
    if onoff:
        part = sole_matrix(program, line)
        return MatrixSetPixel(part.name,
                              expression(onoff.group(1), program, line),
                              expression(onoff.group(2), program, line),
                              style=onoff.group(3).lower())

    bright = re.fullmatch(r"set\s+pixel\s+(\S+)\s+(\S+)\s+brightness\s+(.+)",
                          text, re.I)
    if bright:
        part = sole_matrix(program, line)
        return MatrixSetPixel(part.name,
                              expression(bright.group(1), program, line),
                              expression(bright.group(2), program, line),
                              style="brightness",
                              level=expression(bright.group(3), program, line))

    row = re.fullmatch(r"draw\s+row\s+(\S+)\s*=\s*(.+)", text, re.I)
    if row:
        part = sole_matrix(program, line)
        return MatrixDrawRow(part.name,
                             expression(row.group(1), program, line),
                             expression(row.group(2), program, line))

    image = re.fullmatch(r"show\s+image\s+(\w+)\s+on\s+(\w+)", text, re.I)
    if image:
        table, name = image.group(1), image.group(2)
        part = _named_matrix(program, name, line)
        if part is None:
            raise PseudocodeError(line, f"{name!r} is not a MATRIX8X8 screen")
        if table.lower() not in program.tables:
            raise PseudocodeError(
                line, f"{table!r} is not a TABLE; 'show image' blits an 8-byte "
                      f"TABLE onto the screen")
        return MatrixImage(part.name, table.lower())

    scroll = re.fullmatch(r"scroll\s+(\w+)\s+(left|right|up|down)", text, re.I)
    if scroll:
        part = _named_matrix(program, scroll.group(1), line)
        if part is None:
            return None
        return MatrixScroll(part.name, scroll.group(2).lower())

    sb = re.fullmatch(r"set\s+(\w+)\s+brightness\s+(.+)", text, re.I)
    if sb:
        part = _named_matrix(program, sb.group(1), line)
        if part is not None:
            return MatrixBrightness(part.name,
                                    expression(sb.group(2), program, line))

    clr = re.fullmatch(r"clear\s+(\w+)", text, re.I)
    if clr:
        part = _named_matrix(program, clr.group(1), line)
        if part is not None:
            return MatrixClear(part.name)

    return None


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
        return Wait(expression(wait.group(1), program, line), unit, line)

    drawn = matrix_statement(text, program, line)
    if drawn is not None:
        return drawn

    # ---- Arcade game-engine verbs ----
    # One guard for the family: every one of them starts with `arcade`, so the
    # refusal belongs here rather than repeated at ten parse sites. Without it
    # they parse on a chip, reach an emitter with no case for them, and escape
    # as a bare TypeError -- a 500 where every other unsupported feature gives
    # a line number and names the board.
    if re.match(r"arcade\s", text, re.I):
        require(program, "game", line, "an arcade verb")

    arc_create = re.fullmatch(r"arcade\s+create\s+(\w+)\s+kind\s+(\w+)", text, re.I)
    if arc_create:
        return ArcadeCreate(arc_create.group(1).lower(), arc_create.group(2))

    arc_place = re.match(r"arcade\s+place\s+(\w+)\s+x\s+(.+?)\s+y\s+(.+)$", text, re.I)
    if arc_place:
        return ArcadePlace(arc_place.group(1).lower(),
                           expression(arc_place.group(2), program, line),
                           expression(arc_place.group(3), program, line))

    arc_move = re.match(r"arcade\s+move\s+(\w+)\s+vx\s+(.+?)\s+vy\s+(.+)$", text, re.I)
    if arc_move:
        return ArcadeMove(arc_move.group(1).lower(),
                          expression(arc_move.group(2), program, line),
                          expression(arc_move.group(3), program, line))

    arc_flag = re.fullmatch(
        r"arcade\s+set\s+(\w+)\s+(stay\s+in\s+screen|destroy\s+on\s+wall)", text, re.I)
    if arc_flag:
        flag = "stayinscreen" if "stay" in arc_flag.group(2).lower() else "destroyonwall"
        return ArcadeSetFlag(arc_flag.group(1).lower(), flag)

    arc_score = re.match(r"arcade\s+score\s+add\s+(.+)$", text, re.I)
    if arc_score:
        return ArcadeScore(expression(arc_score.group(1), program, line))

    arc_over = re.fullmatch(r"arcade\s+game\s+over\s+(win|lose)", text, re.I)
    if arc_over:
        return ArcadeGameOver(win=(arc_over.group(1).lower() == "win"))

    arc_tilemap = re.fullmatch(
        r"arcade\s+tilemap\s+(\w+)\s+cols\s+(\S+)\s+rows\s+(\S+)\s+tile\s+(\S+)",
        text, re.I)
    if arc_tilemap:
        return ArcadeTilemap(arc_tilemap.group(1).lower(),
                             expression(arc_tilemap.group(2), program, line),
                             expression(arc_tilemap.group(3), program, line),
                             expression(arc_tilemap.group(4), program, line))

    arc_settile = re.match(
        r"arcade\s+set\s+tile\s+(\w+)\s+col\s+(\S+)\s+row\s+(\S+)\s+to\s+(.+)$",
        text, re.I)
    if arc_settile:
        return ArcadeSetTile(arc_settile.group(1).lower(),
                             expression(arc_settile.group(2), program, line),
                             expression(arc_settile.group(3), program, line),
                             expression(arc_settile.group(4), program, line))

    arc_wall = re.fullmatch(
        r"arcade\s+set\s+wall\s+(\w+)\s+tile\s+(\S+)", text, re.I)
    if arc_wall:
        return ArcadeTileWall(arc_wall.group(1).lower(),
                              expression(arc_wall.group(2), program, line))

    arc_frame = re.match(
        r"arcade\s+set\s+frame\s+(\w+)\s+to\s+(.+)$", text, re.I)
    if arc_frame:
        return ArcadeSetFrame(arc_frame.group(1).lower(),
                              expression(arc_frame.group(2), program, line))

    # ---- LCD verbs ----
    lcd_print_s = re.match(r'lcd\s+print\s+"([^"]*)"\s+on\s+(\w+)$', text, re.I)
    if lcd_print_s:
        return LcdPrint(lcd_print_s.group(2).lower(), text=lcd_print_s.group(1))

    lcd_print_v = re.match(r"lcd\s+print\s+(.+?)\s+on\s+(\w+)$", text, re.I)
    if lcd_print_v:
        return LcdPrint(lcd_print_v.group(2).lower(),
                        value=expression(lcd_print_v.group(1), program, line))

    lcd_cursor = re.match(r"lcd\s+set\s+cursor\s+(\S+)\s+(\S+)\s+on\s+(\w+)$", text, re.I)
    if lcd_cursor:
        return LcdCursor(lcd_cursor.group(3).lower(),
                         expression(lcd_cursor.group(1), program, line),
                         expression(lcd_cursor.group(2), program, line))

    lcd_clear = re.fullmatch(r"lcd\s+clear\s+(\w+)", text, re.I)
    if lcd_clear:
        return LcdClear(lcd_clear.group(1).lower())

    # ---- TFT verbs ----
    tft_pixel = re.match(
        r"tft\s+pixel\s+(\S+)\s+(\S+)\s+R\s+(\S+)\s+G\s+(\S+)\s+B\s+(\S+)\s+on\s+(\w+)$",
        text, re.I)
    if tft_pixel:
        g = tft_pixel.groups()
        return TftPixel(g[5].lower(),
                        expression(g[0], program, line),
                        expression(g[1], program, line),
                        expression(g[2], program, line),
                        expression(g[3], program, line),
                        expression(g[4], program, line))

    tft_fill = re.match(
        r"tft\s+fill\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+R\s+(\S+)\s+G\s+(\S+)\s+B\s+(\S+)\s+on\s+(\w+)$",
        text, re.I)
    if tft_fill:
        g = tft_fill.groups()
        return TftFill(g[7].lower(),
                       expression(g[0], program, line),
                       expression(g[1], program, line),
                       expression(g[2], program, line),
                       expression(g[3], program, line),
                       expression(g[4], program, line),
                       expression(g[5], program, line),
                       expression(g[6], program, line))

    tft_clear = re.fullmatch(r"tft\s+clear\s+(\w+)", text, re.I)
    if tft_clear:
        return TftClear(tft_clear.group(1).lower())

    tft_print_s = re.match(r'tft\s+print\s+"([^"]*)"\s+on\s+(\w+)$', text, re.I)
    if tft_print_s:
        return TftPrint(tft_print_s.group(2).lower(), text=tft_print_s.group(1))

    tft_print_v = re.match(r"tft\s+print\s+(.+?)\s+on\s+(\w+)$", text, re.I)
    if tft_print_v:
        return TftPrint(tft_print_v.group(2).lower(),
                        value=expression(tft_print_v.group(1), program, line))

    tft_cursor = re.match(r"tft\s+set\s+cursor\s+(\S+)\s+(\S+)\s+on\s+(\w+)$", text, re.I)
    if tft_cursor:
        return TftCursor(tft_cursor.group(3).lower(),
                         expression(tft_cursor.group(1), program, line),
                         expression(tft_cursor.group(2), program, line))

    # ---- OLED verbs ----
    oled_pixel = re.match(
        r"oled\s+pixel\s+(\S+)\s+(\S+)\s+(\S+)\s+on\s+(\w+)$", text, re.I)
    if oled_pixel:
        return OledPixel(oled_pixel.group(4).lower(),
                         expression(oled_pixel.group(1), program, line),
                         expression(oled_pixel.group(2), program, line),
                         expression(oled_pixel.group(3), program, line))

    oled_clear = re.fullmatch(r"oled\s+clear\s+(\w+)", text, re.I)
    if oled_clear:
        return OledClear(oled_clear.group(1).lower())

    oled_print_s = re.match(r'oled\s+print\s+"([^"]*)"\s+on\s+(\w+)$', text, re.I)
    if oled_print_s:
        return OledPrint(oled_print_s.group(2).lower(), text=oled_print_s.group(1))

    oled_print_v = re.match(r"oled\s+print\s+(.+?)\s+on\s+(\w+)$", text, re.I)
    if oled_print_v:
        return OledPrint(oled_print_v.group(2).lower(),
                         value=expression(oled_print_v.group(1), program, line))

    oled_cursor = re.match(r"oled\s+set\s+cursor\s+(\S+)\s+(\S+)\s+on\s+(\w+)$", text, re.I)
    if oled_cursor:
        return OledCursor(oled_cursor.group(3).lower(),
                          expression(oled_cursor.group(1), program, line),
                          expression(oled_cursor.group(2), program, line))

    # ---- RGB LED verb ----
    rgb_set = re.match(
        r"set\s+(\w+)\s+colour\s+to\s+R\s+(\S+)\s+G\s+(\S+)\s+B\s+(\S+)$", text, re.I)
    if rgb_set:
        return RgbSet(rgb_set.group(1).lower(),
                      expression(rgb_set.group(2), program, line),
                      expression(rgb_set.group(3), program, line),
                      expression(rgb_set.group(4), program, line))

    # ---- SEVENSEG8 verbs ----
    show_num = re.match(r"show\s+number\s+(.+?)\s+on\s+(\w+)$", text, re.I)
    if show_num and isinstance(program.parts.get(show_num.group(2).lower()),
                               SevenSegPart):
        return ShowNumber(show_num.group(2).lower(),
                          expression(show_num.group(1), program, line))

    show_dig = re.match(r"show\s+digit\s+(.+?)\s*=\s*value\s+(.+?)\s+on\s+(\w+)$",
                        text, re.I)
    if show_dig and isinstance(program.parts.get(show_dig.group(3).lower()),
                               SevenSegPart):
        return ShowDigit(show_dig.group(3).lower(),
                         expression(show_dig.group(1), program, line),
                         expression(show_dig.group(2), program, line))

    set_seg = re.match(r"set\s+digit\s+(.+?)\s+to\s+segments\s+(.+?)\s+on\s+(\w+)$",
                       text, re.I)
    if set_seg and isinstance(program.parts.get(set_seg.group(3).lower()),
                              SevenSegPart):
        return SetDigitSegments(set_seg.group(3).lower(),
                                expression(set_seg.group(1), program, line),
                                expression(set_seg.group(2), program, line))

    clear_disp = re.fullmatch(r"clear\s+(\w+)", lowered)
    if clear_disp and isinstance(program.parts.get(clear_disp.group(1)),
                                 SevenSegPart):
        return ClearDisplay(clear_disp.group(1))

    # ---- LEDBANK8 verbs ----
    led_on = re.match(r"turn\s+on\s+led\s+(.+?)\s+on\s+(\w+)$", text, re.I)
    if led_on and isinstance(program.parts.get(led_on.group(2).lower()),
                             LedBankPart):
        return TurnOnLed(led_on.group(2).lower(),
                         expression(led_on.group(1), program, line))

    led_off = re.match(r"turn\s+off\s+led\s+(.+?)\s+on\s+(\w+)$", text, re.I)
    if led_off and isinstance(program.parts.get(led_off.group(2).lower()),
                              LedBankPart):
        return TurnOffLed(led_off.group(2).lower(),
                          expression(led_off.group(1), program, line))

    set_leds = re.match(r"set\s+leds\s+to\s+(.+?)\s+on\s+(\w+)$", text, re.I)
    if set_leds and isinstance(program.parts.get(set_leds.group(2).lower()),
                               LedBankPart):
        return SetLeds(set_leds.group(2).lower(),
                       expression(set_leds.group(1), program, line))

    only_led = re.match(r"light\s+only\s+led\s+(.+?)\s+on\s+(\w+)$", text, re.I)
    if only_led and isinstance(program.parts.get(only_led.group(2).lower()),
                               LedBankPart):
        return LightOnlyLed(only_led.group(2).lower(),
                            expression(only_led.group(1), program, line))

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
        if isinstance(program.parts[into.group(1).lower()], KeypadPart):
            raise PseudocodeError(
                line, f"{into.group(1)!r} is a keypad and cannot be written; "
                      f"read it in an expression (`set k to {into.group(1)}`)")
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
        require(program, "print", line, "print")
        return Print(text=say.group(1))
    say = re.match(r"print\s+(.+)$", text.strip(), re.I)
    if say:
        require(program, "print", line, "print")
        return Print(value=expression(say.group(1), program, line))

    hertz = re.fullmatch(r"set\s+(\w+)\s+to\s+(.+?)\s*(?:hz|hertz)", text.strip(), re.I)
    if hertz:
        name = hertz.group(1)
        pin = program.pins.get(name.lower())
        if pin is None:
            raise PseudocodeError(line, f"unknown pin {name!r}; declare it with PIN")
        require(program, "tone", line, "a frequency")
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
        require(program, "pwm", line, "a duty cycle")
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
# `WHEN btn pressed:` -- an event hat on a declared INPUT pin. Lowered to a
# POLLED task rather than to INT0/INT1: there are only two external-interrupt
# pins, so an interrupt-based hat would work for two buttons and then silently
# stop being available; the millisecond tick is already a debounce interval;
# and polling is the same state-machine shape the scheduler already has.
# docs/PARTS-TO-BLOCKS.md in the lab repo has the full reasoning.
# `WHEN key 5 pressed:` -- an edge hat on the sole KEYPAD4X4. Polled like
# the pin hats, but through a shared DEBOUNCED scan: one poll task per
# keypad reads the matrix once per dispatch, and a key must be seen in
# two consecutive scans before it counts (a scan mid-bounce reads -1 or
# a neighbour for one pass; two agreeing reads 1 ms apart do not).
WHEN_KEY_RE = re.compile(r"when\s+key\s+(\d+)\s+(pressed|released)\s*:", re.I)
WHEN_PIN_RE = re.compile(r"when\s+(\w+)\s+(pressed|released)\s*:", re.I)
PIN_RE = re.compile(r"pin\s+(\w+)\s*=\s*(\S+)\s+(output|input|analog|pwm|tone)"
                    r"(?:\s+active\s+(low|high))?", re.I)
KEYPAD_RE = re.compile(
    r"part\s+(\w+)\s*=\s*keypad4x4\s+"
    r"rows\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+"
    r"cols\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s*$", re.I)
MATRIX8X8_RE = re.compile(
    r"part\s+(\w+)\s*=\s*matrix8x8\s+"
    r"rows\s+74hc595\s+data\s+(\S+)\s+clock\s+(\S+)\s+latch\s+(\S+)\s+"
    r"columns\s+(\S+)\s*$", re.I)
PART_RE = re.compile(r"part\s+(\w+)\s*=\s*74hc595\s+data\s+(\S+)\s+"
                     r"clock\s+(\S+)\s+latch\s+(\S+)"
                     r"(?:\s+active\s+(low|high))?", re.I)
SEVENSEG_RE = re.compile(
    r"part\s+(\w+)\s*=\s*sevenseg8\s+segments\s+(\S+)\s+"
    r"select\s+(\S+)\s+(\S+)\s+(\S+)"
    r"(?:\s+common\s+(cathode|anode))?", re.I)
LEDBANK_RE = re.compile(
    r"part\s+(\w+)\s*=\s*ledbank8\s+on\s+(\S+)"
    r"(?:\s+active\s+(low|high))?", re.I)
PORT_DECL_RE = re.compile(r"port\s+(\w+)\s*=\s*(\S+)\s+(output|input)"
                          r"(?:\s+active\s+(low|high))?", re.I)
TABLE_RE = re.compile(r"table\s+(\w+)\s*=\s*(.+)$", re.I)
CLOCK_RE = re.compile(r"clock\s+([\d_]+)\s*(hz|mhz)?", re.I)
# Deliberately strict, and not only for tidiness: this string is handed back in
# a Content-Disposition header and used as a filename. Letters, digits,
# underscore and dash, starting with a letter or underscore -- no dots, no
# separators, no quotes, nothing that can escape either context.
PROGRAM_NAME_RE = re.compile(r"name\s+([A-Za-z_][A-Za-z0-9_-]{0,39})\s*", re.I)


def parse(source: str) -> Program:
    lines = read_lines(source)
    if not lines:
        raise PseudocodeError(1, "empty program")

    program = Program()
    index = 0

    device = re.fullmatch(r"device\s+([\w-]+)\s*:?", lines[0].text, re.I)
    if device:
        program.part = device.group(1).lower()
        if program.part in TARGETS:
            target = TARGETS[program.part]
            if target.pseudocode_gap:
                raise PseudocodeError(lines[0].number, target.pseudocode_gap)
            # The device decides what CLOCK means when the program is silent.
            # A later CLOCK line still wins.
            program.clock = target.default_clock
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

        named = PROGRAM_NAME_RE.fullmatch(text.strip())
        if named and not started:
            program.name = named.group(1)
            index += 1
            continue

        clock = CLOCK_RE.fullmatch(lowered)
        if clock and not started:
            value = int(clock.group(1).replace("_", ""))
            program.clock = value * 1_000_000 if clock.group(2) == "mhz" else value
            index += 1
            continue

        keypad = KEYPAD_RE.fullmatch(lowered)
        if keypad and not started:
            require(program, "keypad", line.number, "a KEYPAD4X4 PART")
            name = keypad.group(1)
            if name in program.parts or name in program.ports or name in program.pins:
                raise PseudocodeError(line.number, f"{name!r} declared twice")
            scratch = Program(part=program.part)
            tokens = keypad.groups()[1:]
            roles = ([f"row{i}" for i in range(4)]
                     + [f"col{i}" for i in range(4)])
            # rows scan as outputs, cols read as inputs; resolved against a
            # scratch program for the same reason the 595's pins are
            claims = [program.target.resolve_pin(
                          scratch, f"{name}_{role}", token,
                          "output" if role.startswith("row") else "input",
                          False, line.number)
                      for role, token in zip(roles, tokens)]
            if len({pin.where for pin in claims}) != 8:
                raise PseudocodeError(
                    line.number, f"{name!r} names the same pin twice; a 4x4 "
                                 f"keypad claims eight different pins")
            for claimed in claims:
                for other in program.pins.values():
                    if other.where == claimed.where:
                        raise PseudocodeError(
                            line.number, f"{claimed.where} is already declared as "
                                         f"{other.name!r}; a PART claims its pins")
                for whole in program.ports.values():
                    if getattr(claimed, "port", None) == whole.port:
                        raise PseudocodeError(
                            line.number, f"{claimed.where} is inside the whole port "
                                         f"{whole.name!r}, which would clobber it")
                for prev in program.parts.values():
                    if claimed.where in prev.claimed_where:
                        raise PseudocodeError(
                            line.number, f"{claimed.where} is already claimed by "
                                         f"{prev.name!r}")
            program.parts[name] = KeypadPart(name, "keypad4x4",
                                             claims[:4], claims[4:])
            index += 1
            continue

        matrix = MATRIX8X8_RE.fullmatch(lowered)
        if matrix and not started:
            require(program, "matrix", line.number, "a MATRIX8X8 PART")
            name, data, clock, latch, cols = matrix.groups()
            if name in program.parts or name in program.ports or name in program.pins:
                raise PseudocodeError(line.number, f"{name!r} declared twice")
            # Resolved against an empty scratch program of the same device, like
            # the 595 and keypad: all we want is "is this a real pin/port on this
            # board, and what is it called". The clash cascade below owns the
            # user-facing "a PART claims its pins" answer.
            scratch = Program(part=program.part)
            ctrl = [program.target.resolve_pin(
                        scratch, f"{name}_{role}", token, "output", False,
                        line.number)
                    for role, token in (("data", data), ("clock", clock),
                                        ("latch", latch))]
            # The columns are a WHOLE port (active-low sinks, bit7 = left). The
            # ISR writes it byte-at-a-time; the eight pins exist for the claim
            # machinery and for setup()'s output-direction pass.
            col_port = program.target.resolve_port(
                scratch, f"{name}_cols", cols, "output", True, line.number)
            columns = [program.target.resolve_pin(
                           scratch, f"{name}_c{b}", f"{col_port.label}.{b}",
                           "output", False, line.number)
                       for b in range(8)]
            claims = ctrl + columns
            if len({pin.where for pin in claims}) != 11:
                raise PseudocodeError(
                    line.number, f"{name!r} names the same pin twice; a MATRIX8X8 "
                                 f"claims three 595 pins and eight column pins")
            for claimed in claims:
                for other in program.pins.values():
                    if other.where == claimed.where:
                        raise PseudocodeError(
                            line.number, f"{claimed.where} is already declared as "
                                         f"{other.name!r}; a PART claims its pins")
                for whole in program.ports.values():
                    if getattr(claimed, "port", None) == whole.port:
                        raise PseudocodeError(
                            line.number, f"{claimed.where} is inside the whole port "
                                         f"{whole.name!r}, which would clobber it")
                for prev in program.parts.values():
                    if claimed.where in prev.claimed_where:
                        raise PseudocodeError(
                            line.number, f"{claimed.where} is already claimed by "
                                         f"{prev.name!r}")
            program.parts[name] = MatrixPart(name, "matrix8x8", ctrl[0], ctrl[1],
                                             ctrl[2], col_port, columns)
            index += 1
            continue

        part = PART_RE.fullmatch(lowered)
        if part and not started:
            require(program, "part", line.number, "a PART")
            (name, data, clock, latch, active) = part.groups()
            if name in program.parts or name in program.ports or name in program.pins:
                raise PseudocodeError(line.number, f"{name!r} declared twice")
            # The three pins are ordinary OUTPUT pins, so the target resolves
            # them exactly as it resolves any other -- which is what lets a
            # 74HC595 hang off an Arduino or an ATmega as readily as an 8051.
            # Resolved against an EMPTY program of the same device: all we
            # want here is "is this a real pin on this board, and what is it
            # called". A target's resolve_pin also runs its own clash checks,
            # and letting those fire would answer a PART question with a PIN
            # answer -- the reason a user needs is "a PART claims its pins",
            # which the checks just below give.
            scratch = Program(part=program.part)
            claims = [program.target.resolve_pin(
                          scratch, f"{name}_{role}", token, "output", False,
                          line.number)
                      for role, token in (("data", data), ("clock", clock),
                                          ("latch", latch))]
            if len({pin.where for pin in claims}) != 3:
                raise PseudocodeError(
                    line.number, f"{name!r} names the same pin twice; data, clock and "
                                 f"latch must be three different pins")
            for claimed in claims:
                for other in program.pins.values():
                    if other.where == claimed.where:
                        raise PseudocodeError(
                            line.number, f"{claimed.where} is already declared as "
                                         f"{other.name!r}; a PART claims its pins")
                for whole in program.ports.values():
                    if getattr(claimed, "port", None) == whole.port:
                        raise PseudocodeError(
                            line.number, f"{claimed.where} is inside the whole port "
                                         f"{whole.name!r}, which would clobber it")
                for prev in program.parts.values():
                    if claimed.where in prev.claimed_where:
                        raise PseudocodeError(
                            line.number, f"{claimed.where} is already claimed by "
                                         f"{prev.name!r}")
            program.parts[name] = ShiftPart(name, "74hc595", claims[0], claims[1],
                                            claims[2], active == "low")
            index += 1
            continue

        sevenseg = SEVENSEG_RE.fullmatch(lowered)
        if sevenseg and not started:
            require(program, "part", line.number, "a PART")
            (name, seg_port_tok, sel_a, sel_b, sel_c, common) = sevenseg.groups()
            if name in program.parts or name in program.ports or name in program.pins:
                raise PseudocodeError(line.number, f"{name!r} declared twice")
            seg_port = program.target.resolve_port(
                program, f"{name}_seg", seg_port_tok, "output", False, line.number)
            scratch = Program(part=program.part)
            sel_claims = [program.target.resolve_pin(
                              scratch, f"{name}_sel{i}", tok, "output", False,
                              line.number)
                          for i, tok in enumerate((sel_a, sel_b, sel_c))]
            if len({pin.where for pin in sel_claims}) != 3:
                raise PseudocodeError(
                    line.number, f"{name!r} names the same select pin twice")
            for claimed in sel_claims:
                for other in program.pins.values():
                    if other.where == claimed.where:
                        raise PseudocodeError(
                            line.number, f"{claimed.where} is already declared as "
                                         f"{other.name!r}; a PART claims its pins")
                for prev in program.parts.values():
                    if claimed.where in prev.claimed_where:
                        raise PseudocodeError(
                            line.number, f"{claimed.where} is already claimed by "
                                         f"{prev.name!r}")
            for whole in program.ports.values():
                if whole.port == seg_port.port:
                    raise PseudocodeError(
                        line.number, f"{seg_port_tok.upper()} is already declared as "
                                     f"port {whole.name!r}")
            program.parts[name] = SevenSegPart(
                name, "sevenseg8", seg_port.port, sel_claims,
                common_anode=(common == "anode"))
            index += 1
            continue

        ledbank = LEDBANK_RE.fullmatch(lowered)
        if ledbank and not started:
            require(program, "part", line.number, "a PART")
            (name, port_tok, active) = ledbank.groups()
            if name in program.parts or name in program.ports or name in program.pins:
                raise PseudocodeError(line.number, f"{name!r} declared twice")
            led_port = program.target.resolve_port(
                program, f"{name}_port", port_tok, "output", False, line.number)
            # Warn (not error) if the LED port is shared with a sevenseg's select
            for ss in program.parts.values():
                if isinstance(ss, SevenSegPart):
                    for sp in ss.sel_pins:
                        if getattr(sp, "port", None) == led_port.port:
                            import sys
                            print(f"WARNING: {name} on {port_tok.upper()} shares a port "
                                  f"with {ss.name}'s select pins; the 74HC138 address "
                                  f"visibly drives those LEDs, and a whole-port LED "
                                  f"write overwrites the digit address. Use separate "
                                  f"modes/examples.",
                                  file=sys.stderr)
                            break
            program.parts[name] = LedBankPart(
                name, "ledbank8", led_port.port,
                active_low=(active == "low"),
                led_port_where=led_port.where)
            index += 1
            continue

        port = PORT_DECL_RE.fullmatch(lowered)
        if port and not started:
            require(program, "port", line.number, "a whole-port PORT")
            name, where, direction, active = port.groups()
            if name in program.ports or name in program.pins:
                raise PseudocodeError(line.number, f"{name!r} declared twice")
            whole = program.target.resolve_port(
                program, name, where, direction, active == "low", line.number)
            # A PORT and a PIN on the same port would fight: writing the byte
            # clobbers the bit, and neither declaration would look wrong.
            for other in program.pins.values():
                if getattr(other, "port", None) == whole.port:
                    raise PseudocodeError(
                        line.number,
                        f"{where.upper()} is already used one bit at a time, by "
                        f"{other.name!r} ({other.where}); a PORT writes all eight at "
                        f"once and would clobber it")
            for other in program.ports.values():
                if other.port == whole.port:
                    raise PseudocodeError(
                        line.number,
                        f"{where.upper()} is already declared as {other.name!r}")
            # A PART (595, keypad, MATRIX8X8) that claims any pin on this port
            # owns that latch -- a whole-port write would clobber its bits and,
            # for an ISR-scanned part, race the scan on the write-back.
            for part in program.parts.values():
                if any(getattr(p, "port", None) == whole.port for p in part.claimed):
                    raise PseudocodeError(
                        line.number,
                        f"{where.upper()} overlaps pins already claimed by "
                        f"{part.name!r}; a PART owns those latches")
            program.ports[name] = whole
            index += 1
            continue

        table = TABLE_RE.fullmatch(text.strip())
        if table and not started:
            require(program, "table", line.number, "a TABLE")
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
            if direction in ("pwm", "tone"):
                require(program, direction, line.number, f"a {direction.upper()} pin")
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
            program.when_hats.append(None)
            continue

        key_hat = WHEN_KEY_RE.fullmatch(lowered)
        if key_hat:
            n, edge = int(key_hat.group(1)), key_hat.group(2).lower()
            if n > 15:
                raise PseudocodeError(
                    line.number, f"key {n} does not exist; a KEYPAD4X4 has keys 0..15")
            pad = sole_keypad(program, line.number)
            started = True
            block, index = parse_block(lines, index + 1, line.indent, program)
            if not block:
                raise PseudocodeError(
                    line.number, f"'WHEN key {n} {edge}:' block is empty")
            program.whens.append(block)
            program.when_hats.append((pad.name, edge, n))
            continue

        hat = WHEN_PIN_RE.fullmatch(lowered)
        if hat:
            name, edge = hat.group(1), hat.group(2).lower()
            pin = program.pins.get(name)
            if pin is None:
                raise PseudocodeError(
                    line.number, f"unknown pin {name!r}; declare it with PIN before "
                                 f"reacting to it")
            if pin.direction != "input":
                raise PseudocodeError(
                    line.number, f"{name!r} is {pin.direction.upper()} and cannot be "
                                 f"waited on; only an INPUT pin has an edge to react to")
            started = True
            block, index = parse_block(lines, index + 1, line.indent, program)
            if not block:
                raise PseudocodeError(
                    line.number, f"'WHEN {name} {edge}:' block is empty")
            program.whens.append(block)
            program.when_hats.append((pin.name, edge))
            continue

        raise PseudocodeError(
            line.number, f"do not understand {text!r}"
            + ("" if started else " (expected NAME, CLOCK, PIN, DEFINE or "
                                  "WHEN started:)"))

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
    if isinstance(node, KeypadRef):
        return node.part
    if isinstance(node, MatrixPixelRef):
        return f"pixel {expr_pseudo(node.x)} {expr_pseudo(node.y)} is on"
    if isinstance(node, Randint):
        return f"randint({expr_pseudo(node.low)}, {expr_pseudo(node.high)})"
    if isinstance(node, ControllerAxis):
        return f"controller {node.axis}"
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
        if isinstance(node, MatrixClear):
            out.append(f"{pad}clear {node.part}")
        elif isinstance(node, MatrixSetPixel):
            x, y = expr_pseudo(node.x), expr_pseudo(node.y)
            if node.style == "light":
                out.append(f"{pad}light pixel {x} {y}")
            elif node.style == "clear":
                out.append(f"{pad}clear pixel {x} {y}")
            elif node.style in ("on", "off"):
                out.append(f"{pad}set pixel {x} {y} to {node.style}")
            else:
                out.append(f"{pad}set pixel {x} {y} brightness "
                           f"{expr_pseudo(node.level)}")
        elif isinstance(node, MatrixDrawRow):
            out.append(f"{pad}draw row {expr_pseudo(node.y)} = "
                       f"{expr_pseudo(node.bits)}")
        elif isinstance(node, MatrixImage):
            out.append(f"{pad}show image {node.table} on {node.part}")
        elif isinstance(node, MatrixScroll):
            out.append(f"{pad}scroll {node.part} {node.direction}")
        elif isinstance(node, MatrixBrightness):
            out.append(f"{pad}set {node.part} brightness {expr_pseudo(node.level)}")
        elif isinstance(node, SetPart):
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
        elif isinstance(node, ShowNumber):
            out.append(f"{pad}show number {expr_pseudo(node.value)} on {node.display}")
        elif isinstance(node, ShowDigit):
            out.append(f"{pad}show digit {expr_pseudo(node.digit)} = value "
                       f"{expr_pseudo(node.value)} on {node.display}")
        elif isinstance(node, SetDigitSegments):
            out.append(f"{pad}set digit {expr_pseudo(node.digit)} to segments "
                       f"{expr_pseudo(node.segments)} on {node.display}")
        elif isinstance(node, ClearDisplay):
            out.append(f"{pad}clear {node.display}")
        elif isinstance(node, TurnOnLed):
            out.append(f"{pad}turn on led {expr_pseudo(node.index)} on {node.bank}")
        elif isinstance(node, TurnOffLed):
            out.append(f"{pad}turn off led {expr_pseudo(node.index)} on {node.bank}")
        elif isinstance(node, SetLeds):
            out.append(f"{pad}set leds to {expr_pseudo(node.value)} on {node.bank}")
        elif isinstance(node, LightOnlyLed):
            out.append(f"{pad}light only led {expr_pseudo(node.index)} on {node.bank}")
        elif isinstance(node, ArcadeCreate):
            out.append(f"{pad}arcade create {node.sprite} kind {node.kind}")
        elif isinstance(node, ArcadePlace):
            out.append(f"{pad}arcade place {node.sprite} x {expr_pseudo(node.x)} "
                       f"y {expr_pseudo(node.y)}")
        elif isinstance(node, ArcadeMove):
            out.append(f"{pad}arcade move {node.sprite} vx {expr_pseudo(node.vx)} "
                       f"vy {expr_pseudo(node.vy)}")
        elif isinstance(node, ArcadeSetFlag):
            flag = "stay in screen" if node.flag == "stayinscreen" else "destroy on wall"
            out.append(f"{pad}arcade set {node.sprite} {flag}")
        elif isinstance(node, ArcadeScore):
            out.append(f"{pad}arcade score add {expr_pseudo(node.delta)}")
        elif isinstance(node, ArcadeGameOver):
            out.append(f"{pad}arcade game over {'win' if node.win else 'lose'}")
        elif isinstance(node, ArcadeOnOverlap):
            out.append(f"{pad}ARCADE ON OVERLAP {node.kind_a} {node.kind_b}:")
            out += stmts_pseudo(node.body, depth + 1, active_low)
        elif isinstance(node, ArcadeTilemap):
            out.append(f"{pad}arcade tilemap {node.name} cols "
                       f"{expr_pseudo(node.cols)} rows {expr_pseudo(node.rows)} "
                       f"tile {expr_pseudo(node.tile_size)}")
        elif isinstance(node, ArcadeSetTile):
            out.append(f"{pad}arcade set tile {node.tilemap} col "
                       f"{expr_pseudo(node.col)} row {expr_pseudo(node.row)} "
                       f"to {expr_pseudo(node.tile_index)}")
        elif isinstance(node, ArcadeTileWall):
            out.append(f"{pad}arcade set wall {node.tilemap} tile "
                       f"{expr_pseudo(node.tile_index)}")
        elif isinstance(node, ArcadeSetFrame):
            out.append(f"{pad}arcade set frame {node.sprite} to "
                       f"{expr_pseudo(node.frame)}")
        elif isinstance(node, LcdPrint):
            if node.text is not None:
                out.append(f'{pad}lcd print "{node.text}" on {node.display}')
            else:
                out.append(f"{pad}lcd print {expr_pseudo(node.value)} on {node.display}")
        elif isinstance(node, LcdCursor):
            out.append(f"{pad}lcd set cursor {expr_pseudo(node.row)} "
                       f"{expr_pseudo(node.col)} on {node.display}")
        elif isinstance(node, LcdClear):
            out.append(f"{pad}lcd clear {node.display}")
        elif isinstance(node, TftPixel):
            out.append(f"{pad}tft pixel {expr_pseudo(node.x)} {expr_pseudo(node.y)} "
                       f"R {expr_pseudo(node.r)} G {expr_pseudo(node.g)} "
                       f"B {expr_pseudo(node.b)} on {node.display}")
        elif isinstance(node, TftFill):
            out.append(f"{pad}tft fill {expr_pseudo(node.x)} {expr_pseudo(node.y)} "
                       f"{expr_pseudo(node.w)} {expr_pseudo(node.h)} "
                       f"R {expr_pseudo(node.r)} G {expr_pseudo(node.g)} "
                       f"B {expr_pseudo(node.b)} on {node.display}")
        elif isinstance(node, TftClear):
            out.append(f"{pad}tft clear {node.display}")
        elif isinstance(node, TftPrint):
            if node.text is not None:
                out.append(f'{pad}tft print "{node.text}" on {node.display}')
            else:
                out.append(f"{pad}tft print {expr_pseudo(node.value)} on {node.display}")
        elif isinstance(node, TftCursor):
            out.append(f"{pad}tft set cursor {expr_pseudo(node.row)} "
                       f"{expr_pseudo(node.col)} on {node.display}")
        elif isinstance(node, OledPixel):
            out.append(f"{pad}oled pixel {expr_pseudo(node.x)} {expr_pseudo(node.y)} "
                       f"{expr_pseudo(node.value)} on {node.display}")
        elif isinstance(node, OledClear):
            out.append(f"{pad}oled clear {node.display}")
        elif isinstance(node, OledPrint):
            if node.text is not None:
                out.append(f'{pad}oled print "{node.text}" on {node.display}')
            else:
                out.append(f"{pad}oled print {expr_pseudo(node.value)} on {node.display}")
        elif isinstance(node, OledCursor):
            out.append(f"{pad}oled set cursor {expr_pseudo(node.row)} "
                       f"{expr_pseudo(node.col)} on {node.display}")
        elif isinstance(node, RgbSet):
            out.append(f"{pad}set {node.led} colour to R {expr_pseudo(node.r)} "
                       f"G {expr_pseudo(node.g)} B {expr_pseudo(node.b)}")
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
    out = [f"DEVICE {program.part.upper()}:"]
    if program.name:
        out.append(f"  NAME {program.name}")
    out.append(f"  CLOCK {program.clock}")
    if program.tables:
        out.append("")
        for name, values in program.tables.items():
            out.append(f"  TABLE {name} = " + ", ".join(f"0x{v:02X}" for v in values))
    if program.parts:
        out.append("")
        for part in program.parts.values():
            if isinstance(part, KeypadPart):
                rows = " ".join(p.where for p in part.rows)
                cols = " ".join(p.where for p in part.cols)
                out.append(f"  PART {part.name} = KEYPAD4X4 "
                           f"ROWS {rows} COLS {cols}")
                continue
            if isinstance(part, MatrixPart):
                out.append(f"  PART {part.name} = MATRIX8X8 ROWS 74HC595 "
                           f"DATA {part.data.where} CLOCK {part.clock.where} "
                           f"LATCH {part.latch.where} COLUMNS {part.col_port.label}")
                continue
            if isinstance(part, SevenSegPart):
                common = " COMMON ANODE" if part.common_anode else ""
                sel_str = " ".join(pin.where for pin in part.sel_pins)
                out.append(f"  PART {part.name} = SEVENSEG8 SEGMENTS "
                           f"P{part.seg_port} SELECT {sel_str}{common}")
                continue
            if isinstance(part, LedBankPart):
                polarity = " ACTIVE LOW" if part.active_low else ""
                out.append(f"  PART {part.name} = LEDBANK8 ON "
                           f"{part.led_port_where}{polarity}")
                continue
            polarity = " ACTIVE LOW" if part.active_low else ""
            out.append(f"  PART {part.name} = 74HC595 "
                       f"DATA {part.data.where} CLOCK {part.clock.where} "
                       f"LATCH {part.latch.where}{polarity}")
    if program.ports:
        out.append("")
        for port in program.ports.values():
            polarity = " ACTIVE LOW" if port.active_low else ""
            out.append(f"  PORT {port.name} = {port.label} "
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
    for number, block in enumerate(program.whens):
        hat = program.when_hats[number] if number < len(program.when_hats) else None
        if hat is None:
            header = "  WHEN started:"
        elif len(hat) == 3:
            header = f"  WHEN key {hat[2]} {hat[1]}:"
        else:
            header = f"  WHEN {hat[0]} {hat[1]}:"
        out += ["", header]
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
        raw = port.read
        # gcc-avr 5.4 warns "promoted ~unsigned is always non-zero" on
        # `(unsigned char)~PINC > 0` -- and on `0xFF - PINC` and every other
        # spelling, including ones containing no `~` at all. It is looking
        # through the cast at a complement-shaped subexpression, and the cast
        # is exactly what makes the value a byte again. The generated code is
        # right; the warning is not, so build_avr turns that one check off
        # rather than contorting this expression to dodge it.
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
    if isinstance(node, KeypadRef):
        return f"bw_part_{node.part}_read()"
    if isinstance(node, MatrixPixelRef):
        return (f"(bw_scr_{node.part}_getpx((unsigned char)({expr_c(node.x, ctx)}), "
                f"(unsigned char)({expr_c(node.y, ctx)})) != 0)")
    if isinstance(node, Unary):
        inner = expr_c(node.operand, ctx, UNARY_LEVEL)
        return f"!({inner})" if node.op == "not" else f"-({inner})"
    if isinstance(node, Binary):
        level = LEVEL[node.op]
        text = (f"{expr_c(node.left, ctx, level)} {TO_C[node.op]} "
                f"{expr_c(node.right, ctx, level + 1)}")
        return f"({text})" if level < parent_level else text
    raise PseudocodeError(
        0, f"{type(node).__name__} has no C form on the {ctx.target.display}; "
           f"this is a gap in the emitter, not in your program")


def ms_of(node: Wait, ctx: Emit) -> str:
    """A Wait in milliseconds, folded to a constant where it can be.

    Refuses a wait the target's clock cannot express, rather than quietly
    substituting one it can. The floor and the reason both come from the target
    (`wait_floor_ms`), because how short a wait can be is a fact about the board,
    not about this walker.

    Below the floor, unguarded arithmetic rounds to zero and the wait vanishes:
    the loop then runs at full speed with nothing anywhere to say it changed.
    That is the worst kind of wrong — it compiles, it flashes, and it does
    something other than what it says. Rounding up instead is no better; it
    invents time the user did not ask for. So neither: name it and stop.

    The boundary is the floor *inclusive*, because Python rounds halves to even,
    so `round(0.5) == 0`. Half a millisecond is a perfectly reasonable scan dwell
    to write, and it is the case someone actually hits.
    """
    constant = _const_value(node.amount)
    if constant is not None:
        ms = constant * 1000 if node.unit == "seconds" else constant
        folded = int(round(ms))
        floor = ctx.target.wait_floor_ms
        # `wait 0` is a deliberate yield and stays legal; what has to be caught
        # is a nonzero wait collapsing into nothing.
        if ms > 0 and folded == 0:
            raise PseudocodeError(
                node.line,
                f"wait {_trim(ms)} ms is shorter than {ctx.target.display} can "
                f"express: {ctx.target.wait_floor_reason} has "
                f"{_trim(floor)} ms resolution, so this would compile to no "
                f"wait at all. Use {_trim(floor)} ms or more.")
        if ms < 0:
            raise PseudocodeError(
                node.line,
                f"wait {_trim(ms)} ms is negative. A wait count is unsigned, so "
                f"rather than waiting no time this would wrap to a very long "
                f"one.")
        return str(folded)
    inner = expr_c(node.amount, ctx, UNARY_LEVEL)
    return inner if node.unit == "ms" else f"(unsigned int)(({inner}) * 1000)"


def _trim(value: float) -> str:
    """Format a millisecond count without a trailing `.0` on whole numbers."""
    return f"{value:g}"


def _const_value(node: Expr) -> float | None:
    """The literal value of `node`, or None if it is not a constant.

    `wait -1 seconds` parses as Unary('-', Num(1)), not as a negative Num, so a
    plain isinstance check on Num misses it and the negative falls through to the
    runtime path — where `(unsigned int)(-1 * 1000)` wraps to a 64.5-second wait.
    Folding the unary minus here is what makes the negative check reachable at all.
    """
    if isinstance(node, Num):
        return node.value
    if isinstance(node, Unary) and node.op == "-":
        inner = _const_value(node.operand)
        return None if inner is None else -inner
    return None


def matrix_stmt_c(node: Stmt, pad: str, ctx: Emit) -> list[str] | None:
    """Lower a MATRIX8X8 drawing verb to a frame-buffer call, or None if `node`
    is not one. Shared by the straight-line and cooperative back ends, which
    emit these identically -- every verb is a plain RAM write, never a yield."""
    if isinstance(node, MatrixClear):
        return [f"{pad}bw_scr_{node.part}_clear();"]
    if isinstance(node, MatrixSetPixel):
        x, y = expr_c(node.x, ctx), expr_c(node.y, ctx)
        if node.style in ("light", "on"):
            level = "MATRIX_LEVELS - 1"
        elif node.style in ("clear", "off"):
            level = "0"
        else:                                   # "brightness"
            level = f"bw_scr_level({expr_c(node.level, ctx)})"
        return [f"{pad}bw_scr_{node.part}_setpx((unsigned char)({x}), "
                f"(unsigned char)({y}), (unsigned char)({level}));"]
    if isinstance(node, MatrixDrawRow):
        return [f"{pad}bw_scr_{node.part}_row((unsigned char)({expr_c(node.y, ctx)}), "
                f"(unsigned char)({expr_c(node.bits, ctx)}));"]
    if isinstance(node, MatrixImage):
        return [f"{pad}bw_scr_{node.part}_image(bw_tab_{node.table});"]
    if isinstance(node, MatrixScroll):
        code = {"left": 0, "right": 1, "up": 2, "down": 3}[node.direction]
        return [f"{pad}bw_scr_{node.part}_scroll({code});   /* {node.direction} */"]
    if isinstance(node, MatrixBrightness):
        return [f"{pad}bw_scr_{node.part}_dim = bw_scr_level({expr_c(node.level, ctx)});"]
    return None


def a2_stmt_c(node: Stmt, pad: str, ctx: Emit) -> list[str] | None:
    """Lower SEVENSEG8 and LEDBANK8 verbs to C, or None if not one."""
    if isinstance(node, ShowNumber):
        return [f"{pad}bw_{node.display}_show_number({expr_c(node.value, ctx)});"]
    if isinstance(node, ShowDigit):
        return [f"{pad}bw_{node.display}_show_digit("
                f"(unsigned char)({expr_c(node.digit, ctx)}), "
                f"(unsigned char)({expr_c(node.value, ctx)}));"]
    if isinstance(node, SetDigitSegments):
        return [f"{pad}bw_{node.display}_set_segments("
                f"(unsigned char)({expr_c(node.digit, ctx)}), "
                f"(unsigned char)({expr_c(node.segments, ctx)}));"]
    if isinstance(node, ClearDisplay):
        return [f"{pad}bw_{node.display}_clear();"]
    if isinstance(node, TurnOnLed):
        return [f"{pad}bw_{node.bank}_on("
                f"(unsigned char)({expr_c(node.index, ctx)}));"]
    if isinstance(node, TurnOffLed):
        return [f"{pad}bw_{node.bank}_off("
                f"(unsigned char)({expr_c(node.index, ctx)}));"]
    if isinstance(node, SetLeds):
        return [f"{pad}bw_{node.bank}_set("
                f"(unsigned char)({expr_c(node.value, ctx)}));"]
    if isinstance(node, LightOnlyLed):
        return [f"{pad}bw_{node.bank}_only("
                f"(unsigned char)({expr_c(node.index, ctx)}));"]
    return None


def stmts_c(body: list, depth: int, ctx: Emit) -> list[str]:
    pad = "    " * depth
    out = []
    for node in body:
        drawn = matrix_stmt_c(node, pad, ctx)
        if drawn is not None:
            out += drawn
            continue
        drawn = a2_stmt_c(node, pad, ctx)
        if drawn is not None:
            out += drawn
        elif isinstance(node, SetPin):
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
            # `{ }` rather than a bare `;`: an empty statement after a while
            # clause is what -Wmisleading-indentation fires on, since the
            # NEXT generated line is indented as if the loop guarded it. The
            # braces say "this loop has no body" unambiguously, to the
            # compiler and to anyone reading the output.
            out.append(f"{pad}while (!({expr_c(node.cond, ctx)})) {{ }}")
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
        elif isinstance(node, LcdPrint):
            if node.text is not None:
                out.append(f'{pad}bw_lcd_print_s({node.display}, "{_c_string(node.text)}");')
            else:
                out.append(f"{pad}bw_lcd_print_n({node.display}, {expr_c(node.value, ctx)});")
        elif isinstance(node, LcdCursor):
            out.append(f"{pad}bw_lcd_cursor({node.display}, "
                       f"{expr_c(node.row, ctx)}, {expr_c(node.col, ctx)});")
        elif isinstance(node, LcdClear):
            out.append(f"{pad}bw_lcd_clear({node.display});")
        elif isinstance(node, TftPixel):
            out.append(f"{pad}bw_tft_pixel({node.display}, "
                       f"{expr_c(node.x, ctx)}, {expr_c(node.y, ctx)}, "
                       f"{expr_c(node.r, ctx)}, {expr_c(node.g, ctx)}, "
                       f"{expr_c(node.b, ctx)});")
        elif isinstance(node, TftFill):
            out.append(f"{pad}bw_tft_fill({node.display}, "
                       f"{expr_c(node.x, ctx)}, {expr_c(node.y, ctx)}, "
                       f"{expr_c(node.w, ctx)}, {expr_c(node.h, ctx)}, "
                       f"{expr_c(node.r, ctx)}, {expr_c(node.g, ctx)}, "
                       f"{expr_c(node.b, ctx)});")
        elif isinstance(node, TftClear):
            out.append(f"{pad}bw_tft_clear({node.display});")
        elif isinstance(node, TftPrint):
            if node.text is not None:
                out.append(f'{pad}bw_tft_print_s({node.display}, "{_c_string(node.text)}");')
            else:
                out.append(f"{pad}bw_tft_print_n({node.display}, {expr_c(node.value, ctx)});")
        elif isinstance(node, TftCursor):
            out.append(f"{pad}bw_tft_cursor({node.display}, "
                       f"{expr_c(node.row, ctx)}, {expr_c(node.col, ctx)});")
        elif isinstance(node, OledPixel):
            out.append(f"{pad}bw_oled_pixel({node.display}, "
                       f"{expr_c(node.x, ctx)}, {expr_c(node.y, ctx)}, "
                       f"{expr_c(node.value, ctx)});")
        elif isinstance(node, OledClear):
            out.append(f"{pad}bw_oled_clear({node.display});")
        elif isinstance(node, OledPrint):
            if node.text is not None:
                out.append(f'{pad}bw_oled_print_s({node.display}, "{_c_string(node.text)}");')
            else:
                out.append(f"{pad}bw_oled_print_n({node.display}, {expr_c(node.value, ctx)});")
        elif isinstance(node, OledCursor):
            out.append(f"{pad}bw_oled_cursor({node.display}, "
                       f"{expr_c(node.row, ctx)}, {expr_c(node.col, ctx)});")
        elif isinstance(node, RgbSet):
            out.append(f"{pad}bw_rgb_set({node.led}, "
                       f"{expr_c(node.r, ctx)}, {expr_c(node.g, ctx)}, "
                       f"{expr_c(node.b, ctx)});")
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
        drawn = matrix_stmt_c(node, pad, ctx)
        if drawn is not None:
            out += drawn
            continue
        drawn = a2_stmt_c(node, pad, ctx)
        if drawn is not None:
            out += drawn
            continue
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
        elif isinstance(node, LcdPrint):
            if node.text is not None:
                out.append(f'{pad}bw_lcd_print_s({node.display}, "{_c_string(node.text)}");')
            else:
                out.append(f"{pad}bw_lcd_print_n({node.display}, {expr_c(node.value, ctx)});")
        elif isinstance(node, LcdCursor):
            out.append(f"{pad}bw_lcd_cursor({node.display}, "
                       f"{expr_c(node.row, ctx)}, {expr_c(node.col, ctx)});")
        elif isinstance(node, LcdClear):
            out.append(f"{pad}bw_lcd_clear({node.display});")
        elif isinstance(node, TftPixel):
            out.append(f"{pad}bw_tft_pixel({node.display}, "
                       f"{expr_c(node.x, ctx)}, {expr_c(node.y, ctx)}, "
                       f"{expr_c(node.r, ctx)}, {expr_c(node.g, ctx)}, "
                       f"{expr_c(node.b, ctx)});")
        elif isinstance(node, TftFill):
            out.append(f"{pad}bw_tft_fill({node.display}, "
                       f"{expr_c(node.x, ctx)}, {expr_c(node.y, ctx)}, "
                       f"{expr_c(node.w, ctx)}, {expr_c(node.h, ctx)}, "
                       f"{expr_c(node.r, ctx)}, {expr_c(node.g, ctx)}, "
                       f"{expr_c(node.b, ctx)});")
        elif isinstance(node, TftClear):
            out.append(f"{pad}bw_tft_clear({node.display});")
        elif isinstance(node, TftPrint):
            if node.text is not None:
                out.append(f'{pad}bw_tft_print_s({node.display}, "{_c_string(node.text)}");')
            else:
                out.append(f"{pad}bw_tft_print_n({node.display}, {expr_c(node.value, ctx)});")
        elif isinstance(node, TftCursor):
            out.append(f"{pad}bw_tft_cursor({node.display}, "
                       f"{expr_c(node.row, ctx)}, {expr_c(node.col, ctx)});")
        elif isinstance(node, OledPixel):
            out.append(f"{pad}bw_oled_pixel({node.display}, "
                       f"{expr_c(node.x, ctx)}, {expr_c(node.y, ctx)}, "
                       f"{expr_c(node.value, ctx)});")
        elif isinstance(node, OledClear):
            out.append(f"{pad}bw_oled_clear({node.display});")
        elif isinstance(node, OledPrint):
            if node.text is not None:
                out.append(f'{pad}bw_oled_print_s({node.display}, "{_c_string(node.text)}");')
            else:
                out.append(f"{pad}bw_oled_print_n({node.display}, {expr_c(node.value, ctx)});")
        elif isinstance(node, OledCursor):
            out.append(f"{pad}bw_oled_cursor({node.display}, "
                       f"{expr_c(node.row, ctx)}, {expr_c(node.col, ctx)});")
        elif isinstance(node, RgbSet):
            out.append(f"{pad}bw_rgb_set({node.led}, "
                       f"{expr_c(node.r, ctx)}, {expr_c(node.g, ctx)}, "
                       f"{expr_c(node.b, ctx)});")
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
    # A pin hat must be sampled every tick, so it forces the cooperative
    # scheduler even when it is the only script in the program. A MATRIX8X8
    # forces it for the same reason: its refresh lives in the Timer-0 ISR, which
    # only the scheduler path emits.
    tasks = (len(program.whens) > 1 or any(program.when_hats)
             or program.has_matrix)

    # This said "Hand edits will be lost; change the pseudocode instead." The
    # first half is still true. The second stopped being true when BrickWright's
    # C reader landed -- an edited file can be read back into blocks, so telling
    # someone to go and redo the change in the pseudocode is now the long way
    # round. The qualification matters though: that reader lives in sb3-creator,
    # not here, so stc-compiler on its own genuinely is one-directional.
    out = [
        "/* Generated from BrickWright pseudocode by stc-compiler.",
        " * Regenerating overwrites this file. Edits are not stranded, though:",
        " * BrickWright's C reader imports this back into blocks and names what",
        " * it cannot represent. stc-compiler itself only goes forwards. */",
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

        # `WHEN key N` hats share one debounced scan per keypad: a poll task
        # (dispatched before the hats) reads the matrix at most every 5 ms and
        # a key only becomes current after two agreeing reads, so a scan
        # mid-bounce -- which reads -1 or a neighbour for one pass -- cannot
        # fire a hat. The hats then edge-detect on the debounced value exactly
        # the way pin hats edge-detect on a level.
        key_pads = []
        for hat in program.when_hats:
            if hat is not None and len(hat) == 3 and hat[0] not in key_pads:
                key_pads.append(hat[0])
        tt = target.time_type
        now = target.now()
        for pad in key_pads:
            task_lines += [
                f"/* {pad}: debounced key state shared by the `WHEN key N` hats. */",
                f"static signed char bw_kp_{pad}_raw = -1;",
                f"static signed char bw_kp_{pad}_key = -1;",
                f"static {tt} bw_kp_{pad}_t;",
                f"static void bw_kp_{pad}_poll(void)",
                "{",
                "    signed char r;",
                f"    if (({tt})({now} - bw_kp_{pad}_t) < 5)",
                "        return;                     /* scan every 5 ms */",
                f"    bw_kp_{pad}_t = {now};",
                f"    r = bw_part_{pad}_read();",
                f"    if (r == bw_kp_{pad}_raw)",
                f"        bw_kp_{pad}_key = r;",
                f"    bw_kp_{pad}_raw = r;",
                "}", ""]
            task_names.append(f"bw_kp_{pad}_poll")

        for number, block in enumerate(program.whens):
            task = f"bw_task{number}"
            task_names.append(task)
            hat = program.when_hats[number] if number < len(program.when_hats) else None

            # A hat's body starts at case 1; case 0 is the edge test.
            states = [1] if hat else [0]
            body = stmts_task(block, 1, ctx, task, states, statics)
            head = [f"static unsigned int {task}_state;"]
            if has_wait(block):
                head.append(f"static {target.time_type} {task}_until;")

            if hat is None:
                task_lines += head
                task_lines += [f"/* WHEN started: (script {number + 1}) */",
                               f"static void {task}(void)", "{",
                               f"    switch ({task}_state) {{",
                               "    case 0:",
                               *body,
                               "    }",
                               f"    {task}_state = 0xFFFF;   /* ran to the end */",
                               "}", ""]
                continue

            if len(hat) == 3:
                pad, edge, key_n = hat
                test = (f"now && !{task}_prev" if edge == "pressed"
                        else f"!now && {task}_prev")
                head.append(f"static unsigned char {task}_prev;")
                task_lines += head
                task_lines += [
                    f"/* WHEN key {key_n} {edge}: (script {number + 1})",
                    " *",
                    " * Edge-triggered on the DEBOUNCED key from the shared poll task: a",
                    " * held key runs the body once, and a bouncing contact cannot fire",
                    " * twice, because the poll only updates after two agreeing scans. */",
                    f"static void {task}(void)",
                    "{",
                    f"    unsigned char now = (bw_kp_{pad}_key == {key_n}) ? 1 : 0;",
                    f"    unsigned char fired = ({test}) ? 1 : 0;",
                    f"    {task}_prev = now;",
                    "",
                    f"    switch ({task}_state) {{",
                    "    case 0:",
                    "        if (!fired)",
                    "            return;",
                    f"        {task}_state = 1;",
                    "    case 1:",
                    *body,
                    "    }",
                    f"    {task}_state = 0;   /* ready for the next edge */",
                    "}", ""]
                continue

            pin_name, edge = hat
            pin = ctx.pins[pin_name]
            level = target.read_pin(pin)          # polarity-aware: the LOGICAL level
            rising = "pressed" if edge == "pressed" else "released"
            test = (f"now && !{task}_prev" if edge == "pressed"
                    else f"!now && {task}_prev")
            head.append(f"static unsigned char {task}_prev;")
            task_lines += head
            task_lines += [
                f"/* WHEN {pin_name} {rising}: (script {number + 1})",
                " *",
                " * Polled once per dispatch and EDGE-triggered: `_prev` is updated on every",
                " * pass, so a held button runs the body once rather than every millisecond,",
                " * and a release during the body does not queue a second run. The level read",
                " * is the LOGICAL one, so an ACTIVE LOW button reads as pressed when the pin",
                " * is low. */",
                f"static void {task}(void)",
                "{",
                f"    unsigned char now = ({level}) ? 1 : 0;",
                f"    unsigned char fired = ({test}) ? 1 : 0;",
                f"    {task}_prev = now;",
                "",
                f"    switch ({task}_state) {{",
                "    case 0:",
                "        if (!fired)",
                "            return;",
                f"        {task}_state = 1;",
                "    case 1:",
                *body,
                "    }",
                f"    {task}_state = 0;   /* ready for the next edge */",
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

def emit(program: Program) -> str:
    """Generated source for `program`, in whatever language its target uses.

    `emit_c` remains the C back end and the only one most targets need;
    a target that cannot use it supplies its own `emit`."""
    custom = getattr(program.target, "emit", None)
    return custom(program) if custom else emit_c(program)


def source_language(program: Program) -> str:
    """"c" or "python" -- what `emit` just produced, for callers that have to
    label it (a filename, a syntax highlighter, an editor tab)."""
    return "python" if getattr(program.target, "emit", None) else "c"


def transpile(source: str) -> tuple[str, Program]:
    program = parse(source)
    return emit(program), program


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


# Registered last, and imported here rather than above, because bw_micropython
# subclasses Target and everything it needs is defined by this point. Keeping
# it in its own module keeps a 300-line Python back end out of the file that
# every other target shares.
from bw_micropython import MicrobitTarget, PicoTarget  # noqa: E402

TARGETS["microbit"] = MicrobitTarget()
TARGETS["micro-bit"] = TARGETS["microbit"]
TARGETS["pico"] = PicoTarget()
TARGETS["rp2040"] = TARGETS["pico"]

# Arduino Mega 2560: 54 digital + 16 analog, same core as Uno.
TARGETS["arduino-mega"] = ArduinoTarget("arduino-mega", "Arduino Mega", 53, 15)

# ATtiny family: bare AVR (no Arduino core), port/bit pin names.
# The ATtiny48/88 Timer 0 has no TCCR0B and no WGM01: TCCR0A carries the
# prescaler and the CTC enable (CTC0) together.
TARGETS["attiny88"] = PortBitAvrTarget(
    "attiny88", "ATtiny88", "attiny88", 8192, ports="ABCD", default_clock=8000000,
    tick_ctc_bit="CTC0", tick_split=False, uart=False)
# The ATtiny85 is an ATmega Timer 0 with one register renamed.
TARGETS["attiny85"] = PortBitAvrTarget(
    "attiny85", "ATtiny85", "attiny85", 8192, ports="AB", default_clock=8000000,
    timsk="TIMSK", uart=False)

# STC15W408AS: same register layout as STC15F2K, but no Timer 1.
TARGETS["stc15w408as"] = _stc(
    "stc15w408as", "STC15W408AS", "stc12.h", True, True, True, True, p5=True)

# EATER6502 -- the composable 65C02 machine: 32 KB ROM at $8000, a 65C22
# VIA at $6000, RAM below $4000 (eater.cfg, crt0.s).
#
# It was registered as a PortBitAvrTarget, which meant the pseudocode lane
# emitted AVR C for it: <avr/io.h>, ISR(TIMER0_COMPA_vect), DDRA, _BV(). That
# is not code the machine could run, and /compile did not even get that far --
# PortBitAvrTarget's toolchain is "avr-gcc", so the endpoint looked the part
# up in AVR_TARGETS, raised KeyError, and returned a 500.
#
# Emitting nothing is strictly better than emitting confident code for the
# wrong architecture, so the device now refuses at the DEVICE line and says
# where the working lanes are. Writing the generator is real work and is
# scoped rather than hidden: the pins are the VIA's ($6000 PORTB, $6001 PORTA,
# $6002/$6003 the DDRs), and the open question is the millisecond tick --
# VIA Timer 1 in free-run mode wants an IRQ handler, and crt0.s currently
# points IRQ at a bare RTI.
class Eater6502Target(PortBitAvrTarget):
    toolchain = "cc65"
    pseudocode_gap = (
        "the EATER6502 has no pseudocode generator yet -- it would emit code "
        "for the wrong architecture. Its working lanes are hand-written C and "
        "assembly: POST /compile or /assemble with target=\"eater6502\".")


TARGETS["eater6502"] = Eater6502Target(
    "eater6502", "Eater 6502", "eater6502", 32768, ports="AB",
    default_clock=1000000)

from bw_arcade import ArcadeTarget  # noqa: E402
TARGETS["arcade"] = ArcadeTarget()
