"""
stc-compiler — a REST compiler for the STC12C5A60S2 (and other 8051 parts).

POST C source, get back an Intel HEX image ready for stcgal. The heavy lifting
is SDCC's; this is a thin, stateless wrapper so a browser can reach it.

Shaped deliberately like CrispStrobe/legacy-lego-compiler so the BrickWright
extensions can talk to both with the same client code.
"""

import base64
import html
import os
import re
import shutil
import stat
import subprocess
import tempfile
import uuid

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


def build(req: CompileReq) -> dict:
    """Compile and return the JSON-shaped result. Shared by both endpoints."""
    if len(req.code.encode("utf-8")) > MAX_SOURCE_BYTES:
        return {"success": False, "error": "source too large"}

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

    cmd = [os.path.join(STAGE_BIN, "sdcc"), "-mmcs51", "--std-c99"]
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

        # SDCC's memory map. Useful enough to hand back that callers can warn
        # before an image silently outgrows the part.
        mem = ""
        mem_path = os.path.join(work, "main.mem")
        if os.path.exists(mem_path):
            with open(mem_path, encoding="utf-8", errors="replace") as handle:
                mem = handle.read()

        return {
            "success": True,
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


# --------------------------------------------------------------- imprint
# Site operator details for the About dialog. Kept in the environment rather
# than the source so the address is never committed to a public repo, and so
# a fork deploying its own instance does not publish someone else's details.
#
#   vercel env add IMPRINT_NAME production
#   vercel env add IMPRINT_ADDRESS production   # newlines allowed
#   vercel env add IMPRINT_EMAIL production
#
# Germany requires this on a publicly reachable service (§5 DDG, formerly TMG).
# If nothing is set the section is omitted rather than showing a placeholder.
def imprint_html() -> str:
    name = os.environ.get("IMPRINT_NAME", "").strip()
    address = os.environ.get("IMPRINT_ADDRESS", "").strip()
    email = os.environ.get("IMPRINT_EMAIL", "").strip()
    if not (name or address or email):
        return ""

    rows = []
    if name:
        rows.append(f"<strong>{html.escape(name)}</strong>")
    if address:
        rows.append("<br>".join(html.escape(line) for line in address.splitlines() if line.strip()))
    if email:
        safe = html.escape(email)
        rows.append(f'<a href="mailto:{safe}">{safe}</a>')

    return (
        '<h3>Imprint / Impressum</h3>'
        '<p class=dim>Angaben gem\u00e4\u00df \u00a7 5 DDG \u00b7 responsible for content</p>'
        "<address>" + "<br>".join(rows) + "</address>"
    )


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
  .sheet-foot { display:flex; justify-content:flex-end; gap:8px;
                padding:12px 26px; border-top:1px solid #2a2e37; background:#14161a; }
  code { font:12px ui-monospace,SFMono-Regular,Menlo,monospace; color:#b9c0cc; }
</style></head><body>
<header>
  <h1>stc-compiler <small>C &rarr; Intel HEX for STC12 / 8051, via SDCC</small></h1>
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
      <button data-pane=mem>Memory</button>
      <button data-pane=log>Log</button>
    </div>
    <div id=panes>
      <pre id=out>Press Compile, or POST to <code>/compile</code> for JSON and
<code>/download</code> for the raw file.

See <a href="/docs">/docs</a> for the schema and <a href="/health">/health</a> for the SDCC version.</pre>
      <pre id=mem hidden></pre>
      <pre id=log hidden></pre>
    </div>
  </aside>
</main>

<dialog id=about>
  <div class=sheet>
    <h2>stc-compiler</h2>
    <p class=dim>Compiles C to Intel HEX for the STC12C5A60S2 and other 8051 parts.
    The compile side of a Scratch-blocks-to-8051 back-end for BrickWright.</p>

    <h3>Made by</h3>
    <p><a href="https://github.com/CrispStrobe" target=_blank rel=noopener>CrispStrobe</a> &middot;
       <a href="https://github.com/CrispStrobe/stc-compiler" target=_blank rel=noopener>this service</a> &middot;
       <a href="https://github.com/CrispStrobe/stc12c5a60s2-lab" target=_blank rel=noopener>hardware guide</a> &middot;
       <a href="https://github.com/CrispStrobe/brickwright" target=_blank rel=noopener>BrickWright</a> &middot;
       <a href="https://github.com/CrispStrobe/legacy-lego-compiler" target=_blank rel=noopener>LEGO compiler</a></p>

    <h3>Licences</h3>
    <table>
      <tr><td>This service &mdash; API, UI, build scripts</td><td>MIT</td></tr>
      <tr><td><a href="https://sdcc.sourceforge.net/" target=_blank rel=noopener>SDCC</a> 4.0.0 &mdash;
              <code>sdcc</code>, <code>sdcpp</code>, <code>sdas8051</code>, <code>sdld</code>,
              <code>packihx</code>, <code>makebin</code></td><td>GPL-2.0-or-later</td></tr>
      <tr><td>SDCC runtime headers and libraries, including <code>mcs51/stc12.h</code></td>
          <td>GPL-2.0-or-later<br>with linking exception</td></tr>
      <tr><td><a href="https://github.com/fastapi/fastapi" target=_blank rel=noopener>FastAPI</a>,
              <a href="https://github.com/pydantic/pydantic" target=_blank rel=noopener>Pydantic</a></td><td>MIT</td></tr>
      <tr><td><a href="https://github.com/encode/starlette" target=_blank rel=noopener>Starlette</a>,
              <a href="https://github.com/encode/uvicorn" target=_blank rel=noopener>Uvicorn</a></td><td>BSD-3-Clause</td></tr>
    </table>

    <p><strong>What you compile here is yours.</strong> SDCC is GPL, but its runtime
    libraries and headers carry an explicit exception:</p>
    <blockquote>As a special exception, if you link this library with other files, some of
    which are compiled with SDCC, to produce an executable, this library does not by itself
    cause the resulting executable to be covered by the GNU General Public License.</blockquote>
    <p>The SDCC binaries come unmodified from Debian bullseye. Corresponding source:
    <code>apt-get source sdcc=4.0.0+dfsg-2</code>. Full detail in
    <a href="https://github.com/CrispStrobe/stc-compiler/blob/main/NOTICE.md" target=_blank rel=noopener>NOTICE.md</a>.</p>

    <h3>Disclaimer</h3>
    <p>Provided <strong>as is, without warranty of any kind</strong>, express or implied.
    You are responsible for what you flash and for the hardware you flash it to.</p>
    <ul>
      <li>The <strong>STC12C5A60S2 is a 5&nbsp;V part</strong> (3.5&ndash;5.5&nbsp;V). The
          <strong>STC12LE5A60S2 is not</strong> (2.1&ndash;3.6&nbsp;V) and 5&nbsp;V will
          destroy it. Check the marking on your chip.</li>
      <li>A wrong <code>FOSC</code> gives working code with wrong timing &mdash; verify it
          against a clock before trusting anything time-critical.</li>
      <li>Nothing here is checked for safety-critical, medical or industrial use.</li>
    </ul>

    <h3>Not affiliated</h3>
    <p>Not affiliated with, endorsed by or connected to STC MCU Limited (Hongjing
    Technology), the SDCC project, or the LEGO Group. All trademarks belong to their
    respective owners.</p>

    <h3>Data</h3>
    <p>Source you submit is compiled in an ephemeral container and the workspace is
    deleted as soon as the response is sent. Nothing is stored, logged to a database, or
    used for anything else. The hosting provider may keep ordinary request metadata such
    as IP address and timestamps in its own logs.</p>

    __IMPRINT__
  </div>
  <div class=sheet-foot><button class=primary id=aboutClose>Close</button></div>
</dialog>

<script>
const $ = id => document.getElementById(id);
let image = null;          // {bytes: Uint8Array, filename: string}

document.querySelectorAll('#tabs button').forEach(tab => {
  tab.onclick = () => {
    document.querySelectorAll('#tabs button').forEach(b => b.classList.toggle('on', b === tab));
    ['out', 'mem', 'log'].forEach(p => { $(p).hidden = (p !== tab.dataset.pane); });
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
        target: $('target').value,
        fosc: parseInt($('fosc').value, 10) || null,
        format,
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

$('aboutBtn').onclick = () => $('about').showModal();
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
                .replace("__IMPRINT__", imprint_html()))
