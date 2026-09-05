"""stages.py — assembler debug payload: tokens, symbols, listing.

When debug=True is passed to /assemble, the response gains a `stages`
object with the assembler's internal view:

  stages: {
    tokens:  [{type, text, line, col}],       # lexed source
    passes:  [{symbols: {name: {value, resolved}}}],  # symbol table after link
    listing: str                                # the assembler listing
  }

Each toolchain has its own parser over the tool's native output format.
"""
import re


def tokenize_asm(source: str) -> list[dict]:
    """Tokenize assembly source into [{type, text, line, col}].

    Language-agnostic lexer: recognises labels, directives, instructions,
    registers, numbers, strings, operators, and comments.
    """
    tokens = []
    for lineno, raw in enumerate(source.splitlines(), 1):
        col = 0
        line = raw

        # Strip and record comment
        comment_start = None
        in_string = False
        for i, ch in enumerate(line):
            if ch in ('"', "'") and not in_string:
                in_string = ch
            elif ch == in_string:
                in_string = False
            elif ch == ';' and not in_string:
                comment_start = i
                break
        if comment_start is not None:
            tokens.append({"type": "comment", "text": line[comment_start:],
                           "line": lineno, "col": comment_start + 1})
            line = line[:comment_start]

        pos = 0
        while pos < len(line):
            ch = line[pos]
            if ch in (' ', '\t'):
                pos += 1
                continue

            # String literal
            if ch in ('"', "'"):
                end = line.find(ch, pos + 1)
                if end < 0:
                    end = len(line) - 1
                text = line[pos:end + 1]
                tokens.append({"type": "string", "text": text,
                               "line": lineno, "col": pos + 1})
                pos = end + 1
                continue

            # Label: identifier followed by ':'
            m = re.match(r'([A-Za-z_.][A-Za-z0-9_.]*)\s*:', line[pos:])
            if m and pos == 0:
                tokens.append({"type": "label", "text": m.group(1),
                               "line": lineno, "col": pos + 1})
                pos += m.end()
                continue

            # Directive: starts with '.'
            if ch == '.':
                m = re.match(r'\.\w+', line[pos:])
                if m:
                    tokens.append({"type": "directive", "text": m.group(),
                                   "line": lineno, "col": pos + 1})
                    pos += m.end()
                    continue

            # Number: hex, binary, decimal
            m = re.match(
                r'(?:\$[0-9A-Fa-f]+|0[xX][0-9A-Fa-f]+|0[bB][01]+|#?\$?[0-9][0-9A-Fa-f]*[hH]?|\d+)',
                line[pos:])
            if m:
                tokens.append({"type": "number", "text": m.group(),
                               "line": lineno, "col": pos + 1})
                pos += m.end()
                continue

            # Identifier (instruction, register, symbol)
            m = re.match(r'[A-Za-z_][A-Za-z0-9_]*', line[pos:])
            if m:
                text = m.group()
                # Classify: is it a well-known instruction or a symbol?
                tok_type = "identifier"
                tokens.append({"type": tok_type, "text": text,
                               "line": lineno, "col": pos + 1})
                pos += m.end()
                continue

            # Operator / punctuation
            if ch in '(),+-*/<>|&^~!=%#@':
                tokens.append({"type": "operator", "text": ch,
                               "line": lineno, "col": pos + 1})
                pos += 1
                continue

            pos += 1

    return tokens


# ---- ca65 / ld65 (6502) ----

def parse_ca65_listing(listing_text: str) -> list[dict]:
    """Parse ca65 listing into address/byte/source triples."""
    entries = []
    for line in listing_text.splitlines():
        # Format: AAAAAA[r] N  [bytes]  source
        m = re.match(
            r'^([0-9A-Fa-f]{6})[r ]?\s+(\d+)\s+((?:[0-9A-Fa-f]{2}\s*)*)(.*)',
            line)
        if m:
            addr = int(m.group(1), 16)
            line_num = int(m.group(2))
            byte_str = m.group(3).strip()
            source = m.group(4).rstrip()
            entry = {"addr": addr, "line": line_num, "source": source}
            if byte_str:
                entry["bytes"] = [int(b, 16) for b in byte_str.split()]
            entries.append(entry)
    return entries


def parse_ca65_dbg(dbg_text: str) -> dict:
    """Parse ld65 --dbgfile output into a symbol table.

    Returns {name: {value, resolved, type, scope}} for each symbol.
    """
    symbols = {}
    for line in dbg_text.splitlines():
        if not line.startswith("sym\t"):
            continue
        fields = {}
        for part in line[4:].split(","):
            kv = part.strip().split("=", 1)
            if len(kv) == 2:
                fields[kv[0]] = kv[1]
        name = fields.get("name", "").strip('"')
        if not name:
            continue
        val_str = fields.get("val", "0")
        try:
            value = int(val_str, 0)
        except ValueError:
            value = 0
        symbols[name] = {
            "value": value,
            "resolved": True,
            "type": fields.get("type", ""),
            "scope": fields.get("scope", ""),
        }
    return symbols


def stages_6502(source: str, listing_text: str, dbg_text: str | None) -> dict:
    """Build the stages payload for a 6502/ca65 assembly."""
    result = {
        "tokens": tokenize_asm(source),
        "passes": [],
        "listing": listing_text,
    }
    if dbg_text:
        symbols = parse_ca65_dbg(dbg_text)
        result["passes"].append({"symbols": symbols})
    return result


# ---- sdas8051 / sdldz80 (8051 / Z80) ----

def parse_sdas_listing(listing_text: str) -> list[dict]:
    """Parse sdas8051/sdasz80 .lst listing."""
    entries = []
    for line in listing_text.splitlines():
        # Format varies but typically:
        #    1 0000 02 01 00   ljmp start
        m = re.match(
            r'^\s*(\d+)\s+([0-9A-Fa-f]{4})\s+((?:[0-9A-Fa-f]{2}\s*)*)(.*)',
            line)
        if m:
            line_num = int(m.group(1))
            addr = int(m.group(2), 16)
            byte_str = m.group(3).strip()
            source = m.group(4).rstrip()
            entry = {"addr": addr, "line": line_num, "source": source}
            if byte_str:
                entry["bytes"] = [int(b, 16) for b in byte_str.split()]
            entries.append(entry)
    return entries


# One symbol as sdas writes it. Two forms share the table:
#
#     P1      =  000090 L       an equate -- the built-in SFR names, and any
#                               the source defines with `=`
#   3 start      000100 R       an area-relative label: area index, name,
#                               offset, R for relocatable
#   0 _main      000026 GR      BOTH: exported (G) and area-relative (R)
#
# The flags are a SET, not one letter. Reading only one letter dropped every
# `GR` symbol on the floor -- which is to say every exported function, the
# ones a debugger actually wants, while keeping the file-static helpers next
# to them. Measured 2026-09-05 on `sdcc -mz80` output, where `_main` was
# missing from the stages payload and `_delay_ms` was present.
#
# Six hex digits (the header says "Hexadecimal [24-Bits]"), and up to three
# of these packed per line separated by `|`.
_SDAS_SYMBOL = re.compile(
    r"^\s*(?:(\d+)\s+)?"          # optional area index (relocatable symbols)
    r"([A-Za-z_.$][\w.$]*)\s*"     # name
    r"(=)?\s+"                     # `=` marks an equate
    r"([0-9A-Fa-f]{4,8})\s+"       # value
    r"([A-Za-z]{1,2})\s*$"         # flags: G global, L local, R relocatable
)


def parse_sdas_symbols(sym_text: str) -> dict:
    """Extract symbols from an sdas `.sym` file.

    This wants the .sym, NOT the .lst. sdas writes the symbol table into its
    own file (the `s` in -plosgff asks for it) and the listing has no such
    section at all -- so feeding this the listing, which is what happened
    until 2026-09-02, always returned {} and the /assemble stages payload
    shipped `passes: []` for every 8051 and Z80 request in production.

    Nothing caught it because the 8051 and Z80 stage tests asserted on tokens
    and on the listing, never on the symbols.
    """
    symbols = {}
    in_symbols = False
    for line in sym_text.splitlines():
        if "Symbol Table" in line:
            in_symbols = True
            continue
        if "Area Table" in line:
            in_symbols = False
            continue
        if not in_symbols:
            continue
        # Up to three symbols per line, `|`-separated.
        for column in line.split("|"):
            m = _SDAS_SYMBOL.match(column)
            if not m:
                continue
            area, name, equate, value, scope = m.groups()
            symbols[name] = {
                "value": int(value, 16),
                "resolved": True,
                # An equate is absolute; an area-relative label is only final
                # after the linker has placed its area, and the offset here is
                # within that area.
                "kind": "equate" if equate else "label",
                # G wins over R when both are set: "global" is the fact a
                # caller acts on, and `area` below already says relocatable.
                "scope": ("global" if "G" in scope
                          else "local" if "L" in scope
                          else "relocatable"),
            }
            if area is not None:
                symbols[name]["area"] = int(area)
    return symbols


def stages_8051(source: str, listing_text: str, sym_text: str = "") -> dict:
    """Build the stages payload for an 8051/sdas assembly.

    `sym_text` is the .sym file. It is a separate argument because the
    symbols are in a separate FILE -- passing the listing twice is what the
    original did, in effect, and it silently produced no symbols at all.
    """
    symbols = parse_sdas_symbols(sym_text)
    return {
        "tokens": tokenize_asm(source),
        "passes": [{"symbols": symbols}] if symbols else [],
        "listing": listing_text,
    }


def stages_z80(source: str, listing_text: str, sym_text: str = "") -> dict:
    """Build the stages payload for a Z80/sdasz80 assembly.
    Same listing and .sym format as 8051 (both are SDCC sdas family)."""
    return stages_8051(source, listing_text, sym_text)


# ---- gcc family (AVR, ARM) ----

def parse_nm_symbols(nm_text: str) -> dict:
    """Parse nm -S output into a symbol table.

    nm format: addr [size] type name
    Example: 00000080 00000004 T main
    """
    symbols = {}
    for line in nm_text.splitlines():
        parts = line.strip().split()
        if len(parts) < 3:
            continue
        # With -S: addr size type name
        # Without -S: addr type name
        if len(parts) >= 4 and len(parts[0]) >= 4:
            addr_str, _size, sym_type, name = parts[0], parts[1], parts[2], parts[3]
        elif len(parts) == 3:
            addr_str, sym_type, name = parts
        else:
            continue
        try:
            value = int(addr_str, 16)
        except ValueError:
            continue
        # Skip internal/compiler symbols
        if name.startswith("__") or name.startswith("."):
            continue
        symbols[name] = {
            "value": value,
            "resolved": True,
            "type": sym_type,
        }
    return symbols


def stages_gcc(source: str, nm_text: str,
               listing_artifact: dict | None) -> dict:
    """Build the stages payload for a gcc-family assembly (AVR, ARM).

    Tokens from the source; symbols from nm; listing from the existing
    objdump-based listing artifact.
    """
    symbols = parse_nm_symbols(nm_text)
    listing_text = ""
    if listing_artifact and isinstance(listing_artifact, dict):
        listing_text = listing_artifact.get("asm", "")
    return {
        "tokens": tokenize_asm(source),
        "passes": [{"symbols": symbols}] if symbols else [],
        "listing": listing_text,
    }
