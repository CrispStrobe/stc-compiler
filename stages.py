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


def parse_sdas_symbols(listing_text: str) -> dict:
    """Extract symbols from sdas listing's symbol table section."""
    symbols = {}
    in_symbols = False
    for line in listing_text.splitlines():
        if "Symbol Table" in line or "Area Table" in line:
            in_symbols = "Symbol" in line
            continue
        if in_symbols:
            # Symbol lines: name followed by value
            m = re.match(r'^\s+(\S+)\s+([0-9A-Fa-f]{4})\s', line)
            if m:
                name = m.group(1)
                value = int(m.group(2), 16)
                symbols[name] = {"value": value, "resolved": True}
    return symbols


def stages_8051(source: str, listing_text: str) -> dict:
    """Build the stages payload for an 8051/sdas assembly."""
    symbols = parse_sdas_symbols(listing_text)
    return {
        "tokens": tokenize_asm(source),
        "passes": [{"symbols": symbols}] if symbols else [],
        "listing": listing_text,
    }


def stages_z80(source: str, listing_text: str) -> dict:
    """Build the stages payload for a Z80/sdasz80 assembly.
    Same listing format as 8051 (both are SDCC sdas family)."""
    return stages_8051(source, listing_text)


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
