"""
keil2sdcc — translate Keil C51 source into something SDCC will compile.

Almost every STC12 project in the wild is a Keil µVision project, and SDCC
rejects the Keil dialect outright. The two disagree on syntax, not semantics,
so a source-level translation gets most code across.

What gets rewritten, in the order the constructs actually turn up in a survey
of 378 files from 36 public STC12 projects:

    sbit  LED = P1^0;          1225x   -> __sbit __at (0x90) LED;
    sfr   ADC_CONTR = 0xBC;     779x   -> __sfr  __at (0xBC) ADC_CONTR;
    _nop_()                     247x   -> shim in keil-shim/intrins.h
    xdata / idata / pdata       237x   -> __xdata / __idata / __pdata
    code  (storage class)       109x   -> __code
    bit   flag;                  94x   -> __bit flag;
    ... interrupt 1 using 2      60x   -> ... __interrupt(1) __using(2)
    int x _at_ 0x30;             65x   -> __at (0x30) int x;
    reentrant                    29x   -> __reentrant

`sbit` is the interesting one: Keil writes the bit as `SFR^n`, so the SFR's
address has to be known to compute `addr + n`. Addresses come from SDCC's own
`mcs51/stc12.h` (properly licensed, and the authority for this part) plus any
`sfr` declared earlier in the same file.

Bare `data` is deliberately left alone. It is a storage class in Keil but also
an extremely common variable name, and the survey found only 4 uses of it as a
specifier against many as an identifier -- not worth the false positives.
"""

from __future__ import annotations

import os
import pathlib
import re

SHIM_DIR = pathlib.Path(__file__).parent / "keil-shim"

# Where SDCC's own mcs51 headers live. They are the source of every SFR address
# the sbit rewrite needs, so getting this wrong silently turns every
# `sbit LED = P1^0;` into an unresolved one. SDCC_INCLUDE_DIR lets a deployment
# point at a staged toolchain -- on Vercel the compiler is copied to /tmp.
SDCC_INCLUDE_DIRS = [
    os.environ["SDCC_INCLUDE_DIR"]] if os.environ.get("SDCC_INCLUDE_DIR") else []
SDCC_INCLUDE_DIRS += [
    "/tmp/sdcc/share/sdcc/include/mcs51",
    "/opt/homebrew/share/sdcc/include/mcs51",
    "/usr/share/sdcc/include/mcs51",
    "/usr/local/share/sdcc/include/mcs51",
]

# Headers Keil projects include that SDCC has no equivalent for. Mapped rather
# than vendored: the vendor headers carry no licence, and SDCC's own stc12.h
# already declares the same register names.
HEADER_MAP = {
    "stc12c5a60s2.h": "keil-stc12.h",
    "stc12c5a.h": "keil-stc12.h",
    "stc12c5a60s2_p.h": "keil-stc12.h",
    "stc12c5a60ad.h": "keil-stc12.h",
    "stc12c5a56s2.h": "keil-stc12.h",
    # Deliberately NOT mapped: stc15*.h. The STC15 puts Timer 2 at 0xD6/0xD7
    # where the 8052 (and SDCC's stc12.h) has 0xCC/0xCD, so redirecting it here
    # would produce code that compiles and writes to the wrong registers.
    "reg51.h": "keil-reg51.h",
    "reg52.h": "keil-reg52.h",
    "at89x51.h": "keil-reg51.h",
    "at89x52.h": "keil-reg52.h",
}

STORAGE = ("xdata", "idata", "pdata", "bdata", "code")


def provided_bits(include_shim: bool = True) -> dict[str, int]:
    """Every __sbit SDCC's headers and our compat shim already declare.

    A Keil project routinely declares its own `sbit P13 = P1^3;`. Once the
    shim provides the same name at the same address, keeping both is a
    duplicate-symbol error -- so the translator drops the redundant one.
    """
    out: dict[str, int] = {}
    paths = list(SHIM_DIR.glob("*.h")) if (include_shim and SHIM_DIR.is_dir()) else []
    for base in SDCC_INCLUDE_DIRS:
        directory = pathlib.Path(base)
        if directory.is_dir():
            paths += [directory / "stc12.h", directory / "8052.h", directory / "8051.h"]
            break
    for path in paths:
        if not path.exists():
            continue
        text = path.read_text(errors="replace")
        for value, name in re.findall(
                r"__sbit\s+__at\s*\(?\s*(0x[0-9A-Fa-f]+)\s*\)?\s*(\w+)", text):
            out.setdefault(name, int(value, 16))
        for name, base_addr, index in re.findall(
                r"SBIT\s*\(\s*(\w+)\s*,\s*(0x[0-9A-Fa-f]+)\s*,\s*(\d+)", text):
            out.setdefault(name, int(base_addr, 16) + int(index))
    return out


def sfr_addresses(header: pathlib.Path | None = None) -> dict[str, int]:
    """Parse `SFR(NAME, 0xNN);` out of SDCC's own headers."""
    out: dict[str, int] = {}
    roots = []
    if header:
        roots.append(header)
    else:
        for base in SDCC_INCLUDE_DIRS:
            path = pathlib.Path(base)
            if path.is_dir():
                roots += [path / "stc12.h", path / "8052.h", path / "8051.h"]
                break
    for path in roots:
        if not path or not path.exists():
            continue
        text = path.read_text(errors="replace")
        for name, value in re.findall(r"SFR\s*\(\s*(\w+)\s*,\s*(0x[0-9A-Fa-f]+)", text):
            out.setdefault(name, int(value, 16))
        # 8051.h uses the plain SDCC spelling instead of the SFR() macro.
        for value, name in re.findall(
                r"__sfr\s+__at\s*\(?\s*(0x[0-9A-Fa-f]+)\s*\)?\s*(\w+)", text):
            out.setdefault(name, int(value, 16))
    return out


class Translation:
    def __init__(self, text: str, changes: dict, unresolved: list,
                 warnings: list | None = None):
        self.text = text
        self.changes = changes
        self.unresolved = unresolved
        # Things that will compile but may not behave. Worth more attention
        # than the errors, because nothing else will ever surface them.
        self.warnings = warnings or []


def translate(source: str, known: dict[str, int] | None = None,
              bits: dict[str, int] | None = None,
              headers: dict[str, str] | None = None) -> Translation:
    """Translate one Keil C51 file.

    `headers` maps a lowercased header basename to its real on-disk name. Pass
    it when the whole project is available and include resolution stops being
    guesswork: Windows is case-insensitive and uVision carries its own include
    path, so Keil sources routinely `#include "Common.h"` as `common.h`, or
    reach across the tree with `..\\..\\inc\\Main\\stdafx.h`. Neither
    resolves on a case-sensitive filesystem with POSIX separators.
    """
    known = dict(known if known is not None else sfr_addresses())
    bits = provided_bits() if bits is None else bits
    changes: dict[str, int] = {}
    unresolved: list[str] = []
    warnings: list[str] = []

    def bump(kind, n=1):
        if n:
            changes[kind] = changes.get(kind, 0) + n

    # --- includes ---------------------------------------------------------
    def fix_include(match):
        raw = match.group(2).replace("\\", "/")   # Windows separators
        # Keil projects often qualify the path ("STC/STC12C5A60S2.H"), and the
        # directory means nothing once SDCC's own header is standing in.
        name = re.split(r"[\\/]", raw)[-1].lower()
        target = HEADER_MAP.get(name)
        if target:
            bump("include")
            return f"#include <{target}>"
        # A project-local header: drop the directory and fix the case, then let
        # -I find it. Every directory in the project is on the include path, so
        # the basename alone is both sufficient and more robust than a relative
        # path that assumed a Windows working directory.
        if headers and name in headers:
            actual = headers[name]
            if actual != match.group(2):
                bump("include-path")
                return f'#include "{actual}"'
        elif raw != match.group(2):
            bump("include-path")
            return f'#include {match.group(1)}{raw}{">" if match.group(1) == "<" else chr(34)}'
        return match.group(0)

    source = re.sub(r'#\s*include\s*([<"])([^>"]+)[>"]', fix_include, source)

    # --- Keil intrinsics redeclared by vendor headers ----------------------
    # Vendor headers carry `extern void _nop_ (void);`. Our intrins.h shim
    # defines _nop_ as a function-like macro, so that declaration would
    # macro-expand into garbage before the compiler ever saw it. Drop them.
    source, n = re.subn(
        r"^[ \t]*(?:extern[ \t]+)?(?:unsigned[ \t]+|signed[ \t]+)?\w+[ \t]+"
        r"(_nop_|_testbit_|_cror_|_crol_|_iror_|_irol_|_lror_|_lrol_|_chkfloat_)"
        r"[ \t]*\([^;]*\)[ \t]*;[ \t]*$",
        r"/* Keil intrinsic \1: provided by the intrins.h shim */",
        source, flags=re.M)
    bump("intrinsic-decl", n)
    if n and not re.search(r"#\s*include\s*[<\"]intrins\.h", source, re.I):
        # Keil honours the intrinsic by name once declared -- code declares
        # `extern void _nop_(void);` (even mid-function) instead of including
        # intrins.h. The declaration is gone now, so supply the shim that
        # defines the real thing.
        source = "#include <intrins.h>\n" + source
        bump("intrinsic-include")

    # Keil allows `data bit flag` -- the qualifier is meaningless (a bit lives
    # in bit space regardless) and SDCC rejects the pair as two storage
    # classes. Drop the space qualifier, keep the bit.
    source, n = re.subn(r"(?<![\w.])(?:data|idata|xdata|pdata|bdata)[ \t]+(?=bit\b)",
                        "", source)
    bump("space-bit", n)

    # `void (code *fp)(void)`: a Keil pointer to a function in code space.
    # SDCC function pointers already point into code and its grammar rejects
    # a storage class in that position, so the qualifier just goes.
    source, n = re.subn(r"\(\s*code\s*(?=\*)", "(", source)
    bump("code-fnptr", n)

    # --- bdata --------------------------------------------------------------
    # SDCC has no bdata space. A bdata byte exists to hang sbits off, so give
    # it an absolute slot in the 8051's bit-addressable RAM (0x20-0x2F,
    # allocated from the top down) and resolve those sbits to computed bit
    # addresses (bit n of the byte at 0x20+k is bit address 8k+n). Keil's
    # linker does the same allocation, just implicitly.
    bdata_bits: dict[str, int] = {}
    bdata_next = [0x2F]

    def fix_bdata(match):
        indent, head, name = match.group(1), match.group(2), match.group(3)
        if bdata_next[0] < 0x20:
            unresolved.append(f"bdata {name}: bit-addressable RAM exhausted")
            return match.group(0)
        address = bdata_next[0]
        bdata_next[0] -= 1
        bdata_bits[name] = (address - 0x20) * 8
        bump("bdata-at")
        warnings.append(
            f"bdata {name} placed at absolute 0x{address:02X}; SDCC does not "
            "reserve absolute addresses, so check the map file for overlap")
        return f"{indent}__data __at (0x{address:02X}) {head} {name};"

    source = re.sub(
        r"^([ \t]*)bdata\s+((?:unsigned\s+|signed\s+)?char)\s+(\w+)\s*;",
        fix_bdata, source, flags=re.M)
    source = re.sub(
        r"^([ \t]*)((?:unsigned\s+|signed\s+)?char)\s+bdata\s+(\w+)\s*;",
        fix_bdata, source, flags=re.M)

    # --- sfr / sfr16 ------------------------------------------------------
    # Collected so `sbit X = MYSFR^n;` can resolve an SFR this file declares
    # itself. Kept apart from `provided`, or the dedup below would see a
    # file's own declaration as a duplicate of itself and delete it.
    provided = dict(known)
    own_names: set[str] = set(bdata_bits)
    for name, value in re.findall(
            r"(?:^|(?<=;))\s*sfr\s+(\w+)\s*=\s*(0x[0-9A-Fa-f]+|\d+)\s*;",
            source, re.M):
        known[name] = int(value, 0)

    # A declaration keyword is only anchored to the start of a line OR the end
    # of a previous statement: `sbit a0 = ACC^0;sbit a1 = ACC^1;` is real
    # corpus code, and a line-start-only anchor silently skips everything
    # after the first semicolon.
    def fix_sfr(match):
        name, value = match.group(2), int(match.group(3), 0)
        own_names.add(name)
        if provided.get(name) == value:
            bump("sfr-dup")
            return f"{match.group(1)}/* {name}: already declared by SDCC */"
        bump("sfr")
        return f"{match.group(1)}__sfr __at ({match.group(3)}) {match.group(2)};"

    source = re.sub(r"((?:^|(?<=;))[ \t]*)sfr\s+(\w+)\s*=\s*(0x[0-9A-Fa-f]+|\d+)\s*;",
                    fix_sfr, source, flags=re.M)

    def fix_sfr16(match):
        low = int(match.group(3), 0)
        own_names.add(match.group(2))
        bump("sfr16")
        # Keil names the low byte; SDCC wants both packed, high byte on top.
        return f"{match.group(1)}__sfr16 __at (0x{low + 1:02X}{low:02X}) {match.group(2)};"

    source = re.sub(r"((?:^|(?<=;))[ \t]*)sfr16\s+(\w+)\s*=\s*(0x[0-9A-Fa-f]+|\d+)\s*;",
                    fix_sfr16, source, flags=re.M)

    # --- sbit -------------------------------------------------------------
    def fix_sbit(match):
        indent, name, rhs = match.group(1), match.group(2), match.group(3).strip()
        own_names.add(name)
        bit = re.fullmatch(r"(\w+)\s*\^\s*(\d+)", rhs)
        if bit:
            base, index = bit.group(1), int(bit.group(2))
            if base in bdata_bits:
                bump("sbit")
                return (f"{indent}__sbit __at "
                        f"(0x{bdata_bits[base] + index:02X}) {name};")
            if base in known:
                address = known[base] + index
                if bits.get(name) == address:
                    bump("sbit-dup")
                    return f"{indent}/* {name}: already declared by SDCC */"
                bump("sbit")
                return f"{indent}__sbit __at (0x{address:02X}) {name};"
            if re.fullmatch(r"0[xX][0-9A-Fa-f]+|\d+", base):
                bump("sbit")
                return f"{indent}__sbit __at (0x{int(base, 0) + index:02X}) {name};"
            unresolved.append(f"sbit {name} = {rhs}  (unknown SFR {base!r})")
            return match.group(0)
        direct = re.fullmatch(r"0[xX][0-9A-Fa-f]+|\d+", rhs)
        if direct:
            bump("sbit")
            return f"{indent}__sbit __at (0x{int(rhs, 0):02X}) {name};"
        unresolved.append(f"sbit {name} = {rhs}")
        return match.group(0)

    source = re.sub(r"((?:^|(?<=;))[ \t]*)sbit\s+(\w+)\s*=\s*([^;]+);",
                    fix_sbit, source, flags=re.M)

    # --- interrupt / using ------------------------------------------------
    def fix_interrupt(match):
        bump("interrupt")
        using = f" __using({match.group(2)})" if match.group(2) else ""
        return f" __interrupt({match.group(1)}){using}"

    source = re.sub(r"\binterrupt\s+(\d+)(?:\s+using\s+(\d+))?", fix_interrupt, source)

    # --- storage classes --------------------------------------------------
    # Keil allows the storage class on either side of the type -- both
    # `unsigned char code t[]` and `code unsigned char t[]` occur in the wild --
    # so match the keyword wherever it sits, and rely on what FOLLOWS it.
    #
    # A storage keyword is always followed by whitespace and then a type,
    # an identifier or a `*`. Used as an ordinary identifier it is followed by
    # an operator instead: `g(data)`, `data = 3`, `data[i]`, `code == 3`. That
    # asymmetry is what makes this safe without parsing C properly.
    for keyword in ("xdata", "idata", "pdata", "code", "data"):
        source, n = re.subn(
            rf"(?<![\w.])(?<!->)(?<!__){keyword}\b(?=\s+[A-Za-z_*])",
            f"__{keyword}", source)
        bump(keyword, n)

    # SDCC has no __bdata; bit-addressable RAM is just data, and any sbit into
    # it needs an explicit address anyway (which the sbit rewrite above gives).
    source, n = re.subn(r"(?<![\w.])(?<!__)bdata\b(?=\s+[A-Za-z_*])", "__data", source)
    bump("bdata", n)

    # `bit` as a type: a variable, a return type, a parameter, or a cast.
    # Keil reserves the word, so it is never an identifier in Keil source.
    source, n = re.subn(r"(?<![\w.])bit\b(?=\s*[A-Za-z_*()])", "__bit", source)
    bump("bit", n)

    source, n = re.subn(r"(?<![\w.])(?<!__)reentrant\b", "__reentrant", source)
    bump("reentrant", n)

    # Keil still accepts C90 implicit int: `code NRF905_TxAddress[4] = {...};`.
    # After the storage rewrite that is `__code name[...]` with no type, which
    # SDCC rejects. Exactly one identifier between the qualifier and the
    # bracket or initialiser means the type is missing -- make Keil's implicit
    # int explicit.
    source, n = re.subn(
        r"^([ \t]*)(__(?:code|xdata|idata|pdata|data))[ \t]+"
        r"([A-Za-z_]\w*[ \t]*(?:\[|=))",
        r"\1\2 int \3", source, flags=re.M)
    bump("implicit-int", n)

    # Keil retargets printf with `char putchar(char)`; SDCC's stdio.h demands
    # `int putchar(int)` and treats the Keil signature as a conflicting
    # definition. Same for getchar. The body compiles unchanged either way.
    source, n = re.subn(
        r"\b(?:void|char|unsigned[ \t]+char|int)[ \t]+putchar[ \t]*"
        r"\([ \t]*(?:unsigned[ \t]+)?char[ \t]+(\w+)[ \t]*\)",
        r"int putchar(int \1)", source)
    bump("putchar-signature", n)
    source, n = re.subn(
        r"\b(?:char|unsigned[ \t]+char)[ \t]+getchar[ \t]*\([ \t]*(?:void)?[ \t]*\)",
        "int getchar(void)", source)
    bump("getchar-signature", n)

    # `int x _at_ 0x30;` -> `__at (0x30) int x;`
    def fix_at(match):
        bump("_at_")
        return f"__at ({match.group(3)}) {match.group(1)} {match.group(2)};"

    source = re.sub(r"^([ \t]*(?:\w+\s+)*?\w+)\s+(\w+(?:\[[^\]]*\])?)\s+_at_\s+"
                    r"(0x[0-9A-Fa-f]+|\d+)\s*;", fix_at, source, flags=re.M)

    # --- old-style parameter lists ---------------------------------------
    # Keil accepts `int f(int a, b)`, carrying the type across; SDCC wants each
    # parameter typed. 43 occurrences in the survey, and the cause of most of
    # the "too many parameters" failures.
    def fix_params(match):
        head, params, tail = match.group(1), match.group(2), match.group(3)
        parts = [p.strip() for p in params.split(",")]
        if len(parts) < 2 or " " not in parts[0]:
            return match.group(0)          # a call, or already fully typed
        last_type = parts[0].rsplit(" ", 1)[0].strip()
        out, fixed = [parts[0]], False
        for part in parts[1:]:
            if re.fullmatch(r"[A-Za-z_]\w*", part):
                out.append(f"{last_type} {part}")
                fixed = True
            else:
                out.append(part)
                if " " in part:
                    last_type = part.rsplit(" ", 1)[0].strip()
        if not fixed:
            return match.group(0)
        bump("old-style-params")
        return f"{head}({', '.join(out)}){tail}"

    # The `{` of a definition is routinely on the next line, so the tail must
    # be allowed to cross a newline -- calls still cannot match, because a
    # call is never followed by `{` and a call-as-statement's `;` sits inside
    # the enclosing expression the head fails to match.
    source = re.sub(r"(\b\w+[ \t*]+\w+[ \t]*)\(([^)(;]*)\)([ \t\r\n]*[;{])",
                    fix_params, source)

    # --- flat aggregate initialisers ---------------------------------------
    # `uchar code t[8][8] = {64 values};` is legal C (brace elision), but SDCC
    # rejects it for multi-dimensional arrays. The dimensions are right there
    # in the declarator, so re-brace mechanically -- including short lists,
    # which Keil and C both zero-fill.
    def fix_flat_init(match):
        head, dims_text, values_text, tail = match.groups()
        dims = re.findall(r"\[([^\]]*)\]", dims_text)
        try:
            sizes = [int(d.strip(), 0) if d.strip() else 0 for d in dims]
        except ValueError:
            return match.group(0)             # macro dimensions: leave alone
        # Font tables carry a comment per row; strip them before splitting.
        values_text = re.sub(r"//[^\n]*|/\*.*?\*/", "", values_text, flags=re.S)
        values = [v.strip() for v in values_text.split(",")]
        if values and values[-1] == "":
            values.pop()                      # trailing comma
        if not values:
            return match.group(0)
        inner = 1
        for size in sizes[1:]:
            if size <= 0:
                return match.group(0)
            inner *= size
        if sizes[0] == 0:
            sizes[0] = -(-len(values) // inner)          # ceiling division
        if len(values) > sizes[0] * inner:
            return match.group(0)
        # Nest from the innermost dimension outward. A short list is fine --
        # Keil and C both zero-fill what an initialiser leaves out -- so the
        # last chunk at any level may be partial. `{0}` falls out naturally
        # as fully-nested `{{0}}`, which zero-fills everything.
        level = list(values)
        for size in reversed(sizes[1:]):
            level = ["{" + ", ".join(level[i:i + size]) + "}"
                     for i in range(0, len(level), size)]
        bump("flat-init")
        return f"{head}{dims_text} = {{{', '.join(level)}}}{tail}"

    # The value class admits line and block comments (a font table has one per
    # row, quotes and semicolons included) but still refuses nested braces,
    # strings and bare semicolons, which keep the match from running away.
    source = re.sub(
        r"((?:\w[\w \t]*?[ \t]+)\w+[ \t]*)((?:\[[^\]]*\]){2,})[ \t\r\n]*=[ \t\r\n]*"
        r"\{((?:[^{}\"';/]|/(?![/*])|//[^\n]*|/\*(?:[^*]|\*(?!/))*\*/)*)\}([ \t]*;)",
        fix_flat_init, source)

    # A file-scope array declared `[]` with no initialiser is a tentative
    # definition to Keil but "unknown size" to SDCC. It is really a
    # declaration of an array defined elsewhere, which is spelled `extern`.
    source, n = re.subn(
        r"^(?![ \t]*extern\b)([ \t]*)((?:const[ \t]+)?(?:unsigned[ \t]+|signed[ \t]+)?"
        r"\w+[ \t]+(?:__\w+[ \t]+)*\w+[ \t]*\[[ \t]*\](?:[ \t]*\[[^\]]*\])*[ \t]*;)",
        r"\1extern \2", source, flags=re.M)
    bump("extern-array", n)

    # Keil documents that a local with an explicit memory space inside a
    # reentrant function is static rather than on the reentrant stack; SDCC
    # just makes the same demand explicit. Add the `static`, but only to
    # uninitialised declarations -- an initialiser would silently change from
    # per-call to once.
    reentrant_static = 0
    for match in reversed(list(re.finditer(r"__reentrant\b", source))):
        brace = source.find("{", match.end())
        if brace < 0:
            continue
        depth, i = 1, brace + 1
        while i < len(source) and depth:
            if source[i] == "{":
                depth += 1
            elif source[i] == "}":
                depth -= 1
            i += 1
        body, n = re.subn(
            r"^([ \t]*)(?!static\b)((?:\w+[ \t]+)*__(?:xdata|idata|pdata|data|code)"
            r"[ \t]+(?:\w+[ \t]+)*\w+(?:[ \t]*\[[^\]]*\])*[ \t]*(?:,[^;=()]*)?;)",
            r"\1static \2", source[brace + 1:i - 1], flags=re.M)
        if n:
            reentrant_static += n
            source = source[:brace + 1] + body + source[i - 1:]
    bump("reentrant-static", reentrant_static)

    # --- register visibility ------------------------------------------------
    # Two ways a translation unit loses its registers: the dedup above dropped
    # a declaration SDCC already provides and nothing else includes SDCC's
    # header, or the original relied on a register header living in Keil's
    # install directory, which no repo ships. Either way the cure is the same:
    # make sure the file sees SDCC's declarations. keil-stc12.h is guarded, so
    # a second include through a mapped vendor header costs nothing.
    if not re.search(r'#\s*include\s*[<"]keil-(?:stc12|reg51|reg52)\.h', source):
        used = set(re.findall(r"\b[A-Za-z_]\w+\b", source))
        # Names of 3+ characters only: EA or OV alone could be anybody's
        # identifier, and a file whose only register use is that short failed
        # before this rule existed too.
        wanted = {name for name in (set(known) | set(bits)) - own_names
                  if len(name) >= 3}
        if changes.get("sfr-dup") or changes.get("sbit-dup") or (used & wanted):
            source = "#include <keil-stc12.h>\n" + source
            bump("register-include")

    # --- ISR visibility ---------------------------------------------------
    # SDCC only emits an interrupt vector if the ISR (or a prototype) is visible
    # in the translation unit holding main(). Keil links vectors separately, so
    # Keil projects routinely put ISRs in their own file -- 24 of the 37
    # ISR-defining files in the survey do. That compiles cleanly under SDCC and
    # the interrupt simply never fires, which no error will ever tell you.
    if re.search(r"__interrupt\s*\(", source) and not re.search(r"\bmain\s*\(", source):
        warnings.append(
            "this file defines an interrupt handler but has no main(); SDCC only "
            "emits the vector if the handler or a prototype is visible in the "
            "file containing main(), so copy the prototype there")

    return Translation(source, changes, unresolved, warnings)


class ProjectTranslation:
    def __init__(self, files: dict[str, str], changes: dict, unresolved: list,
                 warnings: list, isr_injected: list,
                 sources: list | None = None, model_flags: list | None = None):
        self.files = files
        self.changes = changes
        self.unresolved = unresolved
        self.warnings = warnings
        # "handler -> main-file" notes, so callers can show what happened.
        self.isr_injected = isr_injected
        # From the uVision project file, when one is present: which .c files
        # the project actually builds (trees routinely carry dead ones), and
        # the SDCC flag matching Keil's memory-model setting.
        self.sources = sources
        self.model_flags = model_flags or []


_ISR_DEF = re.compile(
    r"\bvoid\s+(\w+)\s*\(\s*(?:void)?\s*\)\s*"
    r"(__interrupt\s*\(\s*(\d+)\s*\)(?:\s*__using\s*\(\s*\d+\s*\))?)\s*\{")
_MAIN_DEF = re.compile(r"\b(?:void|int)\s+main\s*\(")
_UVPROJ_FILE = re.compile(r"<FilePath>([^<]+)</FilePath>")
_UVPROJ_MODEL = re.compile(r"<MemoryModel>(\d)</MemoryModel>")


def translate_project(files: dict[str, str],
                      known: dict[str, int] | None = None,
                      bits: dict[str, int] | None = None) -> ProjectTranslation:
    """Translate a whole Keil project: {relative path: text} in, same out.

    Beyond running translate() over every file with a shared header map, this
    fixes the one thing single-file translation cannot: SDCC only emits an
    interrupt vector when the handler or a prototype is visible in the
    translation unit containing main(). Keil links vectors separately, so Keil
    projects routinely keep ISRs in their own file -- which compiles cleanly
    under SDCC and then the interrupt never fires. Here every ISR defined
    outside the main() file gets a prototype injected into it.
    """
    known = dict(known if known is not None else sfr_addresses())
    bits = provided_bits() if bits is None else bits
    headers = {}
    for path in files:
        name = re.split(r"[\\/]", path)[-1]
        if name.lower().endswith(".h"):
            headers[name.lower()] = name

    out: dict[str, str] = {}
    changes: dict[str, int] = {}
    unresolved: list[str] = []
    warnings: list[str] = []
    for path, text in files.items():
        name = re.split(r"[\\/]", path)[-1].lower()
        if not (name.endswith(".c") or name.endswith(".h")):
            out[path] = text
            continue
        result = translate(text, known, bits=bits, headers=headers)
        out[path] = result.text
        for kind, count in result.changes.items():
            changes[kind] = changes.get(kind, 0) + count
        unresolved += [f"{path}: {item}" for item in result.unresolved]
        warnings += [f"{path}: {item}" for item in result.warnings
                     # The single-file advice to copy prototypes by hand is
                     # superseded by the injection below.
                     if "prototype" not in item]

    # A uVision project file is the ground truth for what actually gets
    # built: source trees routinely carry dead or alternate .c files, and the
    # memory model lives here too (0 small, 1 compact, 2 large).
    sources: list[str] | None = None
    model_flags: list[str] = []
    uvproj = next((p for p in files
                   if p.lower().endswith((".uvproj", ".uvprojx"))), None)
    if uvproj:
        listed = {re.split(r"[\\/]", raw)[-1].lower()
                  for raw in _UVPROJ_FILE.findall(files[uvproj])}
        if listed:
            by_base = {re.split(r"[\\/]", p)[-1].lower(): p for p in out}
            sources = sorted(by_base[b] for b in listed
                             if b in by_base and b.endswith(".c"))
            assembly = sorted(b for b in listed if b.endswith((".a51", ".asm"))
                              and not b.startswith("startup"))
            if assembly:
                warnings.append(
                    "the uVision project links assembly modules this "
                    f"translation does not cover: {', '.join(assembly)}")
        model = _UVPROJ_MODEL.search(files[uvproj])
        if model and model.group(1) == "2":
            model_flags = ["--model-large"]
        elif model and model.group(1) == "1":
            model_flags = ["--model-medium"]

    # Keil's BL51 linker matches PUBLIC/EXTRN symbols case-insensitively;
    # SDCC's linker does not. So `extern char lowerReading_Flag;` against a
    # definition spelled LowerReading_Flag links under Keil and comes back
    # "Undefined Global" under SDCC. Rename such externs (and their uses)
    # onto the definition's spelling.
    defined: dict[str, str] = {}          # lowercased -> defining spelling
    exact: set[str] = set()
    for path in out:
        if not path.lower().endswith(".c"):
            continue
        for name in re.findall(
                r"^(?!extern\b|static\b|typedef\b)(?:[\w*]+[ \t]+)+(\w+)"
                r"[ \t]*(?:\[[^\]]*\])?[ \t]*[=;]", out[path], re.M):
            defined.setdefault(name.lower(), name)
            exact.add(name)
        for name in re.findall(
                r"^(?:[\w*]+[ \t]+)+(\w+)[ \t]*\([^;{)]*\)[ \t\r\n]*\{",
                out[path], re.M):
            defined.setdefault(name.lower(), name)
            exact.add(name)
    externs: set[str] = set()
    for path in out:
        if path.lower().endswith((".c", ".h")):
            externs.update(re.findall(
                r"\bextern\s+(?:[\w*]+\s+)*(\w+)\s*(?:\[[^\]]*\])?\s*;",
                out[path]))
    for name in sorted(externs):
        target = defined.get(name.lower())
        if not target or target == name or name in exact:
            continue
        pattern = re.compile(rf"\b{re.escape(name)}\b")
        for path in list(out):
            if path.lower().endswith((".c", ".h")):
                out[path] = pattern.sub(target, out[path])
        changes["case-unify"] = changes.get("case-unify", 0) + 1
        warnings.append(
            f"extern {name} matches the definition {target} only ignoring "
            "case; renamed to it (Keil's BL51 links case-insensitively, "
            "SDCC's linker does not)")

    # Keil lets a header say `extern ... xdata MSR7;` while one .c pins the
    # address with `_at_`. SDCC insists the extern carry the same __at, so
    # collect every absolute definition and stamp its address onto the
    # matching extern declarations project-wide.
    absolutes: dict[str, str] = {}
    for path in out:
        if path.lower().endswith(".c"):
            for address, name in re.findall(
                    r"__at\s*\((0x[0-9A-Fa-f]+|\d+)\)\s*(?:[\w*]+\s+)*(\w+)"
                    r"\s*(?:\[[^\]]*\])?\s*;", out[path]):
                absolutes.setdefault(name, address)
    if absolutes:
        pattern = re.compile(
            r"\bextern\s+((?:[\w*]+\s+)*?)(" + "|".join(
                re.escape(n) for n in absolutes) + r")(\s*(?:\[[^\]]*\])?\s*;)")

        def fix_extern(match):
            head, name, tail = match.groups()
            if "__at" in head:
                return match.group(0)
            changes["extern-at"] = changes.get("extern-at", 0) + 1
            return f"extern __at ({absolutes[name]}) {head}{name}{tail}"

        for path in list(out):
            if path.lower().endswith((".c", ".h")):
                out[path] = pattern.sub(fix_extern, out[path])

    in_build = set(sources) if sources else {
        p for p in out if p.lower().endswith(".c")}
    mains = [p for p in in_build if _MAIN_DEF.search(out[p])]
    handlers: list[tuple[str, str, int, str]] = []   # (file, name, vector, decl)
    for path in out:
        if not (path in in_build or path.lower().endswith(".h")):
            continue
        for name, decl, vector in _ISR_DEF.findall(out[path]):
            handlers.append((path, name, int(vector), decl))

    isr_injected: list[str] = []
    if len(mains) == 1 and handlers:
        main_path = mains[0]
        seen_vectors: dict[int, str] = {}
        prototypes = []
        for path, name, vector, decl in handlers:
            if vector in seen_vectors and seen_vectors[vector] != name:
                warnings.append(
                    f"vector {vector} is defined by both {seen_vectors[vector]} "
                    f"and {name}; only the first is injected")
                continue
            seen_vectors.setdefault(vector, name)
            if path == main_path:
                continue                       # already visible where it counts
            if re.search(rf"\b{name}\s*\([^)]*\)\s*__interrupt", out[main_path]):
                continue                       # a prototype is already there
            prototypes.append(f"void {name}(void) {decl};")
            isr_injected.append(f"{name} (vector {vector}) -> {main_path}")
        if prototypes:
            out[main_path] = (
                "/* ISR prototypes injected by translate_project(): SDCC only\n"
                "   emits an interrupt vector when the handler is visible in\n"
                "   the file containing main(); Keil links vectors separately. */\n"
                + "\n".join(prototypes) + "\n\n" + out[main_path])
    elif len(mains) > 1 and handlers:
        warnings.append(
            f"{len(mains)} files define main() -- skipping ISR prototype "
            "injection; split the project per firmware image and retry")
    elif not mains and handlers:
        warnings.append(
            "no file defines main(): if these files are linked into a larger "
            "project, make sure each ISR has a prototype next to main()")

    return ProjectTranslation(out, changes, unresolved, warnings, isr_injected,
                              sources, model_flags)


def shim_args() -> list[str]:
    """`-I` flags giving SDCC our replacements for Keil-only headers."""
    return ["-I", str(SHIM_DIR)] if SHIM_DIR.is_dir() else []
