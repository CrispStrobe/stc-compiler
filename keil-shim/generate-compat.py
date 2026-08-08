#!/usr/bin/env python3
"""
generate-compat.py — regenerate the keil-compat headers.

Three files, because the register families genuinely differ:

  keil-compat.h        chip-agnostic: P0-P3 bit aliases in the vendor
                       spelling, double->float math mapping, abs via stdlib
  keil-compat-stc12.h  STC12 extras: P4 at 0xC0, ADC/S2CON constants,
                       ISP_* spelled onto IAP_*, IPH2 alias
  keil-compat-8052.h   STC89 extras for reg51/reg52/regx51/regx52 users:
                       AUXR 0x8E, WDT_CONTR 0xE1 (NOT the STC12's 0xC1),
                       ISP_* as real registers at 0xE2-0xE7, P4 at 0xE8
                       (NOT the STC12's 0xC0) with its bit aliases

Every candidate is checked against what the family's own SDCC header already
declares and dropped if it is a duplicate, because SDCC treats a second
declaration of an SFR as an error, not a redefinition. Generating rather than
hand-maintaining means a new SDCC release that adds a register cannot
silently break us.

Values are from the STC12C5A60S2 datasheet (2011-07-15) and the STC89C51RC/RD+
datasheet; the port bit aliases are bit-address arithmetic (bit n of a
bit-addressable SFR at base B is at B+n). Nothing is copied from a vendor
header.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
import keil2sdcc

# ---------------------------------------------------------------- STC12 facts
STC12_SFRS = [
    ("IAP_DATA", 0xC2), ("IAP_ADDRH", 0xC3), ("IAP_ADDRL", 0xC4),
    ("IAP_CMD", 0xC5), ("IAP_TRIG", 0xC6), ("IAP_CONTR", 0xC7),
    ("WDT_CONTR", 0xC1), ("SPSTAT", 0xCE), ("SPCTL", 0xCD), ("SPDAT", 0xCF),
    ("CCAP0L", 0xEA), ("CCAP1L", 0xEB), ("CL", 0xE9), ("CH", 0xF9),
    ("CCAP0H", 0xFA), ("CCAP1H", 0xFB), ("P1ASF", 0x9D), ("P4SW", 0xBB),
    ("AUXR1", 0xA2), ("CCON", 0xD8), ("CMOD", 0xD9),
    ("CCAPM0", 0xDA), ("CCAPM1", 0xDB), ("PCA_PWM0", 0xF2), ("PCA_PWM1", 0xF3),
    ("ADC_CONTR", 0xBC), ("ADC_RES", 0xBD), ("ADC_RESL", 0xBE),
]
STC12_CONSTANTS = [
    ("ADC_POWER", "0x80", "ADC_CONTR bit 7"),
    ("ADC_SPEEDLL", "0x00", "540 clocks per conversion"),
    ("ADC_SPEEDL", "0x20", "360"),
    ("ADC_SPEEDH", "0x40", "180"),
    ("ADC_SPEEDHH", "0x60", "90"),
    ("ADC_FLAG", "0x10", "conversion complete"),
    ("ADC_START", "0x08", "begin a conversion"),
    # S2CON (0x9A) is not bit-addressable, so vendor headers expose its bits
    # as masks. Layout mirrors SCON (datasheet 7.3, UART2).
    ("S2SM0", "0x80", "S2CON bit 7"),
    ("S2SM1", "0x40", "S2CON bit 6"),
    ("S2SM2", "0x20", "S2CON bit 5"),
    ("S2REN", "0x10", "S2CON bit 4, receive enable"),
    ("S2TB8", "0x08", "S2CON bit 3"),
    ("S2RB8", "0x04", "S2CON bit 2"),
    ("S2TI", "0x02", "S2CON bit 1, transmit flag"),
    ("S2RI", "0x01", "S2CON bit 0, receive flag"),
    ("ES2", "0x01", "IE2 bit 0, UART2 interrupt enable"),
]

# -------------------------------------------------------------- STC15 facts
# Source: STC15F2K60S2 datasheet (stcmicro.com). Most of the STC15's map
# matches the STC12's -- which is why the shim can sit on stc12.h -- and the
# entries here are exactly the divergent/additional ones. Note Timer 2: T2H
# at 0xD6, T2L at 0xD7, controlled from AUXR bits (no T2CON); this is the
# register pair that made blanket stc15->stc12 mapping dangerous before the
# family got its own header.
STC15_SFRS = [
    ("T2H", 0xD6), ("T2L", 0xD7),
    ("INT_CLKO", 0x8F),
    ("WKTCL", 0xAA), ("WKTCH", 0xAB),
    ("P5", 0xC8), ("P5M1", 0xC9), ("P5M0", 0xCA),
    ("P6", 0xE8), ("P6M1", 0xCB), ("P6M0", 0xCC),
    ("P7", 0xF8), ("P7M1", 0xE1), ("P7M0", 0xE2),
]

# -------------------------------------------------------------- STC89 facts
# Source: STC89C51RC/RD+ datasheet (stcmicro.com/datasheet/STC89C51RC-en.pdf).
# AUXR/AUXR1/IPH sit at the same addresses on the STC12, so SDCC's own
# stc12.h independently confirms them.
# Only names whose addresses AGREE with the STC12's (or exist nowhere else):
# a real project mixes reg52-family and STC12-family headers in one
# translation unit, and a compat declaration of, say, WDT_CONTR at the
# STC89's 0xE1 would collide with stc12.h's 0xC1 whenever both appear.
# Chip-ambiguous registers (WDT_CONTR, ISP_*, P4 at 0xE8) are deliberately
# NOT declared here -- STC89 code that needs them declares them itself, and
# the translator now preserves such declarations (the dedup checks the full
# per-family address sets).
STC89_SFRS = [
    ("AUXR", 0x8E), ("AUXR1", 0xA2), ("IPH", 0xB7), ("XICON", 0xC0),
]

PORTS_SHARED = (("P0", 0x80), ("P1", 0x90), ("P2", 0xA0), ("P3", 0xB0))
MATH_1 = ("fabs sqrt sin cos tan asin acos atan sinh cosh tanh "
          "exp log log10 floor ceil").split()
MATH_2 = ["atan2", "pow", "fmod", "ldexp"]

# Read SDCC's headers only: scanning the shim would include the very files
# being regenerated, and everything would look like a duplicate of itself.
have_bit = keil2sdcc.provided_bits(include_shim=False)
have_stc12 = keil2sdcc.sfr_addresses()          # stc12.h + 8052.h + 8051.h
base = next(pathlib.Path(b) for b in keil2sdcc.SDCC_INCLUDE_DIRS
            if pathlib.Path(b).is_dir())
have_8052 = {}
for name in ("8051.h", "8052.h"):
    have_8052.update(keil2sdcc.sfr_addresses(base / name))

HEAD = ["/*", " * {name} — GENERATED by generate-compat.py, do not hand-edit.",
        " *", " * Declarations Keil's vendor headers provide that SDCC's do",
        " * not. Anything the family's SDCC header already declares is",
        " * omitted: a second declaration of an SFR is an error to SDCC.",
        " *", " * SPDX-License-Identifier: MIT", " */"]


def write(name, guard, body):
    lines = [line.format(name=name) for line in HEAD]
    lines += [f"#ifndef {guard}", f"#define {guard}", ""]
    lines += body
    lines += ["", f"#endif /* {guard} */"]
    path = pathlib.Path(__file__).parent / name
    path.write_text("\n".join(lines) + "\n")
    print(f"wrote {path.name}: {len(body)} lines")


def port_aliases(port, bit_base, styles=("plain",)):
    out = []
    for n in range(8):
        for style in styles:
            alias = f"{port}{n}" if style == "plain" else f"{port}_{n}"
            if alias in have_bit:
                continue
            out.append(f"__sbit __at (0x{bit_base + n:02X}) {alias};")
    return out


# ------------------------------------------------------------------ shared
body = ["/* Port bit aliases in the vendor spelling (P13) beside SDCC's"
        " (P1_3). */"]
for port, address in PORTS_SHARED:
    body += port_aliases(port, address)
body += ["", "/* SDCC's math.h carries only the float-suffixed C99 names, and",
         "   on mcs51 double is float anyway. Map the double spellings Keil",
         "   code uses. Function-like on purpose: a variable named exp stays",
         "   alone. */"]
for fn in MATH_1:
    body += [f"#ifndef {fn}", f"#define {fn}(x) {fn}f(x)", "#endif"]
for fn in MATH_2:
    body += [f"#ifndef {fn}", f"#define {fn}(a, b) {fn}f(a, b)", "#endif"]
write("keil-compat.h", "_KEIL_COMPAT_H_", body)

# ------------------------------------------------------------------ stc12
body = ["/* Registers SDCC's stc12.h does not carry (usually none). */"]
body += [f"__sfr __at (0x{a:02X}) {n};" for n, a in STC12_SFRS
         if n not in have_stc12]
body += ["", "/* P4 lives at 0xC0 on the STC12 (the STC89 puts it at 0xE8)."
             " */"]
body += port_aliases("P4", 0xC0, ("plain",))
body += ["", "/* Vendor-header constants (datasheet 9.2 / 7.3). */"]
body += [f"#define {n:<12} {v}   /* {note} */" for n, v, note in STC12_CONSTANTS]
body += ["", "/* Same register, different vendor spelling. */"]
if "IPH2" not in have_stc12:
    body += ["#define IPH2         IP2H"]
body += ["", "/* Older vendor headers spell the IAP registers ISP_*. */"]
body += [f"#define ISP_{n[4:]:<8} {n}" for n, _ in STC12_SFRS
         if n.startswith("IAP_")]
write("keil-compat-stc12.h", "_KEIL_COMPAT_STC12_H_", body)

# ------------------------------------------------------------------ stc15
body = ["/* STC15F2K60S2 additions on top of stc12.h (see the generator for",
        "   why the base is stc12.h: the two maps agree except for these).",
        "   P5/P6/P7 are bit-addressable where the address allows. */"]
body += [f"__sfr __at (0x{a:02X}) {n};" for n, a in STC15_SFRS
         if n not in have_stc12]
body += ["", "/* Port bit aliases for the extra ports. */"]
for port, address in (("P5", 0xC8), ("P6", 0xE8), ("P7", 0xF8)):
    body += port_aliases(port, address, ("plain", "underscore"))
write("keil-compat-stc15.h", "_KEIL_COMPAT_STC15_H_", body)

# ------------------------------------------------------------------ 8052/STC89
body = ["/* STC89C5xRC/RD+ extras a reg51/reg52 project expects, restricted",
        "   to addresses that cannot conflict with the STC12 family (see the",
        "   note on STC89_SFRS in the generator). */"]
body += [f"__sfr __at (0x{a:02X}) {n};" for n, a in STC89_SFRS
         if n not in have_8052]
write("keil-compat-8052.h", "_KEIL_COMPAT_8052_H_", body)
