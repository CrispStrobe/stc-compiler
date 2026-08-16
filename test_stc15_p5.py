"""P5 on the STC15 — the reference implementation's side of the contract.

sb3-creator's generateC emits the identical STC15 supplement; this file is
the oracle, so the two must not drift (both cite STC15-PERIPHERAL-MODEL.md
section 3). The RBS15667 console's buzzer is P5.5 — the pin that made this
real.
"""
import pytest

import stc_pseudocode as sp
from stc_pseudocode import PseudocodeError


SRC = """DEVICE STC15F2K60S2
CLOCK 11059200
PIN buzzer = P5.5 OUTPUT ACTIVE LOW

WHEN flag clicked:
  FOREVER:
    turn on buzzer
    wait 0.1 seconds
    turn off buzzer
    wait 0.1 seconds
"""


def test_stc15_p5_supplement_and_drive():
    c = sp.emit_c(sp.parse(SRC))
    # The supplement fills exactly what stc12.h lacks — and never
    # redeclares what it already has (P5/P5M0/P5M1 ship in SDCC 4.5.0).
    assert "__sbit __at (0xCD) P5_5;" in c
    assert "__sbit __at (0xCC) P5_4;" in c
    assert "__sfr  __at (0xD6) T2H;" in c
    assert "__sfr  __at (0xBA) P_SW2;" in c
    assert "#define INT_CLKO WAKE_CLKO" in c
    assert "__at (0xC8) P5;" not in c, "P5 latch comes from stc12.h, never redeclared"
    # The pin is set up push-pull and actually driven.
    assert "P5M1 &= ~0x20;" in c
    assert "P5_5 = " in c


def test_p5_missing_on_stc12():
    with pytest.raises(PseudocodeError) as e:
        sp.emit_c(sp.parse(SRC.replace("STC15F2K60S2", "STC12C5A60S2")))
    assert "does not exist" in str(e.value)
    assert "STC15" in str(e.value)


def test_p5_unbonded_bit_refused():
    with pytest.raises(PseudocodeError) as e:
        sp.emit_c(sp.parse(SRC.replace("P5.5", "P5.1")))
    assert "not bonded" in str(e.value)


def test_stc12_gets_no_supplement():
    src = SRC.replace("STC15F2K60S2", "STC12C5A60S2").replace("P5.5", "P1.0")
    c = sp.emit_c(sp.parse(src))
    assert "STC15 supplement" not in c
