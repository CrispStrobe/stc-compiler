# stc-compiler

A serverless REST API that compiles **C to Intel HEX for the STC12C5A60S2** and
other 8051 parts, using [SDCC](https://sdcc.sourceforge.net/). POST source, get
back an image ready to hand to [`stcgal`](https://github.com/grigorig/stcgal) —
or to a browser that speaks the ISP protocol itself.

Deliberately shaped like
[`CrispStrobe/legacy-lego-compiler`](https://github.com/CrispStrobe/legacy-lego-compiler),
which does the same job for LEGO NXT and EV3 bytecode, so client code is nearly
identical between them.

---

## Who calls this

The compile side of the STC12 work in
[`CrispStrobe/stc12c5a60s2-lab`](https://github.com/CrispStrobe/stc12c5a60s2-lab),
and eventually the BrickWright block-to-8051 back-end: the browser generates C,
POSTs it here, gets back a `.hex`, and flashes it over Web Serial.

It is a separate deployment from `legacy-lego-compiler` on purpose. SDCC is
GPL-2.0-or-later; that repository's story is MIT plus MPL/BSD, and there is no
reason to entangle the two. See [`NOTICE.md`](NOTICE.md) for exactly what the
GPL does and does not reach here — short version: the wrapper stays MIT, and
your compiled `.hex` is yours, thanks to SDCC's linking exception.

---

## API

### `POST /compile`

```jsonc
{
  "code":    "#include <stc12.h>\nvoid main(void) { ... }",  // required
  "target":  "stc12c5a60s2",   // stc12c5a60s2 | stc12c5a16s2 | mcs51
  "fosc":    11059200,         // emitted as -DFOSC_HZ=11059200UL; null to omit
  "defines": { "DEBUG": "1" }, // extra -D flags; null value = bare define
  "options": ["--opt-code-size"], // raw SDCC flags
  "format":  "hex"             // hex (packihx) | ihx (raw) | bin (makebin)
}
```

Success:

```jsonc
{
  "success":  true,
  "base64":   "OjAzMDAwMDAwMDIw...",  // the image
  "filename": "main.hex",
  "bytes":    308,
  "log":      "",
  "memory":   "Internal RAM layout:\n..."  // SDCC's memory map
}
```

Failure returns `{"success": false, "error": "<compiler output>"}`.

The `memory` field is worth surfacing to users — it is how you catch an image
quietly outgrowing 60 KB of flash or 248 bytes of stack.

### `GET /health`

Reports the SDCC version and the known targets. Also the cheapest way to see
whether a cold start staged the toolchain correctly.

### `GET /`

A small browser UI with the blink example preloaded — no build step, no CDN.

### `GET /docs`

FastAPI's generated OpenAPI documentation.

---

## Targets

| `target` | Applied limits |
|---|---|
| `stc12c5a60s2` | `--iram-size 256 --xram-size 1024 --code-size 61440` |
| `stc12c5a16s2` | as above, `--code-size 16384` |
| `mcs51` | none — bring your own via `options` |

Every target compiles `-mmcs51 --std-c99`. Adding a part is three lines in
`TARGETS` in [`app.py`](app.py).

---

## Calling it

### JavaScript (browser / TurboWarp extension)

```javascript
async function compileSTC12(code) {
  const res = await fetch('https://stc-compiler.vercel.app/compile', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ code, target: 'stc12c5a60s2', fosc: 11059200 }),
  });
  const data = await res.json();
  if (!data.success) throw new Error(data.error);
  return data.base64;              // hand to the flasher
}
```

### Python

```python
import base64, requests

def compile_stc12(code, out="main.hex"):
    res = requests.post(
        "https://stc-compiler.vercel.app/compile",
        json={"code": code, "target": "stc12c5a60s2"},
    ).json()
    if not res.get("success"):
        raise RuntimeError(res.get("error", "compile failed"))
    with open(out, "wb") as f:
        f.write(base64.b64decode(res["base64"]))
```

Then flash it:

```bash
stcgal -P stc12 -p /dev/cu.usbserial-XXXX -l 2400 -b 115200 main.hex
```

---

## Architecture

Pre-built Linux binaries shipped next to a thin FastAPI wrapper, so each
invocation can `exec()` the compiler against a writable `/tmp` workspace.

```
POST /compile
   └─ stage toolchain into /tmp/sdcc   (once per container, ~8 MB)
   └─ write source to /tmp/build-<uuid>/main.c
   └─ exec bin/sdcc ─┬─ bin/sdcpp      (preprocessor)
                     ├─ bin/sdas8051   (assembler)
                     └─ bin/sdld       (linker)      → main.ihx
   └─ bin/packihx or bin/makebin       → main.hex / main.bin
   └─ base64 → JSON, delete the workspace
```

Two details that matter and are easy to get wrong:

**SDCC finds its headers relative to `argv[0]`**, as `<binary>/../share/sdcc`.
That is why the staging step copies `share/` alongside `bin/` instead of
pointing at the read-only originals — keeping the relative layout intact means
no path flags and no duplicate-library link errors.

**SDCC is four programs, not one.** It `fork`/`exec`s `sdcpp`, then `sdas8051`,
then `sdld` — confirm any time with `sdcc -V`. All four have to be present and
executable, which is what the `/tmp` staging and `chmod` are for.

---

## Building the vendored toolchain

```bash
./scripts/fetch-sdcc.sh
```

This lifts the binaries out of Debian's `.deb` packages and strips them to the
mcs51 target only — a full SDCC install is ~150 MB, almost all of it pic16
libraries and documentation. The result is **~8 MB**, against Vercel's 250 MB
uncompressed function limit.

**Why Debian bullseye (SDCC 4.0.0) rather than the newest release:** glibc
symbol versioning is forward-compatible only. Bullseye's toolchain caps the
requirement at `GLIBC_2.29`, which runs fine on Vercel's Amazon Linux 2023
(glibc 2.34). Bookworm's SDCC 4.5.0 needs `GLIBC_2.36` and would not start.
Check before bumping:

```bash
strings bin/sdas8051 | grep -oE 'GLIBC_2\.[0-9]+' | sort -V | tail -1
```

---

## Deploying

```bash
vercel --prod
```

`vercel.json` uses the legacy `builds` config, which includes the whole project
directory in the function bundle — `bin/` and `share/` included. The newer
config traces Python imports only and would silently omit them.

Compiles take well under a second (~0.2 s for a small program), so the default
function duration is not a consideration.

---

## Licensing

- **This wrapper** — MIT, see [`LICENSE`](LICENSE).
- **SDCC** — GPL-2.0-or-later. Its runtime libraries and headers carry a
  linking exception, so binaries you compile with it are unencumbered.
- Full detail, including why serving it over HTTP is not distribution:
  [`NOTICE.md`](NOTICE.md).
