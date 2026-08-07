"""
stc-compiler — a REST compiler for the STC12C5A60S2 (and other 8051 parts).

POST C source, get back an Intel HEX image ready for stcgal. The heavy lifting
is SDCC's; this is a thin, stateless wrapper so a browser can reach it.

Shaped deliberately like CrispStrobe/legacy-lego-compiler so the BrickWright
extensions can talk to both with the same client code.
"""

import base64
import html
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import uuid

import keil2sdcc
import stc_disasm
import stc_pseudocode

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, PlainTextResponse, Response
from pydantic import BaseModel

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SRC_BIN = os.path.join(BASE_DIR, "bin")
SRC_SHARE = os.path.join(BASE_DIR, "share")

# Vercel's deployment directory is read-only and does not reliably preserve the
# executable bit, so the toolchain gets staged into /tmp once per container.
# We copy `share/` alongside `bin/` rather than pointing at the originals,
# because SDCC locates its headers and libraries as `<argv[0]>/../share/sdcc`.
# Keeping that relative layout intact means no path flags and no surprises.
STAGE = "/tmp/sdcc"
STAGE_BIN = os.path.join(STAGE, "bin")
STAGE_SHARE = os.path.join(STAGE, "share")

# A bare option value: numbers, identifiers, "0x1F", "a,b". Deliberately
# excludes '/' and '\\' so a path can never be smuggled in as an input file.
VALUE_RE = re.compile(r"[A-Za-z0-9_.,=+:-]+")

# Anything beyond this is a runaway, not a program.
COMPILE_TIMEOUT = 25
MAX_SOURCE_BYTES = 1_000_000

# Known parts. `defines` are handed to SDCC as -D flags; `flags` are the memory
# and size limits that make an image actually fit the chip.
TARGETS = {
    "stc12c5a60s2": {
        "flags": ["--iram-size", "256", "--xram-size", "1024",
                  "--code-size", "61440"],
        "description": "STC12C5A60S2 — 60 KB flash, 256 B IRAM, 1024 B XRAM",
    },
    "stc12c5a16s2": {
        "flags": ["--iram-size", "256", "--xram-size", "1024",
                  "--code-size", "16384"],
        "description": "STC12C5A16S2 — 16 KB flash",
    },
    # Escape hatch: no size limits, caller supplies everything via `options`.
    "mcs51": {
        "flags": [],
        "description": "Generic 8051, no size limits applied",
    },
}

app = FastAPI(title="stc-compiler", docs_url="/docs")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


def stage_toolchain():
    """Copy the toolchain into /tmp on cold start. Cheap and idempotent."""
    if os.path.isdir(STAGE_BIN) and os.path.exists(os.path.join(STAGE_BIN, "sdcc")):
        return
    os.makedirs(STAGE, exist_ok=True)
    if not os.path.isdir(STAGE_BIN):
        shutil.copytree(SRC_BIN, STAGE_BIN)
    if not os.path.isdir(STAGE_SHARE):
        shutil.copytree(SRC_SHARE, STAGE_SHARE)
    for name in os.listdir(STAGE_BIN):
        path = os.path.join(STAGE_BIN, name)
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


class CompileReq(BaseModel):
    code: str
    # "c" compiles the body as-is; "pseudocode" runs it through the
    # BrickWright-style front end first (see stc_pseudocode.py); "keil"
    # translates the Keil C51 dialect into SDCC's (see keil2sdcc.py).
    language: str = "c"
    target: str = "stc12c5a60s2"
    # Clock in Hz. Emitted as -DFOSC_HZ=<n>UL, which is what the
    # stc12c5a60s2-lab examples expect. None omits it entirely.
    fosc: int | None = 11059200
    # Extra -D defines, as {"NAME": "value"} or {"NAME": None} for bare defines.
    defines: dict[str, str | None] = {}
    # Raw extra SDCC flags, e.g. ["--model-large", "--opt-code-size"].
    options: list[str] = []
    # "ihx" is SDCC's raw output; "hex" runs it through packihx; "bin" through
    # makebin. stcgal accepts hex and bin.
    format: str = "hex"
    # Also return an 8051 disassembly of the image. Off by default: it is
    # several KB of text nobody asked for.
    disassemble: bool = False


def build(req: CompileReq) -> dict:
    """Compile and return the JSON-shaped result. Shared by both endpoints."""
    if len(req.code.encode("utf-8")) > MAX_SOURCE_BYTES:
        return {"success": False, "error": "source too large"}

    generated_c = None
    keil_changes: dict = {}
    keil_unresolved: list = []
    if req.language.lower() in ("pseudocode", "pseudo", "bw"):
        try:
            generated_c, program = stc_pseudocode.transpile(req.code)
        except stc_pseudocode.PseudocodeError as exc:
            return {"success": False, "error": str(exc), "line": exc.line,
                    "stage": "transpile"}
        req = req.model_copy(update={"code": generated_c})
        # The pseudocode's own CLOCK wins; FOSC_HZ is already baked into the C.
        req = req.model_copy(update={"fosc": None})
    elif req.language.lower() in ("keil", "c51"):
        stage_toolchain()      # SFR addresses come from the staged SDCC headers
        result = keil2sdcc.translate(req.code)
        generated_c = result.text
        keil_changes = result.changes
        keil_unresolved = result.unresolved
        req = req.model_copy(update={"code": generated_c})
    elif req.language.lower() != "c":
        return {"success": False,
                "error": f"unknown language '{req.language}'; use 'c' or 'pseudocode'"}

    target = TARGETS.get(req.target.lower())
    if target is None:
        return {"success": False,
                "error": f"unknown target '{req.target}'; "
                         f"known: {', '.join(sorted(TARGETS))}"}
    if req.format not in ("ihx", "hex", "bin"):
        return {"success": False, "error": "format must be ihx, hex or bin"}

    stage_toolchain()

    work = os.path.join(tempfile.gettempdir(), f"build-{uuid.uuid4().hex}")
    os.makedirs(work, exist_ok=True)
    src = os.path.join(work, "main.c")
    with open(src, "w", encoding="utf-8") as handle:
        handle.write(req.code)

    cmd = [os.path.join(STAGE_BIN, "sdcc"), "-mmcs51"]
    # Keil source predates C99 habits and leans on the older grammar; forcing
    # --std-c99 on it costs compiles for nothing.
    if not keil_changes:
        cmd.append("--std-c99")
    cmd += keil2sdcc.shim_args()
    cmd += target["flags"]
    if req.fosc:
        cmd.append(f"-DFOSC_HZ={int(req.fosc)}UL")
    for name, value in req.defines.items():
        if not name.replace("_", "").isalnum():
            return {"success": False, "error": f"bad define name: {name!r}"}
        cmd.append(f"-D{name}" if value is None else f"-D{name}={value}")
    # Flags, plus the bare values that some of them take ("--code-size", "64").
    # What we must not allow is a path: sdcc would happily accept it as a
    # second input file and compile something out of the container.
    for index, opt in enumerate(req.options):
        opt = str(opt)
        if opt.startswith("-"):
            cmd.append(opt)
            continue
        if index == 0 or not VALUE_RE.fullmatch(opt):
            return {"success": False,
                    "error": f"options must be flags, or plain values following "
                             f"a flag; rejected {opt!r}"}
        cmd.append(opt)
    cmd += ["-o", work + os.sep, src]

    env = dict(os.environ, PATH=STAGE_BIN + os.pathsep + os.environ.get("PATH", ""))

    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=COMPILE_TIMEOUT, cwd=work, env=env)
    except subprocess.TimeoutExpired:
        shutil.rmtree(work, ignore_errors=True)
        return {"success": False, "error": f"compile timed out after {COMPILE_TIMEOUT}s"}

    # The workspace path is an implementation detail; callers should see the
    # same "main.c:12: error" they would get compiling locally.
    log = ((result.stdout or "") + (result.stderr or "")).replace(work + os.sep, "")
    ihx = os.path.join(work, "main.ihx")

    if result.returncode != 0 or not os.path.exists(ihx):
        shutil.rmtree(work, ignore_errors=True)
        return {"success": False, "error": log.strip() or "compilation failed",
                "log": log}

    try:
        if req.format == "ihx":
            out, name = ihx, "main.ihx"
        elif req.format == "hex":
            out, name = os.path.join(work, "main.hex"), "main.hex"
            with open(out, "w", encoding="utf-8") as handle:
                packed = subprocess.run([os.path.join(STAGE_BIN, "packihx"), ihx],
                                        capture_output=True, text=True, timeout=10)
                handle.write(packed.stdout)
        else:
            out, name = os.path.join(work, "main.bin"), "main.bin"
            subprocess.run([os.path.join(STAGE_BIN, "makebin"), "-p", ihx, out],
                           capture_output=True, timeout=10)

        with open(out, "rb") as handle:
            blob = handle.read()

        listing = None
        if req.disassemble:
            try:
                with open(ihx, encoding="utf-8") as handle:
                    listing = stc_disasm.disassemble_hex(handle.read())
            except (ValueError, OSError) as exc:
                listing = f"(disassembly failed: {exc})"

        # SDCC's memory map. Useful enough to hand back that callers can warn
        # before an image silently outgrows the part.
        mem = ""
        mem_path = os.path.join(work, "main.mem")
        if os.path.exists(mem_path):
            with open(mem_path, encoding="utf-8", errors="replace") as handle:
                mem = handle.read()

        return {
            "success": True,
            "c": generated_c,          # None unless the source was translated
            "translated": keil_changes or None,
            "unresolved": keil_unresolved or None,
            "disassembly": listing,     # None unless disassemble was requested
            "base64": base64.b64encode(blob).decode("ascii"),
            "filename": name,
            "bytes": len(blob),
            "log": log,
            "memory": mem,
        }
    finally:
        shutil.rmtree(work, ignore_errors=True)


@app.post("/compile")
async def compile_source(req: CompileReq):
    """Compile and return the image base64-encoded inside JSON."""
    return build(req)


class DisassembleReq(BaseModel):
    """An Intel HEX image, as text or base64."""
    hex: str | None = None
    base64: str | None = None


@app.post("/disassemble")
async def disassemble_image(req: DisassembleReq):
    """Intel HEX in, 8051 assembly out.

    Handy for checking what actually landed in the image rather than trusting
    the compiler -- and for diffing two builds that should be identical.
    """
    text = req.hex
    if text is None and req.base64:
        try:
            text = base64.b64decode(req.base64).decode("utf-8", "replace")
        except Exception as exc:  # noqa: BLE001 - reported to the caller
            return {"success": False, "error": f"bad base64: {exc}"}
    if not text:
        return {"success": False, "error": "provide 'hex' or 'base64'"}
    try:
        memory = stc_disasm.parse_hex(text)
        entries = stc_disasm.disassemble(memory)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}
    return {
        "success": True,
        "disassembly": stc_disasm.format_listing(entries),
        "instructions": len(entries),
        "bytes": len(memory),
        "start": min(memory) if memory else 0,
        "end": max(memory) if memory else 0,
    }


@app.post("/translate")
async def translate_keil(req: CompileReq):
    """Keil C51 in, SDCC-dialect C out. No compiler involved."""
    stage_toolchain()
    result = keil2sdcc.translate(req.code)
    return {"success": True, "c": result.text,
            "translated": result.changes, "unresolved": result.unresolved}


@app.post("/decompile")
async def decompile_pseudocode(req: CompileReq):
    """Pseudocode in, canonical pseudocode out.

    The front end's AST is the source of truth, so this is `parse` followed by
    `emit_pseudocode`: normalised layout, comments dropped, and a fixed point
    -- feeding the result back in returns it unchanged.
    """
    try:
        return {"success": True, "pseudocode": stc_pseudocode.decompile(req.code)}
    except stc_pseudocode.PseudocodeError as exc:
        return {"success": False, "error": str(exc), "line": exc.line}


@app.post("/transpile")
async def transpile_only(req: CompileReq):
    """Pseudocode in, C out. No compiler involved -- useful for seeing exactly
    what the front end produced before handing it to SDCC."""
    try:
        code, program = stc_pseudocode.transpile(req.code)
    except stc_pseudocode.PseudocodeError as exc:
        return {"success": False, "error": str(exc), "line": exc.line}
    return {
        "success": True,
        "c": code,
        "part": program.part,
        "clock": program.clock,
        "pins": {name: {"sfr": pin.sfr, "direction": pin.direction,
                        "active_low": pin.active_low}
                 for name, pin in program.pins.items()},
        "variables": program.variables,
    }


@app.post("/download")
async def download(req: CompileReq):
    """Same as /compile, but returns the raw file with a filename attached.

    Saves callers from base64-decoding by hand:
        curl -X POST .../download -H 'Content-Type: application/json' \
             -d @req.json -OJ
    """
    result = build(req)
    if not result["success"]:
        return PlainTextResponse(result["error"], status_code=400)
    return Response(
        content=base64.b64decode(result["base64"]),
        media_type="application/octet-stream",
        headers={
            "Content-Disposition": f'attachment; filename="{result["filename"]}"',
            "X-Image-Bytes": str(result["bytes"]),
            # So a browser fetch() can read the size without a preflight dance.
            "Access-Control-Expose-Headers": "Content-Disposition, X-Image-Bytes",
        },
    )


# ----------------------------------------------------------------- about
# Mirrors the About screen of the sibling apps (CrisperWeaver, CrispSorter):
# service provider, contact, privacy, disclaimer, licence, then the list of
# third-party components. Bilingual, like the rest of this project's docs.
#
# The provider block can be overridden per-deployment so a fork does not
# publish someone else's details:  IMPRINT_NAME / _ADDRESS / _EMAIL / _PHONE.
PROVIDER_NAME = os.environ.get("IMPRINT_NAME", "Christian Ströbele")
PROVIDER_ADDRESS = os.environ.get(
    "IMPRINT_ADDRESS", "Nikolausstr. 5\n70190 Stuttgart\nDeutschland / Germany")
PROVIDER_EMAIL = os.environ.get("IMPRINT_EMAIL", "postmaster@crispstro.be")
PROVIDER_PHONE = os.environ.get("IMPRINT_PHONE", "+49 176 6421 8601")

SDCC_VERSION_LABEL = "4.0.0+dfsg-2"

LICENCE_ROWS = [
    ("This service &mdash; API, browser UI, build scripts",
     "MIT", "https://github.com/CrispStrobe/stc-compiler/blob/main/LICENSE"),
    ("<a href='https://sdcc.sourceforge.net/' target=_blank rel=noopener>SDCC</a> 4.0.0 &mdash; "
     "<code>sdcc</code>, <code>sdcpp</code>, <code>sdas8051</code>, <code>sdld</code>, "
     "<code>packihx</code>, <code>makebin</code>",
     "GPL-2.0-or-later", "https://www.gnu.org/licenses/old-licenses/gpl-2.0.html"),
    ("SDCC runtime headers and libraries, including <code>mcs51/stc12.h</code>",
     "GPL-2.0-or-later<br>with linking exception",
     "https://www.gnu.org/licenses/old-licenses/gpl-2.0.html"),
    ("<a href='https://github.com/fastapi/fastapi' target=_blank rel=noopener>FastAPI</a>, "
     "<a href='https://github.com/pydantic/pydantic' target=_blank rel=noopener>Pydantic</a>",
     "MIT", "https://opensource.org/license/mit"),
    ("<a href='https://github.com/encode/starlette' target=_blank rel=noopener>Starlette</a>, "
     "<a href='https://github.com/encode/uvicorn' target=_blank rel=noopener>Uvicorn</a>",
     "BSD-3-Clause", "https://opensource.org/license/bsd-3-clause"),
]

LINKING_EXCEPTION = (
    "As a special exception, if you link this library with other files, some of which "
    "are compiled with SDCC, to produce an executable, this library does not by itself "
    "cause the resulting executable to be covered by the GNU General Public License."
)

TEXT = {
    "en": {
        "tagline": "Compiles C to Intel HEX for the STC12C5A60S2 and other 8051 parts. "
                   "The compile side of a Scratch-blocks-to-8051 back-end for BrickWright.",
        "provider": "Service Provider",
        "contact": "Contact",
        "email": "Email",
        "phone": "Phone",
        "project": "Project",
        "privacy": "Privacy",
        "privacy_text":
            "Source you submit is compiled in an ephemeral container and the workspace is "
            "deleted as soon as the response is sent. Nothing is stored, put in a database, "
            "or used for anything else. No cookies, no analytics, no tracking. The hosting "
            "provider may keep ordinary request metadata such as IP address and timestamps "
            "in its own logs.",
        "disclaimer": "Disclaimer",
        "disclaimer_text":
            "This software is provided \u201cas is\u201d, without warranty of any kind, express "
            "or implied, including but not limited to the warranties of merchantability, "
            "fitness for a particular purpose and noninfringement. In no event shall the "
            "authors be liable for any claim, damages or other liability arising from, out "
            "of or in connection with the software or its use.",
        "hardware": "You are responsible for what you flash and for the hardware you flash it to:",
        "hw_1": "The <strong>STC12C5A60S2 is a 5&nbsp;V part</strong> (3.5&ndash;5.5&nbsp;V). "
                "The <strong>STC12LE5A60S2 is not</strong> (2.1&ndash;3.6&nbsp;V) and 5&nbsp;V "
                "will destroy it. Check the marking on your chip.",
        "hw_2": "A wrong <code>FOSC</code> produces working code that keeps the wrong time. "
                "Verify it against a clock before trusting anything time-critical.",
        "hw_3": "Nothing here is validated for safety-critical, medical or industrial use.",
        "licence": "Licence",
        "licence_text":
            "This service is free software under the MIT licence. It bundles the SDCC "
            "compiler, which is GPL-2.0-or-later.",
        "output_yours": "What you compile here is yours.",
        "output_text":
            "SDCC is GPL, but its runtime libraries and headers &mdash; the ones your program "
            "actually links against &mdash; carry an explicit exception:",
        "source_offer":
            "The SDCC binaries are unmodified, from Debian bullseye. Corresponding source: ",
        "components": "Third-party components",
        "affil": "Not affiliated",
        "affil_text":
            "Not affiliated with, endorsed by or connected to STC MCU Limited (Hongjing "
            "Technology), the SDCC project, or the LEGO Group. All trademarks belong to "
            "their respective owners.",
        "close": "Close",
    },
    "de": {
        "tagline": "Übersetzt C nach Intel HEX für den STC12C5A60S2 und andere 8051-Typen. "
                   "Die Compiler-Seite eines Scratch-Blöcke-nach-8051-Backends für BrickWright.",
        "provider": "Anbieter",
        "contact": "Kontakt",
        "email": "E-Mail",
        "phone": "Telefon",
        "project": "Projekt",
        "privacy": "Datenschutz",
        "privacy_text":
            "Eingesendeter Quelltext wird in einem flüchtigen Container übersetzt; das "
            "Arbeitsverzeichnis wird gelöscht, sobald die Antwort verschickt ist. Es wird "
            "nichts gespeichert, in eine Datenbank gelegt oder anderweitig verwendet. Keine "
            "Cookies, keine Analyse, kein Tracking. Der Hosting-Anbieter kann übliche "
            "Verbindungsdaten wie IP-Adresse und Zeitstempel in seinen eigenen Protokollen "
            "vorhalten.",
        "disclaimer": "Haftungsausschluss",
        "disclaimer_text":
            "Diese Software wird \u201ewie besehen\u201c zur Verfügung gestellt, ohne jegliche "
            "ausdrückliche oder stillschweigende Gewährleistung, insbesondere der "
            "Marktgängigkeit, Eignung für einen bestimmten Zweck oder der Nichtverletzung "
            "von Rechten Dritter. In keinem Fall haften die Autor:innen für Ansprüche, "
            "Schäden oder sonstige Haftungen, die sich aus oder im Zusammenhang mit der "
            "Software oder deren Nutzung ergeben.",
        "hardware": "Sie sind selbst dafür verantwortlich, was Sie flashen und auf welche Hardware:",
        "hw_1": "Der <strong>STC12C5A60S2 ist ein 5-V-Typ</strong> (3,5&ndash;5,5&nbsp;V). Der "
                "<strong>STC12LE5A60S2 ist es nicht</strong> (2,1&ndash;3,6&nbsp;V), 5&nbsp;V "
                "zerstören ihn. Prüfen Sie den Aufdruck auf dem Chip.",
        "hw_2": "Ein falsches <code>FOSC</code> ergibt lauffähigen Code mit falschen Zeiten. "
                "Vor zeitkritischem Einsatz gegen eine Uhr prüfen.",
        "hw_3": "Nichts hiervon ist für sicherheitskritische, medizinische oder industrielle "
                "Anwendungen geprüft.",
        "licence": "Lizenz",
        "licence_text":
            "Dieser Dienst ist freie Software unter der MIT-Lizenz. Er bündelt den "
            "SDCC-Compiler, der unter GPL-2.0-or-later steht.",
        "output_yours": "Was Sie hier übersetzen, gehört Ihnen.",
        "output_text":
            "SDCC steht unter der GPL, seine Laufzeitbibliotheken und Header &mdash; also das, "
            "wogegen Ihr Programm tatsächlich gelinkt wird &mdash; tragen jedoch eine "
            "ausdrückliche Ausnahme:",
        "source_offer":
            "Die SDCC-Binaries stammen unverändert aus Debian bullseye. Zugehöriger Quelltext: ",
        "components": "Fremdkomponenten",
        "affil": "Keine Verbindung",
        "affil_text":
            "Keine Verbindung zu, Billigung durch oder Zugehörigkeit zu STC MCU Limited "
            "(Hongjing Technology), dem SDCC-Projekt oder der LEGO-Gruppe. Alle Marken "
            "gehören ihren jeweiligen Inhabern.",
        "close": "Schließen",
    },
}

REPO_LINKS = (
    '<a href="https://github.com/CrispStrobe" target=_blank rel=noopener>CrispStrobe</a> &middot; '
    '<a href="https://github.com/CrispStrobe/stc-compiler" target=_blank rel=noopener>stc-compiler</a> &middot; '
    '<a href="https://github.com/CrispStrobe/stc12c5a60s2-lab" target=_blank rel=noopener>stc12c5a60s2-lab</a> &middot; '
    '<a href="https://github.com/CrispStrobe/brickwright" target=_blank rel=noopener>BrickWright</a> &middot; '
    '<a href="https://github.com/CrispStrobe/legacy-lego-compiler" target=_blank rel=noopener>legacy-lego-compiler</a>'
)


def about_pane(lang: str) -> str:
    """One language's worth of About content."""
    s = TEXT[lang]
    address = "<br>".join(html.escape(line) for line in PROVIDER_ADDRESS.splitlines() if line.strip())
    mail = html.escape(PROVIDER_EMAIL)
    tel = html.escape(PROVIDER_PHONE)
    tel_href = "tel:" + re.sub(r"[^+0-9]", "", PROVIDER_PHONE)

    rows = "".join(
        f"<tr><td>{what}</td><td><a href='{url}' target=_blank rel=noopener>{lic}</a></td></tr>"
        for what, lic, url in LICENCE_ROWS
    )

    return f"""<div class=pane data-lang="{lang}">
  <p class=dim>{s['tagline']}</p>

  <h3>{s['provider']}</h3>
  <address><strong>{html.escape(PROVIDER_NAME)}</strong><br>{address}</address>

  <h3>{s['contact']}</h3>
  <p>{s['email']}: <a href="mailto:{mail}">{mail}</a><br>
     {s['phone']}: <a href="{tel_href}">{tel}</a></p>

  <h3>{s['project']}</h3>
  <p>{REPO_LINKS}</p>

  <h3>{s['privacy']}</h3>
  <p>{s['privacy_text']}</p>

  <h3>{s['disclaimer']}</h3>
  <p>{s['disclaimer_text']}</p>
  <p>{s['hardware']}</p>
  <ul><li>{s['hw_1']}</li><li>{s['hw_2']}</li><li>{s['hw_3']}</li></ul>

  <h3>{s['licence']}</h3>
  <p>{s['licence_text']}</p>
  <p><strong>{s['output_yours']}</strong> {s['output_text']}</p>
  <blockquote>{LINKING_EXCEPTION}</blockquote>
  <p>{s['source_offer']}<code>apt-get source sdcc={SDCC_VERSION_LABEL}</code> &middot;
     <a href="https://github.com/CrispStrobe/stc-compiler/blob/main/NOTICE.md"
        target=_blank rel=noopener>NOTICE.md</a></p>

  <h3>{s['components']}</h3>
  <table>{rows}</table>

  <h3>{s['affil']}</h3>
  <p>{s['affil_text']}</p>
</div>"""


def about_html() -> str:
    return about_pane("en") + about_pane("de")


@app.get("/health")
async def health():
    stage_toolchain()
    try:
        version = subprocess.run([os.path.join(STAGE_BIN, "sdcc"), "--version"],
                                 capture_output=True, text=True, timeout=10).stdout
    except Exception as exc:  # noqa: BLE001 - surfaced to the caller as text
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "sdcc": version.strip().splitlines()[0] if version else "",
        "targets": {name: cfg["description"] for name, cfg in TARGETS.items()},
    }


EXAMPLE = """#include <stc12.h>

/* Blink two LEDs on P1.0 (pin 1) and P1.1 (pin 2), wired active-low:
   +5V --[1k]--|>|-- pin.  Writing 0 lights the LED. */

#define T0_RELOAD (65536UL - (FOSC_HZ / 12UL / 1000UL))

static void delay_ms(unsigned int ms)
{
    while (ms--) {
        TL0 = (unsigned char)(T0_RELOAD & 0xFF);
        TH0 = (unsigned char)(T0_RELOAD >> 8);
        TF0 = 0;
        TR0 = 1;
        while (!TF0) ;
        TR0 = 0;
        TF0 = 0;
    }
}

void main(void)
{
    P1M1 &= ~0x03;              /* P1.0, P1.1 -> push-pull */
    P1M0 |=  0x03;
    AUXR &= ~0x80;              /* Timer 0 at FOSC/12 */
    TMOD  = (TMOD & 0xF0) | 0x01;

    for (;;) {
        P1_0 = 0; P1_1 = 1; delay_ms(500);
        P1_0 = 1; P1_1 = 0; delay_ms(500);
    }
}
"""


# The page is a plain string with a __EXAMPLE__ placeholder rather than an
# f-string: JavaScript is mostly braces, and doubling every one of them to
# survive f-string interpolation makes it unreadable and easy to break.
PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>stc-compiler</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin:0; font:14px/1.5 -apple-system,BlinkMacSystemFont,system-ui,sans-serif;
         background:#14161a; color:#e6e6e6; display:flex; flex-direction:column; height:100vh; }
  header { padding:10px 18px; background:#1c1f26; border-bottom:1px solid #2a2e37;
           display:flex; gap:14px; align-items:center; flex-wrap:wrap; }
  h1 { font-size:15px; margin:0 auto 0 0; font-weight:600; white-space:nowrap; }
  h1 small { font-weight:400; color:#8a91a0; margin-left:8px; }
  main { flex:1; display:flex; min-height:0; }
  textarea { flex:1; border:0; padding:16px; resize:none; background:#14161a; color:#e6e6e6;
             font:13px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace; outline:none; }
  aside { width:44%; min-width:300px; border-left:1px solid #2a2e37;
          display:flex; flex-direction:column; background:#101216; }
  #tabs { display:flex; gap:2px; padding:8px 10px 0; border-bottom:1px solid #2a2e37; }
  #tabs button { background:none; border:0; border-bottom:2px solid transparent;
                 color:#8a91a0; padding:6px 12px; font-size:12px; cursor:pointer; }
  #tabs button.on { color:#e6e6e6; border-bottom-color:#3b82f6; }
  #panes { flex:1; overflow:auto; padding:14px 16px; }
  pre { margin:0; white-space:pre-wrap; word-break:break-all;
        font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace; color:#b9c0cc; }
  button.primary { background:#3b82f6; color:#fff; border:0; padding:7px 16px;
                   border-radius:6px; font-size:13px; font-weight:500; cursor:pointer; }
  button.ghost { background:#22262f; color:#e6e6e6; border:1px solid #333945;
                 padding:7px 14px; border-radius:6px; font-size:13px; cursor:pointer; }
  button:disabled { opacity:.4; cursor:default; }
  select, input { background:#22262f; color:#e6e6e6; border:1px solid #333945;
                  border-radius:6px; padding:6px 8px; font-size:13px; }
  label { color:#8a91a0; font-size:12px; display:flex; align-items:center; gap:5px; }
  .ok { color:#4ade80; } .err { color:#f87171; } .dim { color:#8a91a0; }
  a { color:#7aa2f7; }
  button.link { background:none; border:0; color:#8a91a0; font-size:13px;
                cursor:pointer; text-decoration:underline; padding:4px; }
  button.link:hover { color:#e6e6e6; }
  dialog { background:#181b21; color:#e6e6e6; border:1px solid #2a2e37; border-radius:10px;
           max-width:640px; width:calc(100% - 32px); padding:0; }
  dialog::backdrop { background:rgba(0,0,0,.6); }
  .sheet { padding:22px 26px 26px; max-height:78vh; overflow:auto; }
  dialog h2 { margin:0 0 4px; font-size:17px; }
  dialog h3 { margin:22px 0 6px; font-size:13px; text-transform:uppercase;
              letter-spacing:.06em; color:#8a91a0; }
  dialog p, dialog li { font-size:13px; line-height:1.6; color:#c8cedb; margin:6px 0; }
  dialog ul { margin:6px 0; padding-left:18px; }
  dialog table { width:100%; border-collapse:collapse; margin:8px 0; font-size:12.5px; }
  dialog td { padding:5px 8px 5px 0; border-bottom:1px solid #23272f;
              vertical-align:top; color:#c8cedb; }
  dialog td:last-child { text-align:right; white-space:nowrap; color:#8a91a0; }
  dialog address { font-style:normal; font-size:13px; line-height:1.65; color:#c8cedb; }
  dialog blockquote { margin:8px 0; padding:8px 12px; border-left:2px solid #3b82f6;
                      background:#14161a; font-size:12.5px; color:#b9c0cc; }
  .sheet-head { display:flex; align-items:center; gap:12px; padding:18px 26px 12px;
               border-bottom:1px solid #2a2e37; }
  .sheet-head h2 { margin:0 auto 0 0; font-size:17px; }
  .langs { display:flex; gap:4px; }
  button.lang { background:none; border:1px solid #2a2e37; color:#8a91a0;
                border-radius:6px; padding:4px 10px; font-size:12px; cursor:pointer; }
  button.lang.on { color:#e6e6e6; border-color:#3b82f6; }
  .pane[hidden] { display:none; }
  .sheet-foot { display:flex; justify-content:flex-end; gap:8px;
                padding:12px 26px; border-top:1px solid #2a2e37; background:#14161a; }
  code { font:12px ui-monospace,SFMono-Regular,Menlo,monospace; color:#b9c0cc; }
</style></head><body>
<header>
  <h1>stc-compiler <small>C &rarr; Intel HEX for STC12 / 8051, via SDCC</small></h1>
  <label>language
    <select id=language>
      <option value=pseudocode>Pseudocode</option>
      <option value=c selected>C</option>
    </select>
  </label>
  <label>target
    <select id=target>
      <option value=stc12c5a60s2>STC12C5A60S2</option>
      <option value=stc12c5a16s2>STC12C5A16S2</option>
      <option value=mcs51>generic 8051</option>
    </select>
  </label>
  <label>FOSC <input id=fosc value=11059200 size=9></label>
  <label>format
    <select id=format>
      <option value=hex>.hex (packihx)</option>
      <option value=ihx>.ihx (raw)</option>
      <option value=bin>.bin</option>
    </select>
  </label>
  <button class=primary id=go>Compile</button>
  <button class=ghost id=dl disabled>Download</button>
  <button class=ghost id=copy disabled>Copy</button>
  <span id=status class=dim>ready</span>
  <button class=link id=aboutBtn title="Licences, disclaimer and imprint">About</button>
</header>
<main>
  <textarea id=code spellcheck=false>__EXAMPLE__</textarea>
  <aside>
    <div id=tabs>
      <button class=on data-pane=out>Output</button>
      <button data-pane=cgen hidden>C</button>
      <button data-pane=asm hidden>Disassembly</button>
      <button data-pane=mem>Memory</button>
      <button data-pane=log>Log</button>
    </div>
    <div id=panes>
      <pre id=out>Press Compile, or POST to <code>/compile</code> for JSON and
<code>/download</code> for the raw file.

See <a href="/docs">/docs</a> for the schema and <a href="/health">/health</a> for the SDCC version.</pre>
      <pre id=cgen hidden></pre>
      <pre id=asm hidden></pre>
      <pre id=mem hidden></pre>
      <pre id=log hidden></pre>
    </div>
  </aside>
</main>

<dialog id=about>
  <div class=sheet-head>
    <h2>stc-compiler</h2>
    <div class=langs>
      <button class="lang on" data-lang=en>English</button>
      <button class=lang data-lang=de>Deutsch</button>
    </div>
  </div>
  <div class=sheet>__ABOUT__</div>
  <div class=sheet-foot><button class=primary id=aboutClose>Close</button></div>
</dialog>

<script>
const $ = id => document.getElementById(id);
let image = null;          // {bytes: Uint8Array, filename: string}

document.querySelectorAll('#tabs button').forEach(tab => {
  tab.onclick = () => {
    document.querySelectorAll('#tabs button').forEach(b => b.classList.toggle('on', b === tab));
    ['out', 'cgen', 'asm', 'mem', 'log'].forEach(p => { $(p).hidden = (p !== tab.dataset.pane); });
  };
});

function bytesFromBase64(b64) {
  const binary = atob(b64);
  const out = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) out[i] = binary.charCodeAt(i);
  return out;
}

// .bin is not text, so show a classic offset/hex/ascii dump instead of mojibake.
function hexdump(bytes, limit = 4096) {
  const lines = [];
  const n = Math.min(bytes.length, limit);
  for (let off = 0; off < n; off += 16) {
    const row = bytes.subarray(off, Math.min(off + 16, n));
    const hex = [...row].map(b => b.toString(16).padStart(2, '0')).join(' ').padEnd(47);
    const ascii = [...row].map(b => (b >= 32 && b < 127) ? String.fromCharCode(b) : '.').join('');
    lines.push(off.toString(16).padStart(6, '0') + '  ' + hex + '  |' + ascii + '|');
  }
  if (bytes.length > limit) lines.push(`... ${bytes.length - limit} more bytes`);
  return lines.join('\n');
}

function setBusy(busy) {
  $('go').disabled = busy;
  if (busy) { $('dl').disabled = true; $('copy').disabled = true; }
}

$('go').onclick = async () => {
  setBusy(true);
  $('status').className = 'dim';
  $('status').textContent = 'compiling...';
  image = null;
  const format = $('format').value;
  try {
    const response = await fetch('/compile', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        code: $('code').value,
        language: $('language').value,
        target: $('target').value,
        fosc: parseInt($('fosc').value, 10) || null,
        format,
        disassemble: true,
      })
    });
    const data = await response.json();
    if (data.success) {
      image = {bytes: bytesFromBase64(data.base64), filename: data.filename};
      $('status').className = 'ok';
      $('status').textContent = data.bytes + ' bytes → ' + data.filename;
      $('out').textContent = (format === 'bin')
        ? hexdump(image.bytes)
        : new TextDecoder().decode(image.bytes);
      $('asm').textContent = data.disassembly || '';
      document.querySelector('#tabs button[data-pane=asm]').hidden = !data.disassembly;
      $('cgen').textContent = data.c || '';
      document.querySelector('#tabs button[data-pane=cgen]').hidden = !data.c;
      $('mem').textContent = data.memory || '(none)';
      $('log').textContent = data.log || '(no warnings)';
      $('dl').disabled = false;
      $('copy').disabled = (format === 'bin');
    } else {
      $('status').className = 'err';
      $('status').textContent = 'failed';
      $('out').textContent = data.error || 'unknown error';
      $('log').textContent = data.log || data.error || '';
    }
  } catch (err) {
    $('status').className = 'err';
    $('status').textContent = 'error';
    $('out').textContent = String(err);
  }
  setBusy(false);
};

$('dl').onclick = () => {
  if (!image) return;
  const url = URL.createObjectURL(new Blob([image.bytes], {type: 'application/octet-stream'}));
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = image.filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  // Revoke on the next tick; Safari cancels the download if it goes too early.
  setTimeout(() => URL.revokeObjectURL(url), 1000);
};

$('copy').onclick = async () => {
  if (!image) return;
  await navigator.clipboard.writeText(new TextDecoder().decode(image.bytes));
  const previous = $('copy').textContent;
  $('copy').textContent = 'Copied';
  setTimeout(() => { $('copy').textContent = previous; }, 1200);
};

const STARTERS = {c: $('code').value, pseudocode: __PSEUDO_EXAMPLE__};
let lastLanguage = $('language').value;
$('language').onchange = () => {
  const next = $('language').value;
  // Only clobber the editor if it still holds the untouched starter program.
  if ($('code').value.trim() === (STARTERS[lastLanguage] || '').trim()) {
    $('code').value = STARTERS[next];
  }
  lastLanguage = next;
  $('fosc').disabled = (next === 'pseudocode');   // pseudocode carries CLOCK
};
$('language').onchange();

$('aboutBtn').onclick = () => $('about').showModal();
document.querySelectorAll('button.lang').forEach(btn => {
  btn.onclick = () => {
    document.querySelectorAll('button.lang').forEach(b => b.classList.toggle('on', b === btn));
    document.querySelectorAll('#about .pane').forEach(pane => {
      pane.hidden = (pane.dataset.lang !== btn.dataset.lang);
    });
    $('aboutClose').textContent = (btn.dataset.lang === 'de') ? 'Schlie\u00dfen' : 'Close';
  };
});
// Start on the browser's language if it is German.
if ((navigator.language || '').toLowerCase().startsWith('de')) {
  document.querySelector('button.lang[data-lang=de]').click();
} else {
  document.querySelectorAll('#about .pane').forEach(p => { p.hidden = (p.dataset.lang !== 'en'); });
}
$('aboutClose').onclick = () => $('about').close();
// Click outside the sheet closes it; <dialog> already handles Escape.
$('about').addEventListener('click', event => {
  if (event.target === $('about')) $('about').close();
});

// Cmd/Ctrl-Enter compiles, like every other editor.
$('code').addEventListener('keydown', event => {
  if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') { event.preventDefault(); $('go').click(); }
});
</script></body></html>
"""


@app.get("/", response_class=HTMLResponse)
async def index():
    # RCDATA inside <textarea> tolerates a bare "<", but "&" would be read as
    # an entity -- and the example has "&=" in it. Escape properly.
    return (PAGE.replace("__EXAMPLE__", html.escape(EXAMPLE))
                .replace("__PSEUDO_EXAMPLE__", json.dumps(stc_pseudocode.EXAMPLE))
                .replace("__ABOUT__", about_html()))
