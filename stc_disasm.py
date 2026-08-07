"""
stc_disasm — an MCS-51 (8051) disassembler, for checking what actually landed
in the image rather than trusting the compiler.

The instruction table is built programmatically from the encoding's own
regularities (the `Rn` runs, the `@Ri` pairs, the `addr11` ladders) instead of
being typed out 256 times, because a hand-written table is exactly the kind of
thing that carries a silent typo in the one opcode you never test.

Verified against SDCC's own assembly listing for the same program: see
`scripts/test-disasm.py`.
"""

from __future__ import annotations

import re

# ------------------------------------------------------------------ SFR names
# STC12C5A60S2. Addresses cross-checked against SDCC's mcs51/stc12.h.
SFR = {
    0x80: "P0", 0x81: "SP", 0x82: "DPL", 0x83: "DPH", 0x87: "PCON",
    0x88: "TCON", 0x89: "TMOD", 0x8A: "TL0", 0x8B: "TL1", 0x8C: "TH0",
    0x8D: "TH1", 0x8E: "AUXR", 0x90: "P1", 0x91: "P1M1", 0x92: "P1M0",
    0x93: "P0M1", 0x94: "P0M0", 0x95: "P2M1", 0x96: "P2M0", 0x97: "CLK_DIV",
    0x98: "SCON", 0x99: "SBUF", 0x9A: "S2CON", 0x9B: "S2BUF", 0x9C: "BRT",
    0x9D: "P1ASF", 0xA0: "P2", 0xA2: "AUXR1", 0xA8: "IE", 0xA9: "SADDR",
    0xB0: "P3", 0xB1: "P3M1", 0xB2: "P3M0", 0xB3: "P4M1", 0xB4: "P4M0",
    0xB6: "IP2H", 0xB7: "IPH", 0xB8: "IP", 0xB9: "SADEN", 0xBB: "P4SW",
    0xBC: "ADC_CONTR", 0xBD: "ADC_RES", 0xBE: "ADC_RESL", 0xC0: "P4",
    0xC8: "P5", 0xC9: "P5M1", 0xCA: "P5M0", 0xD0: "PSW", 0xD8: "CCON",
    0xD9: "CMOD", 0xDA: "CCAPM0", 0xDB: "CCAPM1", 0xE0: "ACC", 0xE9: "CL",
    0xF0: "B", 0xF2: "PCA_PWM0", 0xF3: "PCA_PWM1", 0xF9: "CH",
    0xFA: "CCAP0H", 0xFB: "CCAP1H",
}

# Bit-addressable SFRs live at 0x80, 0x88, ... 0xF8; bits 0x80-0xFF map onto
# them eight at a time. Below 0x80 the bits belong to RAM 0x20-0x2F.
BIT_NAMES = {
    0x88: "IT0", 0x89: "IE0", 0x8A: "IT1", 0x8B: "IE1",
    0x8C: "TR0", 0x8D: "TF0", 0x8E: "TR1", 0x8F: "TF1",
    0xAF: "EA", 0xAC: "ES", 0xA9: "ET0", 0xAB: "ET1", 0xA8: "EX0", 0xAA: "EX1",
    0xD7: "CY", 0xD6: "AC", 0xD2: "OV", 0xD0: "P",
    0x98: "RI", 0x99: "TI", 0x9A: "RB8", 0x9B: "TB8",
}


# Symbolic output is for humans. Turn it off and every operand becomes numeric,
# which is what an assembler needs -- see scripts/test-reassemble.py, where the
# disassembly is fed back through sdas8051 and the bytes compared.
SYMBOLIC = True


def direct(address: int) -> str:
    if not SYMBOLIC:
        return f"0x{address:02X}"
    return SFR.get(address, f"0x{address:02X}")


def bit(address: int) -> str:
    if not SYMBOLIC:
        return f"0x{address:02X}"
    if address in BIT_NAMES:
        return BIT_NAMES[address]
    if address >= 0x80:
        base, index = address & 0xF8, address & 0x07
        name = SFR.get(base)
        return f"{name}.{index}" if name else f"0x{address:02X}"
    byte, index = 0x20 + (address >> 3), address & 0x07
    return f"0x{byte:02X}.{index}"


# ------------------------------------------------------- the instruction table
# Each entry: (length, formatter). The formatter receives the operand bytes and
# the address of the *following* instruction (for relative branches).

def _build() -> dict[int, tuple[int, object]]:
    table: dict[int, tuple[int, object]] = {}

    def put(opcode, length, render):
        table[opcode] = (length, render)

    def imm(value):
        return f"#0x{value:02X}"

    # --- families that repeat across a run of registers ---------------------
    # base+0x04 = A,#imm | +0x05 = A,direct | +0x06/07 = A,@Ri | +0x08..0F = A,Rn
    for base, name in ((0x20, "ADD"), (0x30, "ADDC"), (0x40, "ORL"),
                       (0x50, "ANL"), (0x60, "XRL"), (0x90, "SUBB")):
        put(base + 0x04, 2, lambda o, n, m=name: f"{m}   A,{imm(o[0])}")
        put(base + 0x05, 2, lambda o, n, m=name: f"{m}   A,{direct(o[0])}")
        for i in range(2):
            put(base + 0x06 + i, 1, lambda o, n, m=name, i=i: f"{m}   A,@R{i}")
        for r in range(8):
            put(base + 0x08 + r, 1, lambda o, n, m=name, r=r: f"{m}   A,R{r}")

    for base, name in ((0x00, "INC"), (0x10, "DEC")):
        put(base + 0x04, 1, lambda o, n, m=name: f"{m}   A")
        put(base + 0x05, 2, lambda o, n, m=name: f"{m}   {direct(o[0])}")
        for i in range(2):
            put(base + 0x06 + i, 1, lambda o, n, m=name, i=i: f"{m}   @R{i}")
        for r in range(8):
            put(base + 0x08 + r, 1, lambda o, n, m=name, r=r: f"{m}   R{r}")

    # MOV A,<src> and MOV <dst>,A
    put(0xE5, 2, lambda o, n: f"MOV   A,{direct(o[0])}")
    put(0xF5, 2, lambda o, n: f"MOV   {direct(o[0])},A")
    put(0x74, 2, lambda o, n: f"MOV   A,{imm(o[0])}")
    put(0x75, 3, lambda o, n: f"MOV   {direct(o[0])},{imm(o[1])}")
    put(0x85, 3, lambda o, n: f"MOV   {direct(o[1])},{direct(o[0])}")
    for i in range(2):
        put(0xE6 + i, 1, lambda o, n, i=i: f"MOV   A,@R{i}")
        put(0xF6 + i, 1, lambda o, n, i=i: f"MOV   @R{i},A")
        put(0x76 + i, 2, lambda o, n, i=i: f"MOV   @R{i},{imm(o[0])}")
        put(0x86 + i, 2, lambda o, n, i=i: f"MOV   {direct(o[0])},@R{i}")
        put(0xA6 + i, 2, lambda o, n, i=i: f"MOV   @R{i},{direct(o[0])}")
    for r in range(8):
        put(0xE8 + r, 1, lambda o, n, r=r: f"MOV   A,R{r}")
        put(0xF8 + r, 1, lambda o, n, r=r: f"MOV   R{r},A")
        put(0x78 + r, 2, lambda o, n, r=r: f"MOV   R{r},{imm(o[0])}")
        put(0x88 + r, 2, lambda o, n, r=r: f"MOV   {direct(o[0])},R{r}")
        put(0xA8 + r, 2, lambda o, n, r=r: f"MOV   R{r},{direct(o[0])}")

    # XCH / XCHD / DJNZ / CJNE
    put(0xC5, 2, lambda o, n: f"XCH   A,{direct(o[0])}")
    for i in range(2):
        put(0xC6 + i, 1, lambda o, n, i=i: f"XCH   A,@R{i}")
        put(0xD6 + i, 1, lambda o, n, i=i: f"XCHD  A,@R{i}")
    for r in range(8):
        put(0xC8 + r, 1, lambda o, n, r=r: f"XCH   A,R{r}")
        put(0xD8 + r, 2, lambda o, n, r=r: f"DJNZ  R{r},0x{rel(o[0], n):04X}")
    put(0xD5, 3, lambda o, n: f"DJNZ  {direct(o[0])},0x{rel(o[1], n):04X}")
    put(0xB4, 3, lambda o, n: f"CJNE  A,{imm(o[0])},0x{rel(o[1], n):04X}")
    put(0xB5, 3, lambda o, n: f"CJNE  A,{direct(o[0])},0x{rel(o[1], n):04X}")
    for i in range(2):
        put(0xB6 + i, 3, lambda o, n, i=i: f"CJNE  @R{i},{imm(o[0])},0x{rel(o[1], n):04X}")
    for r in range(8):
        put(0xB8 + r, 3, lambda o, n, r=r: f"CJNE  R{r},{imm(o[0])},0x{rel(o[1], n):04X}")

    # AJMP / ACALL ladders: the top three bits of the opcode carry addr11's high bits.
    for page in range(8):
        put(0x01 + (page << 5), 2,
            lambda o, n, p=page: f"AJMP  0x{((n & 0xF800) | (p << 8) | o[0]):04X}")
        put(0x11 + (page << 5), 2,
            lambda o, n, p=page: f"ACALL 0x{((n & 0xF800) | (p << 8) | o[0]):04X}")

    # --- one-off opcodes ----------------------------------------------------
    singles = {
        0x00: (1, "NOP"), 0x22: (1, "RET"), 0x32: (1, "RETI"),
        0x03: (1, "RR    A"), 0x13: (1, "RRC   A"),
        0x23: (1, "RL    A"), 0x33: (1, "RLC   A"),
        0x84: (1, "DIV   AB"), 0xA4: (1, "MUL   AB"),
        0xC4: (1, "SWAP  A"), 0xD4: (1, "DA    A"),
        0xE4: (1, "CLR   A"), 0xF4: (1, "CPL   A"),
        0xA3: (1, "INC   DPTR"), 0x73: (1, "JMP   @A+DPTR"),
        0x83: (1, "MOVC  A,@A+PC"), 0x93: (1, "MOVC  A,@A+DPTR"),
        0xE0: (1, "MOVX  A,@DPTR"), 0xF0: (1, "MOVX  @DPTR,A"),
        0xB3: (1, "CPL   C"), 0xC3: (1, "CLR   C"), 0xD3: (1, "SETB  C"),
        0xA5: (1, ".db   0xA5"),   # reserved, never emitted by a compiler
    }
    for opcode, (length, text) in singles.items():
        put(opcode, length, lambda o, n, t=text: t)
    for i in range(2):
        put(0xE2 + i, 1, lambda o, n, i=i: f"MOVX  A,@R{i}")
        put(0xF2 + i, 1, lambda o, n, i=i: f"MOVX  @R{i},A")

    put(0x02, 3, lambda o, n: f"LJMP  0x{(o[0] << 8 | o[1]):04X}")
    put(0x12, 3, lambda o, n: f"LCALL 0x{(o[0] << 8 | o[1]):04X}")
    put(0x90, 3, lambda o, n: f"MOV   DPTR,#0x{(o[0] << 8 | o[1]):04X}")

    # direct-operand logic
    put(0x42, 2, lambda o, n: f"ORL   {direct(o[0])},A")
    put(0x43, 3, lambda o, n: f"ORL   {direct(o[0])},{imm(o[1])}")
    put(0x52, 2, lambda o, n: f"ANL   {direct(o[0])},A")
    put(0x53, 3, lambda o, n: f"ANL   {direct(o[0])},{imm(o[1])}")
    put(0x62, 2, lambda o, n: f"XRL   {direct(o[0])},A")
    put(0x63, 3, lambda o, n: f"XRL   {direct(o[0])},{imm(o[1])}")
    put(0xC0, 2, lambda o, n: f"PUSH  {direct(o[0])}")
    put(0xD0, 2, lambda o, n: f"POP   {direct(o[0])}")

    # bit operations
    put(0x72, 2, lambda o, n: f"ORL   C,{bit(o[0])}")
    put(0x82, 2, lambda o, n: f"ANL   C,{bit(o[0])}")
    put(0xA0, 2, lambda o, n: f"ORL   C,/{bit(o[0])}")
    put(0xB0, 2, lambda o, n: f"ANL   C,/{bit(o[0])}")
    put(0x92, 2, lambda o, n: f"MOV   {bit(o[0])},C")
    put(0xA2, 2, lambda o, n: f"MOV   C,{bit(o[0])}")
    put(0xB2, 2, lambda o, n: f"CPL   {bit(o[0])}")
    put(0xC2, 2, lambda o, n: f"CLR   {bit(o[0])}")
    put(0xD2, 2, lambda o, n: f"SETB  {bit(o[0])}")

    # branches
    put(0x10, 3, lambda o, n: f"JBC   {bit(o[0])},0x{rel(o[1], n):04X}")
    put(0x20, 3, lambda o, n: f"JB    {bit(o[0])},0x{rel(o[1], n):04X}")
    put(0x30, 3, lambda o, n: f"JNB   {bit(o[0])},0x{rel(o[1], n):04X}")
    put(0x40, 2, lambda o, n: f"JC    0x{rel(o[0], n):04X}")
    put(0x50, 2, lambda o, n: f"JNC   0x{rel(o[0], n):04X}")
    put(0x60, 2, lambda o, n: f"JZ    0x{rel(o[0], n):04X}")
    put(0x70, 2, lambda o, n: f"JNZ   0x{rel(o[0], n):04X}")
    put(0x80, 2, lambda o, n: f"SJMP  0x{rel(o[0], n):04X}")
    return table


def rel(offset: int, next_address: int) -> int:
    """Signed 8-bit displacement from the address after the instruction."""
    return (next_address + (offset - 256 if offset > 127 else offset)) & 0xFFFF


TABLE = _build()
assert len(TABLE) == 256, f"instruction table has {len(TABLE)} entries, not 256"


# ----------------------------------------------------------------- Intel HEX

def parse_hex(text: str) -> dict[int, int]:
    """Intel HEX -> {address: byte}, verifying every record checksum."""
    memory: dict[int, int] = {}
    base = 0
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        if not line.startswith(":"):
            raise ValueError(f"line {number}: missing ':'")
        raw = bytes.fromhex(line[1:])
        if (sum(raw) & 0xFF) != 0:
            raise ValueError(f"line {number}: bad checksum")
        count, address, kind = raw[0], (raw[1] << 8) | raw[2], raw[3]
        payload = raw[4:4 + count]
        if kind == 0x00:
            for index, value in enumerate(payload):
                memory[base + address + index] = value
        elif kind == 0x01:
            break
        elif kind == 0x04:
            base = ((payload[0] << 8) | payload[1]) << 16
        elif kind == 0x02:
            base = ((payload[0] << 8) | payload[1]) << 4
    return memory


# ------------------------------------------------------------------ disassemble

# Mnemonics whose 16-bit-looking operand is a real control-flow target. Only
# these seed sync points: a `MOV DPTR, #imm16` is as often a table base as a
# code address, and anchoring on it could split live instructions.
_BRANCHES = ("LJMP", "AJMP", "SJMP", "LCALL", "ACALL", "JZ", "JNZ", "JC",
             "JNC", "JB", "JNB", "JBC", "CJNE", "DJNZ")


def _sweep(memory: dict[int, int], first: int, last: int,
           anchors: set[int]) -> tuple[list, set, set]:
    """One linear pass. Anchors are addresses proven to be instruction
    starts; an instruction that would span one is cut short and emitted as
    `.db` bytes, and decoding re-phases at the anchor."""
    entries: list[tuple[int, bytes, str]] = []
    starts: set[int] = set()
    targets: set[int] = set()
    address = first
    while address <= last:
        opcode = memory.get(address)
        if opcode is None:
            address += 1
            continue
        length, render = TABLE[opcode]
        conflict = next((a for a in range(address + 1, address + length)
                         if a in anchors), None)
        if conflict is not None:
            raw = bytes(memory.get(a, 0) for a in range(address, conflict))
            entries.append((address, raw,
                            ".db   " + ", ".join(f"0x{b:02X}" for b in raw)))
            address = conflict
            continue
        raw = bytes(memory.get(address + i, 0) for i in range(length))
        following = (address + length) & 0xFFFF
        try:
            text = render(raw[1:], following)
        except IndexError:
            text = f".db   0x{opcode:02X}"    # truncated at the end of the image
        entries.append((address, raw, text))
        starts.add(address)
        if text.startswith(_BRANCHES):
            for value in re.findall(r"0x([0-9A-Fa-f]{4})\b", text):
                targets.add(int(value, 16))
        address += length
    return entries, starts, targets


def disassemble(memory: dict[int, int], start: int | None = None,
                end: int | None = None,
                sync: bool = True) -> list[tuple[int, bytes, str]]:
    """Linear sweep with branch-target sync points.

    Linear because SDCC lays code out contiguously and a sweep is what you
    want when comparing against a compiler listing. Keil, though, interleaves
    jump and font tables between functions; data in the code stream decodes
    as nonsense and desynchronises every boundary after it. The fix uses the
    branches themselves as ground truth: a target landing mid-instruction
    *proves* the sweep is out of phase there, so the target becomes an anchor
    -- decoding restarts at it and the orphaned bytes before it are emitted
    as `.db`. Iterate until no new anchors appear (anchors only grow, so this
    terminates). Images with no embedded data never gain an anchor and come
    out exactly as the plain sweep did.
    """
    if not memory:
        return []
    first = min(memory) if start is None else start
    last = max(memory) if end is None else end
    anchors: set[int] = set()
    while True:
        entries, starts, targets = _sweep(memory, first, last, anchors)
        if not sync:
            return entries
        fresh = {t for t in targets
                 if first <= t <= last and t in memory
                 and t not in starts and t not in anchors}
        if not fresh:
            return entries
        anchors |= fresh


def format_listing(entries: list[tuple[int, bytes, str]]) -> str:
    """`address  bytes            mnemonic` — the shape every 8051 tool uses."""
    lines = []
    for address, raw, text in entries:
        encoded = " ".join(f"{b:02X}" for b in raw)
        lines.append(f"{address:04X}  {encoded:<11}  {text}")
    return "\n".join(lines)


def disassemble_hex(text: str) -> str:
    return format_listing(disassemble(parse_hex(text)))
