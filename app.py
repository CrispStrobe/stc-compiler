"""
stc-compiler — a REST compiler for the STC12C5A60S2 (and other 8051 parts).

POST C source, get back an Intel HEX image ready for stcgal. The heavy lifting
is SDCC's; this is a thin, stateless wrapper so a browser can reach it.

Shaped deliberately like CrispStrobe/legacy-lego-compiler so the BrickWright
extensions can talk to both with the same client code.
"""

import base64
import os
import shutil
import stat
import subprocess
import tempfile
import uuid

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
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


@app.post("/compile")
async def compile_source(req: CompileReq):
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
    cmd += [str(opt) for opt in req.options]
    cmd += ["-o", work + os.sep, src]

    env = dict(os.environ, PATH=STAGE_BIN + os.pathsep + os.environ.get("PATH", ""))

    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=COMPILE_TIMEOUT, cwd=work, env=env)
    except subprocess.TimeoutExpired:
        shutil.rmtree(work, ignore_errors=True)
        return {"success": False, "error": f"compile timed out after {COMPILE_TIMEOUT}s"}

    log = (result.stdout or "") + (result.stderr or "")
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


@app.get("/", response_class=HTMLResponse)
async def index():
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>stc-compiler</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; font:14px/1.5 -apple-system,BlinkMacSystemFont,system-ui,sans-serif;
         background:#14161a; color:#e6e6e6; display:flex; flex-direction:column; height:100vh; }}
  header {{ padding:12px 20px; background:#1c1f26; border-bottom:1px solid #2a2e37;
            display:flex; gap:16px; align-items:center; flex-wrap:wrap; }}
  h1 {{ font-size:15px; margin:0; font-weight:600; }}
  h1 small {{ font-weight:400; color:#8a91a0; margin-left:8px; }}
  main {{ flex:1; display:flex; min-height:0; }}
  textarea {{ flex:1; border:0; padding:16px; resize:none; background:#14161a; color:#e6e6e6;
              font:13px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace; outline:none; }}
  aside {{ width:42%; min-width:280px; border-left:1px solid #2a2e37; padding:16px;
           overflow:auto; background:#101216; }}
  pre {{ margin:0; white-space:pre-wrap; word-break:break-all;
         font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace; color:#b9c0cc; }}
  button {{ background:#3b82f6; color:#fff; border:0; padding:7px 16px; border-radius:6px;
            font-size:13px; font-weight:500; cursor:pointer; }}
  button:disabled {{ opacity:.5; cursor:default; }}
  select, input {{ background:#22262f; color:#e6e6e6; border:1px solid #333945;
                   border-radius:6px; padding:6px 8px; font-size:13px; }}
  label {{ color:#8a91a0; font-size:12px; }}
  .ok {{ color:#4ade80; }} .err {{ color:#f87171; }}
  a {{ color:#7aa2f7; }}
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
  <button id=go>Compile</button>
  <span id=status></span>
</header>
<main>
  <textarea id=code spellcheck=false>{EXAMPLE}</textarea>
  <aside><pre id=out>POST /compile with {{"code": "...", "target": "stc12c5a60s2"}}.
See <a href="/docs">/docs</a> for the schema, <a href="/health">/health</a> for the SDCC version.</pre></aside>
</main>
<script>
const $ = id => document.getElementById(id);
let blob = null;
$('go').onclick = async () => {{
  $('go').disabled = true; $('status').textContent = 'compiling...';
  try {{
    const r = await fetch('/compile', {{
      method: 'POST', headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{code: $('code').value, target: $('target').value,
                            fosc: parseInt($('fosc').value, 10) || null}})
    }});
    const d = await r.json();
    if (d.success) {{
      blob = d.base64;
      $('status').innerHTML = '<span class=ok>' + d.bytes + ' bytes</span>';
      $('out').textContent = atob(d.base64) + (d.memory ? '\\n\\n' + d.memory : '');
    }} else {{
      $('status').innerHTML = '<span class=err>failed</span>';
      $('out').textContent = d.error || 'unknown error';
    }}
  }} catch (e) {{
    $('status').innerHTML = '<span class=err>error</span>';
    $('out').textContent = String(e);
  }}
  $('go').disabled = false;
}};
</script></body></html>"""
