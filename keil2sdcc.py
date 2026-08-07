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

    # --- sfr / sfr16 ------------------------------------------------------
    # Collected so `sbit X = MYSFR^n;` can resolve an SFR this file declares
    # itself. Kept apart from `provided`, or the dedup below would see a
    # file's own declaration as a duplicate of itself and delete it.
    provided = dict(known)
    for name, value in re.findall(r"^\s*sfr\s+(\w+)\s*=\s*(0x[0-9A-Fa-f]+|\d+)\s*;",
                                  source, re.M):
        known[name] = int(value, 0)

    def fix_sfr(match):
        name, value = match.group(2), int(match.group(3), 0)
        if provided.get(name) == value:
            bump("sfr-dup")
            return f"{match.group(1)}/* {name}: already declared by SDCC */"
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

    source = re.sub(r"^([ \t]*)sbit\s+(\w+)\s*=\s*([^;]+);", fix_sbit, source, flags=re.M)

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

    source = re.sub(r"(\b\w+[ \t*]+\w+[ \t]*)\(([^)(;]*)\)([ \t]*[;{])",
                    fix_params, source)

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


def shim_args() -> list[str]:
    """`-I` flags giving SDCC our replacements for Keil-only headers."""
    return ["-I", str(SHIM_DIR)] if SHIM_DIR.is_dir() else []
