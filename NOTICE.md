# Third-Party Notices & Attribution

`stc-compiler` is distributed under the **MIT** license (see [`LICENSE`](LICENSE)),
which covers **only CrispStrobe's original wrapper code** — `app.py`, the
deployment configuration, and `scripts/fetch-sdcc.sh`.

The repository also ships **pre-compiled third-party compiler binaries and
their headers and libraries**, which are *not* CrispStrobe's work and retain
their own upstream licenses.

| Vendored artifact | Upstream project | License |
|---|---|---|
| `bin/sdcc`, `bin/sdcpp`, `bin/sdas8051`, `bin/sdld`, `bin/packihx`, `bin/makebin` | [SDCC — Small Device C Compiler](https://sdcc.sourceforge.net/) | **GPL-2.0-or-later** |
| `share/sdcc/include/**`, `share/sdcc/lib/**` | SDCC runtime headers and libraries | **GPL-2.0-or-later with a linking exception** (see below) |

Binaries were taken unmodified from Debian's `sdcc` and `sdcc-libraries`
packages, version `4.0.0+dfsg-2`. `scripts/fetch-sdcc.sh` reproduces the
bundle exactly; `vendor/sdcc/VERSION` records the provenance and
`vendor/sdcc/copyright` carries Debian's assembled copyright file.

## What the GPL does and does not reach here

Three separate questions, which are easy to conflate:

**1. Does the wrapper become GPL?** No. `app.py` communicates with SDCC by
`fork`/`exec` with command-line arguments and files on disk. Nothing is linked;
nothing derives from SDCC's source. They are separate programs that happen to
ship together — what the GPL calls mere aggregation (GPLv2 §2, GPLv3 §5). The
wrapper stays MIT.

**2. Does the compiler output become GPL?** No. SDCC's runtime libraries and
headers — including the `mcs51/stc12.h` this service's callers rely on — carry
an explicit linking exception:

> As a special exception, if you link this library with other files, some of
> which are compiled with SDCC, to produce an executable, this library does not
> by itself cause the resulting executable to be covered by the GNU General
> Public License.

So a `.hex` compiled by this service belongs to whoever wrote the C.

**3. Does serving it over HTTP trigger anything?** No. GPLv2 and GPLv3 are
triggered by *distribution*, not by use over a network — that is the AGPL,
which SDCC is not under. Callers receive compiler *output*, never the compiler.

**3b. That answer flips the moment the compiler runs in the browser.** The
plan in the README's "toolchain in the browser" section ships SDCC compiled to
WebAssembly as a static asset, so the visitor receives **the compiler itself**,
not its output. That is distribution, plainly, and the reasoning in (3) stops
applying to it — (1) and (2) are unaffected, since the wrapper still does not
link SDCC and the linking exception still covers the `.hex`.

What compliance requires there, and it is not onerous:

- Publish the corresponding source for that exact build — the upstream tarball
  URL and its SHA-256, not just "SDCC 4.5.0".
- Publish **any patches** applied to make it build under Emscripten. Patches
  are part of the corresponding source; applying them without publishing them
  is the failure mode to avoid.
- Record the toolchain that produced it (the `emcc` version), so the build can
  be reproduced rather than merely inspected.
- Carry the licence text and notices alongside the artifact, as this repository
  already does for the vendored Linux binaries.

None of this restricts what anyone does with the `.hex` they compile: the
runtime-library linking exception in (2) is what governs the output, and it is
unchanged.

What **does** apply is that this repository redistributes GPL binaries, so it
carries the license text, preserves the notices, and identifies the exact
corresponding source. SDCC is unmodified upstream; the source for this precise
build is `apt-get source sdcc=4.0.0+dfsg-2`, and upstream releases are at
<https://sdcc.sourceforge.net/>. Written requests: open an issue on this
repository.

*None of the above is legal advice.*

## Related

STC MCU Limited publishes no open-source repository — only the datasheet PDF
and the Windows-only, proprietary STC-ISP.exe. Nearly all third-party STC12
code on GitHub carries no license at all. That is precisely why this service
uses SDCC's own properly-licensed `stc12.h` rather than a vendored vendor
header. See [`CrispStrobe/stc12c5a60s2-lab`](https://github.com/CrispStrobe/stc12c5a60s2-lab)
for the hardware side.
