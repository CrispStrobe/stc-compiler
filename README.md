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

### `POST /transpile`

Pseudocode in, C out, no compiler involved — for seeing exactly what the front
end produced. Returns `c`, plus the resolved `part`, `clock`, `pins` and
`variables`. Errors carry a `line`.

### `POST /download`

Identical request body, but returns the **raw file** with a filename attached
instead of base64 inside JSON — so `curl` can just save it:

```bash
curl -X POST https://stc-compiler.vercel.app/download \
     -H 'Content-Type: application/json' \
     -d '{"code": "#include <stc12.h>\nvoid main(void) { P1 = 0; for(;;); }"}' \
     -OJ                       # -OJ takes the name from Content-Disposition
# -> main.hex
```

Response headers: `Content-Disposition: attachment; filename="main.hex"`,
`X-Image-Bytes`, and `Access-Control-Expose-Headers` so a browser `fetch()` can
read both. A compile failure returns **400** with the compiler output as plain
text.

### `GET /health`

Reports the SDCC version and the known targets. Also the cheapest way to see
whether a cold start staged the toolchain correctly.

### `GET /`

A small browser UI with the blink example preloaded — no build step, no CDN.
Pick a target, clock and output format, compile with the button or
<kbd>Cmd</kbd>/<kbd>Ctrl</kbd>+<kbd>Enter</kbd>, then **Download** the image or
**Copy** it to the clipboard. Output, SDCC's memory map and the build log are
on separate tabs; `.bin` is shown as a hex dump rather than mojibake.

**About dialog** — an *About* link in the header opens a modal listing every
licence (this wrapper MIT; SDCC GPL-2.0-or-later; its runtime headers with the
linking exception, quoted in full), the corresponding-source pointer, links to
the related CrispStrobe repositories, a warranty and hardware disclaimer, a
non-affiliation statement, what happens to submitted source, and the imprint.

### Imprint / Impressum

The operator details in the About dialog come from environment variables, so a
real postal address is never committed to a public repository and a fork
deploying its own instance does not publish someone else's details:

```bash
vercel env add IMPRINT_NAME production      # e.g. Jane Doe
vercel env add IMPRINT_ADDRESS production   # multi-line is fine
vercel env add IMPRINT_EMAIL production
vercel --prod                               # redeploy to pick them up
```

If none are set the section is omitted entirely rather than rendering a
placeholder. Note that a publicly reachable service operated from Germany
generally needs one (§ 5 DDG, formerly TMG).

### `GET /docs`

FastAPI's generated OpenAPI documentation.

---

## Pseudocode

Set `"language": "pseudocode"` and the body goes through a BrickWright-style
front end first. The dialect follows the conventions already used by
[`sb3-creator`](https://github.com/CrispStrobe/sb3-creator)'s pseudocode —
UPPERCASE for structure and control flow, lowercase for statements,
indentation for nesting:

```
DEVICE STC12C5A60S2:
  CLOCK 11059200
  PIN led1 = P1.0 OUTPUT ACTIVE LOW
  PIN led2 = P1.1 OUTPUT ACTIVE LOW
  PIN button = P3.2 INPUT

  WHEN started:
    set counter to 0
    FOREVER:
      REPEAT 6:
        turn on led1
        turn off led2
        wait 0.15 seconds
        turn off led1
        turn on led2
        wait 0.15 seconds
      change counter by 1
      IF counter > 2 THEN:
        set counter to 0
```

| Form | Notes |
|---|---|
| `DEVICE <part>:` | optional wrapper, like `SPRITE Name:` |
| `CLOCK <hz>` / `CLOCK <n> MHz` | overrides the `fosc` request field |
| `PIN <name> = P1.0 OUTPUT [ACTIVE LOW]` | `ACTIVE LOW` makes `turn on` drive 0 |
| `PIN <name> = P3.2 INPUT [ACTIVE LOW]` | readable in expressions |
| `PIN <name> = P1.2 ANALOG` | 10-bit ADC; channel *n* is on `P1.n`, so P1 only |
| `DEFINE <name> (a) (b):` | procedure; callable as `name 3, 80` or `name(3, 80)` |
| `WHEN started:` | also accepts `WHEN flag clicked:` |
| `FOREVER:` · `REPEAT n:` · `IF c THEN:` / `ELSE:` | indentation-scoped |
| `WHILE c:` · `REPEAT UNTIL c:` · `wait until c` | |
| `turn on/off <pin>` · `set <pin> high/low` · `toggle <pin>` | |
| `wait <n> seconds` / `<n> ms` | Timer 0, not a spin loop |
| `set <v> to <e>` · `change <v> by <e>` | variables are 16-bit `int` |
| `+ - * / %`, `= != < > <= >=`, `and or not` | `=` compares, as in Scratch |

Procedures may be called before they are defined — the order people actually
write in — because headers are registered in a first pass. Parameters are
locals, so they never leak into the global variable list.

`ANALOG` pins read through the ADC wherever they appear in an expression, so
`IF pot > 512 THEN:` just works: the emitter adds the polled `adc_read()`
helper, sets `P1ASF`, puts the pin in high-impedance mode and powers the
converter. Channel *n* lives on `P1.n`, which is why `ANALOG` is rejected on
any other port.

`ACTIVE LOW` is the point of the whole thing: a quasi-bidirectional 8051 pin
sinks 20 mA but sources ~230 µA, so LEDs get wired active-low and `turn on`
has to emit a `0`. The front end knows that, so the pseudocode never has to.

Response includes the generated `c` alongside the image, so nothing is hidden.

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
