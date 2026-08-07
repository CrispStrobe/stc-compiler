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
    return PAGE.replace("__EXAMPLE__", html.escape(EXAMPLE))
