"""test_device_matrix — every advertised device is built, not merely emitted.

`test_device_parity.py` proves each DEVICE parses and produces some C. That
is a weaker claim than it looks: on 2026-09-02 a sweep of all nineteen
devices through the real compile path found three that emit plausible source
and cannot be built —

  attiny85/attiny88   the AVR generator emits ATmega timer register names
                      (TIMSK0, WGM01), which do not exist on those parts
  eater6502           registered as a port/bit AVR target, so /compile
                      dispatched it to avr-gcc and raised KeyError -> HTTP 500,
                      and the C it generated was AVR C for a 6502

All three passed parity, because parity asserts len(c) > 100.

So this file drives `app.build()` — the same function the endpoint calls —
once per device, and holds each to a DECLARED outcome:

  BUILDS          a real image comes back, with bytes and a valid image
  TRANSPILE_ONLY  refused on purpose, naming the toolchain it would need,
                  and handing back the generated source anyway
  NO_GENERATOR    the DEVICE name is real but nothing emits for it yet, and
                  it says so at the DEVICE line rather than borrowing the
                  nearest generator
  KNOWN_GAP       a recorded defect, with the reason written down

A KNOWN_GAP is asserted to STILL BE BROKEN. That is the point: when someone
fixes one, this test fails and forces the table — and the README's "Known
gaps" section — to be updated in the same commit. A gap list nobody is made
to maintain silently becomes a lie.

Every device in stc_pseudocode.TARGETS must appear in EXPECTED, so a new
device cannot be added without saying which of the three it is.
"""
from __future__ import annotations

import pytest

import app
import stc_pseudocode as sp

BUILDS = "builds"
TRANSPILE_ONLY = "transpile-only"
NO_GENERATOR = "no-generator"
KNOWN_GAP = "known-gap"


class Case:
    """One device: how to write a blink for it, and what should happen."""

    def __init__(self, pin, clock, outcome, toolchain=None, gap=None,
                 source=None, says=None):
        self.pin, self.clock, self.outcome = pin, clock, outcome
        self.toolchain, self.gap, self.source = toolchain, gap, source
        self.says = says or ()          # substrings the refusal must contain

    def program(self, device: str) -> str:
        if self.source is not None:
            return self.source
        clock = f"  CLOCK {self.clock}\n" if self.clock else ""
        return (f"DEVICE {device.upper()}:\n{clock}"
                f"  PIN led = {self.pin} OUTPUT\n\n"
                f"  WHEN started:\n"
                f"    FOREVER:\n"
                f"      toggle led\n"
                f"      wait 500 ms\n")


# The toolchain string is the one the refusal must name -- it is what tells a
# caller which program to install, so a wrong one is worse than none.
EXPECTED = {
    # ---- 8051: SDCC, hosted here ----
    "stc12c5a60s2": Case("P1.0", 11059200, BUILDS),
    "stc12c5a16s2": Case("P1.0", 11059200, BUILDS),
    "stc15f2k60s2": Case("P1.0", 11059200, BUILDS),
    "stc15w408as":  Case("P1.0", 11059200, BUILDS),
    "stc89c52":     Case("P1.0", 11059200, BUILDS),
    "stc89c52rc":   Case("P1.0", 11059200, BUILDS),

    # ---- bare AVR: avr-gcc, hosted here ----
    "atmega328p": Case("D13", 16000000, BUILDS),
    "atmega168p": Case("D13", 16000000, BUILDS),

    # ---- bare AVR, the tiny parts ----
    # Both were KNOWN_GAP until 2026-09-02: the generator emitted ATmega
    # Timer-0 spellings. The ATtiny85 names the mask register TIMSK, and the
    # ATtiny48/88 has no TCCR0B or WGM01 at all -- one TCCR0A carries the
    # prescaler and CTC0 together. Timer 0 is now a target fact.
    "attiny85": Case("PB3", 8000000, BUILDS),
    "attiny88": Case("PB0", 8000000, BUILDS),

    # ---- Arduino core: transpiles here, built by the IDE ----
    "arduino-uno":  Case("D13", 16000000, TRANSPILE_ONLY, toolchain="arduino-cli"),
    "arduino-nano": Case("D13", 16000000, TRANSPILE_ONLY, toolchain="arduino-cli"),
    "arduino-mega": Case("D13", 16000000, TRANSPILE_ONLY, toolchain="arduino-cli"),

    # ---- MicroPython: interpreted on the device, nothing to compile ----
    "microbit":  Case("P0", None, TRANSPILE_ONLY, toolchain="uflash"),
    "micro-bit": Case("P0", None, TRANSPILE_ONLY, toolchain="uflash"),
    "pico":      Case("GP25", None, TRANSPILE_ONLY, toolchain="uf2"),
    "rp2040":    Case("GP25", None, TRANSPILE_ONLY, toolchain="uf2"),

    # ---- 6502 ----
    # Until 2026-09-02 this was registered as a PortBitAvrTarget: the
    # pseudocode lane emitted AVR C for a 65C02, and /compile raised KeyError
    # looking the part up in AVR_TARGETS -> 500. Emitting nothing beats
    # emitting confident code for the wrong architecture, so it now refuses at
    # the DEVICE line and points at the lanes that do work. The generator
    # itself is unwritten -- see Eater6502Target for what it needs.
    "eater6502": Case("PA0", 1000000, NO_GENERATOR,
                      says=("no pseudocode generator", "eater6502")),

    # ---- a game engine, not a chip ----
    "arcade": Case(None, None, TRANSPILE_ONLY, toolchain="pxt", source=(
        "DEVICE ARCADE\n\n"
        "WHEN started:\n"
        "  arcade create hero kind Player\n"
        "  arcade place hero x 80 y 60\n"
        "  FOREVER:\n"
        "    arcade score add 1\n"
        "    wait 1 seconds\n")),
}


def compile_device(device: str, case: Case):
    """Drive the real endpoint function. Returns (result, exception)."""
    req = app.CompileReq(code=case.program(device), language="pseudocode")
    try:
        return app.build(req), None
    except Exception as exc:                        # noqa: BLE001 -- that IS the finding
        return None, exc


def valid_intel_hex(text: str) -> bool:
    """Every record's checksum agrees. A truncated or corrupt image is
    exactly what a 'success' with bytes would otherwise hide."""
    saw = False
    for raw in text.splitlines():
        record = raw.strip()
        if not record:
            continue
        if record[0] != ":" or len(record) < 11 or len(record) % 2 == 0:
            return False
        octets = [int(record[i:i + 2], 16) for i in range(1, len(record), 2)]
        if sum(octets) & 0xFF:
            return False
        saw = True
    return saw


# --------------------------------------------------------------- the table

def test_every_device_is_accounted_for():
    """A device may not be added to the front end without declaring here
    whether it builds, is transpile-only, or is a recorded gap."""
    missing = sorted(set(sp.TARGETS) - set(EXPECTED))
    assert not missing, (
        f"devices with no declared outcome: {missing}. Add each to EXPECTED "
        f"in this file (and to the README's device table).")


def test_the_table_has_not_rotted():
    """The reverse: an entry here for a device that no longer exists means
    the table is describing something that is gone."""
    extra = sorted(set(EXPECTED) - set(sp.TARGETS))
    assert not extra, f"EXPECTED names devices the front end does not know: {extra}"


def test_every_known_gap_says_why():
    for device, case in EXPECTED.items():
        if case.outcome == KNOWN_GAP:
            assert case.gap and case.gap.strip(), \
                f"{device} is a known gap with no reason recorded"


def test_every_no_generator_declares_what_it_must_say():
    for device, case in EXPECTED.items():
        if case.outcome == NO_GENERATOR:
            assert case.says, (
                f"{device} has no generator but declares nothing its refusal "
                f"must say; a refusal nobody checks decays into a bare error")


def test_every_transpile_only_names_a_toolchain():
    for device, case in EXPECTED.items():
        if case.outcome == TRANSPILE_ONLY:
            assert case.toolchain, \
                f"{device} is transpile-only but does not say which toolchain"


# ---------------------------------------------------------- the real builds

BUILD_DEVICES = sorted(d for d, c in EXPECTED.items() if c.outcome == BUILDS)
NO_GEN_DEVICES = sorted(d for d, c in EXPECTED.items() if c.outcome == NO_GENERATOR)
REFUSE_DEVICES = sorted(d for d, c in EXPECTED.items() if c.outcome == TRANSPILE_ONLY)
GAP_DEVICES = sorted(d for d, c in EXPECTED.items() if c.outcome == KNOWN_GAP)


@pytest.mark.parametrize("device", BUILD_DEVICES)
def test_device_builds(device):
    """A real image, with real bytes, that is a real Intel HEX file."""
    case = EXPECTED[device]
    result, exc = compile_device(device, case)
    assert exc is None, f"{device}: build() raised {type(exc).__name__}: {exc}"
    assert result.get("success"), \
        f"{device}: {str(result.get('error'))[:400]}"
    assert result["bytes"] > 0, f"{device}: an empty image"
    import base64
    image = base64.b64decode(result["base64"]).decode("ascii", "replace")
    assert valid_intel_hex(image), f"{device}: image is not valid Intel HEX"


@pytest.mark.parametrize("device", REFUSE_DEVICES)
def test_device_is_transpile_only(device):
    """Refused on purpose: names the toolchain, and hands back the source
    anyway. Returning nothing would make the refusal useless."""
    case = EXPECTED[device]
    result, exc = compile_device(device, case)
    assert exc is None, f"{device}: build() raised {type(exc).__name__}: {exc}"
    assert result.get("success") is False, \
        f"{device}: expected a transpile-only refusal, got an image"
    assert result.get("toolchain") == case.toolchain, \
        f"{device}: refusal names {result.get('toolchain')!r}, expected {case.toolchain!r}"
    assert case.toolchain in str(result.get("error")), \
        f"{device}: the message does not name the toolchain the caller needs"
    assert result.get("c"), f"{device}: refused without returning the source"
    assert result.get("filename"), f"{device}: refused without naming the file"


@pytest.mark.parametrize("device", NO_GEN_DEVICES)
def test_device_has_no_generator_and_says_so(device):
    """Refused at the DEVICE line, with a message that names the device and
    points at whatever does work. The failure this guards against is not a
    crash -- it is a plausible-looking image for the wrong architecture,
    which is the one outcome nobody downstream can detect."""
    case = EXPECTED[device]
    result, exc = compile_device(device, case)
    assert exc is None, f"{device}: build() raised {type(exc).__name__}: {exc}"
    assert result.get("success") is False, \
        f"{device}: expected a refusal, got output"
    message = str(result.get("error", "")).lower()
    for fragment in case.says:
        assert fragment.lower() in message, \
            f"{device}: refusal does not mention {fragment!r}: {message[:200]}"
    # It must fail in the front end, not three layers into a compiler.
    assert result.get("stage") == "transpile", \
        f"{device}: refused at stage {result.get('stage')!r}, expected 'transpile'"


@pytest.mark.parametrize("device", GAP_DEVICES)
def test_known_gap_is_still_broken(device):
    """Asserted broken on purpose. When this fails, the gap is fixed: move
    the device to BUILDS here and delete its entry from the README's
    'Known gaps'. A gap list nobody maintains becomes a lie."""
    case = EXPECTED[device]
    result, exc = compile_device(device, case)
    broken = exc is not None or not result.get("success")
    assert broken, (
        f"{device} now builds -- the gap is closed. Update EXPECTED in this "
        f"file and the README's Known gaps section. Recorded reason was: "
        f"{case.gap}")


# ------------------------------------------- the DEVICE picks the size limits

# The parts the front end knows, with the flash each one actually has. An 8051
# DEVICE with no entry in app.TARGETS used to be compiled against whatever the
# request's `target` field said, which defaults to the STC12C5A60S2 -- so an
# image too big for an STC89's 8 KB linked cleanly and came back.
FLASH_KB = {
    "stc12c5a60s2": 60, "stc12c5a16s2": 16, "stc15f2k60s2": 60,
    "stc89c52": 8, "stc89c52rc": 8, "stc15w408as": 8,
}


def test_every_8051_device_has_size_limits():
    """A DEVICE the front end accepts but app.TARGETS has never heard of is
    silently compiled with someone else's ceiling."""
    sdcc = {k for k, t in sp.TARGETS.items() if t.toolchain == "sdcc-mcs51"}
    missing = sorted(sdcc - set(app.TARGETS))
    assert not missing, (
        f"8051 devices with no entry in app.TARGETS: {missing}. Without one "
        f"they inherit the request's target -- by default the STC12's 60 KB.")
    assert sdcc == set(FLASH_KB), \
        f"FLASH_KB is out of step with the front end: {sdcc ^ set(FLASH_KB)}"


def test_code_size_flag_matches_the_part():
    for device, kb in FLASH_KB.items():
        flags = app.TARGETS[device]["flags"]
        size = int(flags[flags.index("--code-size") + 1])
        assert size == kb * 1024, \
            f"{device}: --code-size {size}, but the part has {kb} KB"


@pytest.mark.parametrize("device", ["stc89c52rc", "stc15w408as"])
def test_an_image_too_big_for_the_part_is_refused(device):
    """The ceiling has to BITE, not merely be passed. A flash table of 12 KB
    fits an STC12 and cannot fit an 8 KB part; before the DEVICE selected the
    target this linked for every one of them.
    """
    blob = ", ".join(str(i % 256) for i in range(12000))
    source = (f"DEVICE {device.upper()}:\n  CLOCK 11059200\n"
              f"  TABLE blob = {blob}\n"
              f"  PIN led = P1.0 OUTPUT\n\n"
              f"  WHEN started:\n    set x to blob[1]\n    turn on led\n")
    result = app.build(app.CompileReq(code=source, language="pseudocode"))
    assert result.get("success") is False, \
        f"{device}: a {12000}-byte table linked into an 8 KB part"
    assert "Insufficient ROM" in str(result.get("error")), \
        f"{device}: refused, but not for running out of flash: " \
        f"{str(result.get('error'))[:200]}"


def test_the_same_image_still_fits_the_stc12():
    """The other half of the claim: the refusal above is about the part, not
    about the program being unbuildable."""
    blob = ", ".join(str(i % 256) for i in range(12000))
    source = ("DEVICE STC12C5A60S2:\n  CLOCK 11059200\n"
              f"  TABLE blob = {blob}\n"
              "  PIN led = P1.0 OUTPUT\n\n"
              "  WHEN started:\n    set x to blob[1]\n    turn on led\n")
    result = app.build(app.CompileReq(code=source, language="pseudocode"))
    assert result.get("success"), str(result.get("error"))[:300]


def test_the_device_beats_the_request_field():
    """`target` is what a caller sends when there is no DEVICE line to read.
    When there is one, it wins -- otherwise a front end that always sends
    target='stc12c5a60s2' (the default) silently unsets every other part's
    ceiling."""
    blob = ", ".join(str(i % 256) for i in range(12000))
    source = ("DEVICE STC89C52RC:\n  CLOCK 11059200\n"
              f"  TABLE blob = {blob}\n"
              "  PIN led = P1.0 OUTPUT\n\n"
              "  WHEN started:\n    set x to blob[1]\n    turn on led\n")
    result = app.build(app.CompileReq(code=source, language="pseudocode",
                                      target="stc12c5a60s2"))
    assert result.get("success") is False, \
        "the request's target overrode the DEVICE line"
