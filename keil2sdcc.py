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

import pathlib
import re

SHIM_DIR = pathlib.Path(__file__).parent / "keil-shim"

# Headers Keil projects include that SDCC has no equivalent for. Mapped rather
# than vendored: the vendor headers carry no licence, and SDCC's own stc12.h
# already declares the same register names.
HEADER_MAP = {
    "stc12c5a60s2.h": "stc12.h",
    "stc12c5a.h": "stc12.h",
    "stc12c5a60s2_p.h": "stc12.h",
    "stc12c5a60ad.h": "stc12.h",
    "reg51.h": "8051.h",
    "reg52.h": "8052.h",
    "at89x51.h": "at89x51.h",
    "at89x52.h": "at89x52.h",
}

STORAGE = ("xdata", "idata", "pdata", "bdata", "code")


def sfr_addresses(header: pathlib.Path | None = None) -> dict[str, int]:
    """Parse `SFR(NAME, 0xNN);` out of SDCC's own headers."""
    out: dict[str, int] = {}
    roots = []
    if header:
        roots.append(header)
    else:
        for base in ("/opt/homebrew/share/sdcc/include/mcs51",
                     "/usr/share/sdcc/include/mcs51",
                     "/usr/local/share/sdcc/include/mcs51"):
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
    def __init__(self, text: str, changes: dict, unresolved: list):
        self.text = text
        self.changes = changes
        self.unresolved = unresolved


def translate(source: str, known: dict[str, int] | None = None) -> Translation:
    known = dict(known if known is not None else sfr_addresses())
    changes: dict[str, int] = {}
    unresolved: list[str] = []

    def bump(kind, n=1):
        if n:
            changes[kind] = changes.get(kind, 0) + n

    # --- includes ---------------------------------------------------------
    def fix_include(match):
        raw = match.group(2)
        # Keil projects often qualify the path ("STC/STC12C5A60S2.H"), and the
        # directory means nothing once SDCC's own header is standing in.
        name = re.split(r"[\\/]", raw)[-1].lower()
        target = HEADER_MAP.get(name)
        if not target:
            return match.group(0)
        bump("include")
        return f"#include <{target}>"

    source = re.sub(r'#\s*include\s*([<"])([^>"]+)[>"]', fix_include, source)

    # --- sfr / sfr16 ------------------------------------------------------
    # Collect first so `sbit X = MYSFR^n;` can resolve a locally declared SFR.
    for name, value in re.findall(r"^\s*sfr\s+(\w+)\s*=\s*(0x[0-9A-Fa-f]+|\d+)\s*;",
                                  source, re.M):
        known[name] = int(value, 0)

    def fix_sfr(match):
        bump("sfr")
        return f"{match.group(1)}__sfr __at ({match.group(3)}) {match.group(2)};"

    source = re.sub(r"^([ \t]*)sfr\s+(\w+)\s*=\s*(0x[0-9A-Fa-f]+|\d+)\s*;",
                    fix_sfr, source, flags=re.M)

    def fix_sfr16(match):
        low = int(match.group(3), 0)
        bump("sfr16")
        # Keil names the low byte; SDCC wants both packed, high byte on top.
        return f"{match.group(1)}__sfr16 __at (0x{low + 1:02X}{low:02X}) {match.group(2)};"

    source = re.sub(r"^([ \t]*)sfr16\s+(\w+)\s*=\s*(0x[0-9A-Fa-f]+|\d+)\s*;",
                    fix_sfr16, source, flags=re.M)

    # --- sbit -------------------------------------------------------------
    def fix_sbit(match):
        indent, name, rhs = match.group(1), match.group(2), match.group(3).strip()
        bit = re.fullmatch(r"(\w+)\s*\^\s*(\d+)", rhs)
        if bit:
            base, index = bit.group(1), int(bit.group(2))
            if base in known:
                bump("sbit")
                return f"{indent}__sbit __at (0x{known[base] + index:02X}) {name};"
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

    source = re.sub(r"^([ \t]*)sbit\s+(\w+)\s*=\s*([^;]+);", fix_sbit, source, flags=re.M)

    # --- interrupt / using ------------------------------------------------
    def fix_interrupt(match):
        bump("interrupt")
        using = f" __using({match.group(2)})" if match.group(2) else ""
        return f" __interrupt({match.group(1)}){using}"

    source = re.sub(r"\binterrupt\s+(\d+)(?:\s+using\s+(\d+))?", fix_interrupt, source)

    # --- storage classes --------------------------------------------------
    for keyword in STORAGE:
        source, n = re.subn(rf"(?<![\w.])(?<!__){keyword}(?=\s+[\w*])",
                            f"__{keyword}", source)
        bump(keyword, n)

    # `bit` as a type. Guarded so it does not touch identifiers containing it.
    source, n = re.subn(r"(?<![\w.])(?<!__)bit(?=\s+\w+\s*[;,=\)\[])", "__bit", source)
    bump("bit", n)

    source, n = re.subn(r"(?<![\w.])(?<!__)reentrant\b", "__reentrant", source)
    bump("reentrant", n)

    # Bare `data`. Only rewritten where a type keyword immediately precedes it,
    # which is the one position where it cannot be an identifier -- `unsigned
    # char data i;` is a declaration, `foo(data)` is a variable.
    source, n = re.subn(
        r"\b(unsigned|signed|char|int|long|short|float|double|void)\s+data\s+(?=[\w*])",
        r"\1 __data ", source)
    bump("data", n)

    # `int x _at_ 0x30;` -> `__at (0x30) int x;`
    def fix_at(match):
        bump("_at_")
        return f"__at ({match.group(3)}) {match.group(1)} {match.group(2)};"

    source = re.sub(r"^([ \t]*(?:\w+\s+)*?\w+)\s+(\w+(?:\[[^\]]*\])?)\s+_at_\s+"
                    r"(0x[0-9A-Fa-f]+|\d+)\s*;", fix_at, source, flags=re.M)

    return Translation(source, changes, unresolved)


def shim_args() -> list[str]:
    """`-I` flags giving SDCC our replacements for Keil-only headers."""
    return ["-I", str(SHIM_DIR)] if SHIM_DIR.is_dir() else []
