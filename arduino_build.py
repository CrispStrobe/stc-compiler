"""Arduino sketches, compiled as the Arduino IDE compiles them.

`language: "arduino"` takes a sketch -- `setup()`, `loop()`, and whatever C++
the Arduino API invites: `Serial`, `String`, classes, templates, the bundled
libraries -- and builds it against the real core source, vendored in
arduino-core/ by scripts/fetch-arduino-core.sh:

    ArduinoCore-avr 1.8.8   Uno, Nano, Mega   (ATmega328P, ATmega168P, ATmega2560)
    ATTinyCore (2.0 line)   ATtiny85, ATtiny88

The pipeline is the IDE's, step for step, because a sketch that builds there
should build here and a sketch that fails there should fail here:

1. **Sketch preprocessing** (`prepare_sketch`). The `.ino` dialect is C++ plus
   two conveniences: `Arduino.h` is included for you, and functions may be
   called before they are defined, because the builder writes their
   prototypes. Both are done here, with `#line` directives so that every
   diagnostic names the line of the sketch the user wrote, not of the file the
   compiler saw.
2. **The core**, compiled with the flags the core's own platform.txt uses --
   including `-flto`, which ATTinyCore cannot build without (see
   fetch-avr-gcc.sh). The objects are cached in /tmp per (core, variant, MCU,
   clock, defines), so a warm function instance compiles only the sketch.
3. **Libraries**: an `#include <Wire.h>` pulls in the core's bundled Wire, as
   the IDE's library discovery does -- transitively, since a library may
   include another.
4. **Link** with LTO and section GC, then objcopy to Intel HEX.

What is deliberately not here: any library that is not bundled with the core
(there is no library manager), and `-std` above what gcc-avr 5.4 knows. Both
fail with the compiler's own message plus a sentence naming the cause.
"""

from __future__ import annotations

import base64
import concurrent.futures
import hashlib
import os
import re
import shutil
import subprocess
import tempfile
import uuid

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CORE_ROOT = os.path.join(BASE_DIR, "arduino-core")
CACHE_ROOT = os.path.join(tempfile.gettempdir(), "arduino-core-cache")

# What `ARDUINO` means to a sketch: the IDE version it claims to be built by.
# 1.8.19, the last 1.x IDE, is what both cores' feature checks expect.
ARDUINO_VERSION = "10819"

# Bump when the flags below change: cached core objects built with the old
# flags must not be linked against a sketch built with the new ones.
FLAGS_VERSION = "1"

# One entry per `target` the route accepts. Chip names resolve to the board
# the rest of this service already means by them (atmega328p is the Uno's
# chip); the board names are accepted too, because the Nano's variant is not
# the Uno's -- it brings out A6 and A7.
BOARDS = {
    "atmega328p": {
        "mcu": "atmega328p", "core": "arduino", "variant": "standard",
        "board": "AVR_UNO", "default_clock": 16000000, "flash": 32768,
        "description": "ATmega328P — Arduino Uno pinout, ArduinoCore-avr",
    },
    "arduino-uno": {
        "mcu": "atmega328p", "core": "arduino", "variant": "standard",
        "board": "AVR_UNO", "default_clock": 16000000, "flash": 32768,
        "description": "Arduino Uno — ATmega328P, ArduinoCore-avr",
    },
    "arduino-nano": {
        "mcu": "atmega328p", "core": "arduino", "variant": "eightanaloginputs",
        "board": "AVR_NANO", "default_clock": 16000000, "flash": 32768,
        "description": "Arduino Nano — ATmega328P with A6/A7, ArduinoCore-avr",
    },
    "atmega168p": {
        "mcu": "atmega168p", "core": "arduino", "variant": "standard",
        "board": "AVR_DIECIMILA", "default_clock": 16000000, "flash": 16384,
        "description": "ATmega168P — Uno pinout, 16 KB flash, ArduinoCore-avr",
    },
    "atmega2560": {
        "mcu": "atmega2560", "core": "arduino", "variant": "mega",
        "board": "AVR_MEGA2560", "default_clock": 16000000, "flash": 262144,
        "description": "ATmega2560 — Arduino Mega pinout, ArduinoCore-avr",
    },
    "arduino-mega": {
        "mcu": "atmega2560", "core": "arduino", "variant": "mega",
        "board": "AVR_MEGA2560", "default_clock": 16000000, "flash": 262144,
        "description": "Arduino Mega 2560 — ArduinoCore-avr",
    },
    "attiny85": {
        "mcu": "attiny85", "core": "tiny", "variant": "tinyx5",
        "board": "AVR_ATTINYX5", "default_clock": 8000000, "flash": 8192,
        "description": "ATtiny85 — ATTinyCore, 8 KB flash",
    },
    "attiny88": {
        "mcu": "attiny88", "core": "tiny", "variant": "tinyx8",
        "board": "AVR_ATTINYX8", "default_clock": 8000000, "flash": 8192,
        "description": "ATtiny88 — ATTinyCore, 8 KB flash",
    },
}

# Per core: the language standard its platform.txt asks for, as far as
# gcc-avr 5.4 can honour it (ATTinyCore asks for gnu++17; 5.4 calls that
# gnu++1z), and the defines its build recipe adds.
CORE_FLAGS = {
    "arduino": {"cxx_std": "gnu++11", "defines": []},
    # CLOCK_SOURCE 0 is ATTinyCore's "internal oscillator", the fuse state a
    # factory-fresh ATtiny ships in.
    "tiny": {"cxx_std": "gnu++1z", "defines": ["-DCLOCK_SOURCE=0"]},
}

COMMON_FLAGS = ["-Os", "-g", "-flto", "-fno-fat-lto-objects",
                "-ffunction-sections", "-fdata-sections"]
C_FLAGS = ["-std=gnu11"]
# DECIMAL_DIG: ArduinoCore-avr 1.8.8's WString.cpp sizes its float buffers
# with it, and gcc 5.4's <float.h> only defines it for C99, not for C++.
# __DECIMAL_DIG__ is the compiler's own value -- the one <float.h> would use.
CXX_FLAGS = ["-fpermissive", "-fno-exceptions", "-fno-threadsafe-statics",
             "-Wno-error=narrowing", "-DDECIMAL_DIG=__DECIMAL_DIG__"]
ASM_FLAGS = ["-x", "assembler-with-cpp"]

SOURCE_EXTS = (".c", ".cpp", ".S")


class ArduinoBuildError(Exception):
    """A failed step, with what the tools said. `stage` is compile or link."""

    def __init__(self, message: str, log: str = "", stage: str = "compile"):
        super().__init__(message)
        self.log = log
        self.stage = stage


# ------------------------------------------------------------ preprocessing

_CONTROL = {"if", "while", "for", "switch", "return", "sizeof", "catch", "else",
            "do", "case", "new", "delete", "throw", "defined"}
_TYPE_DEF_RE = re.compile(
    r"\b(?:class|struct|union|enum(?:\s+class)?)\s+([A-Za-z_]\w*)"
    r"|\btypedef\b[^;{}]*?\b([A-Za-z_]\w*)\s*;"
    r"|\busing\s+([A-Za-z_]\w*)\s*=")
_HEADER_RE = re.compile(
    r"^(?P<ret>(?:[A-Za-z_][\w:]*(?:\s*<[^;{}()]*>)?[\s*&]+)+?)"
    r"(?P<name>[A-Za-z_]\w*)\s*"
    r"\((?P<args>[^;{}]*)\)\s*(?P<quals>(?:const\s*)?)$", re.S)


def _mask(code: str) -> str:
    """The sketch with comments, string/char literals and preprocessor lines
    blanked to spaces -- same length, same newlines -- so that braces and
    parentheses can be counted without being fooled by `"{"` or `// )`."""
    out = list(code)
    i, n = 0, len(code)
    line_start = True
    while i < n:
        c = code[i]
        if line_start and c == "#":
            # A directive runs to an unescaped newline.
            j = i
            while j < n and not (code[j] == "\n" and code[j - 1] != "\\"):
                j += 1
            for k in range(i, j):
                if out[k] != "\n":
                    out[k] = " "
            i = j
            continue
        if c == "/" and i + 1 < n and code[i + 1] == "/":
            j = code.find("\n", i)
            j = n if j < 0 else j
            for k in range(i, j):
                out[k] = " "
            i = j
            continue
        if c == "/" and i + 1 < n and code[i + 1] == "*":
            j = code.find("*/", i + 2)
            j = n if j < 0 else j + 2
            for k in range(i, j):
                if out[k] != "\n":
                    out[k] = " "
            i = j
            continue
        if c in "\"'":
            j = i + 1
            while j < n and code[j] != c:
                j += 2 if code[j] == "\\" else 1
            j = min(j + 1, n)
            for k in range(i + 1, j - 1):
                if out[k] != "\n":
                    out[k] = " "
            i = j
            line_start = False
            continue
        if c == "\n":
            line_start = True
        elif not c.isspace():
            line_start = False
        i += 1
    return "".join(out)


def find_function_definitions(code: str) -> list[dict]:
    """Every free function DEFINED at file scope, in order: its name, the
    prototype to declare it with, the offset its header starts at, and the
    text of its signature. Methods defined out of line (`Foo::bar`), and
    anything inside a class, namespace or `extern "C"` block, are not free
    functions at file scope and are left alone -- as the IDE leaves them."""
    masked = _mask(code)
    found = []
    depth = 0
    stmt_start = 0          # where the current top-level statement began
    paren = 0
    for i, c in enumerate(masked):
        if c == "(":
            paren += 1
        elif c == ")":
            paren -= 1
        elif c == "{":
            if depth == 0 and paren == 0:
                header = masked[stmt_start:i].strip()
                fn = _parse_header(header)
                if fn:
                    start = stmt_start + (len(masked[stmt_start:i])
                                          - len(masked[stmt_start:i].lstrip()))
                    fn["start"] = start
                    found.append(fn)
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                stmt_start = i + 1
        elif c == ";" and depth == 0 and paren == 0:
            stmt_start = i + 1
    return found


def _parse_header(header: str) -> dict | None:
    header = " ".join(header.split())
    if not header or "=" in header.split("(")[0]:
        return None
    # A template's prototype needs its template head, and a wrong one is a
    # hard error the user did not write; the IDE's own generator gets these
    # wrong often enough that declaring them by hand is the known idiom.
    if header.startswith("template") or header.startswith("extern"):
        return None
    m = _HEADER_RE.match(header)
    if not m:
        return None
    name = m.group("name")
    ret = m.group("ret").strip()
    ret_words = set(re.findall(r"[A-Za-z_]\w*", ret))
    if name in _CONTROL or ret_words & {"class", "struct", "union", "enum",
                                        "namespace", "operator", "return",
                                        "typedef", "using"}:
        return None
    if "::" in name or ret.endswith("::"):
        return None
    args = m.group("args")
    # A default argument may be given once. Repeating it in a prototype makes
    # the definition an error, so these are left for the user to order.
    if "=" in args:
        return None
    # The signature exactly as written (whitespace normalised), so the
    # prototype declares what the definition defines and nothing else.
    signature = header
    return {"name": name, "prototype": signature + ";", "signature": signature}


def prepare_sketch(code: str, filename: str = "sketch.ino") -> tuple[str, list[str]]:
    """The translation unit the compiler sees, and the prototypes it gained.

    `Arduino.h` first, as the IDE does. Then the sketch up to its first
    function definition, then the prototypes, then the rest -- each part
    behind a `#line` so that a diagnostic in any of them names the sketch's
    own line number.

    A prototype whose signature names a type the sketch defines only LATER
    than the insertion point would be an error the user did not write, so it
    is left out; that function must then be defined before it is called,
    which is the IDE's behaviour for the same sketch.
    """
    defs = find_function_definitions(code)
    q = filename.replace("\\", "\\\\").replace('"', '\\"')
    head = f'#include <Arduino.h>\n#line 1 "{q}"\n'
    if not defs:
        return head + code, []

    insert_at = code.rfind("\n", 0, defs[0]["start"]) + 1
    masked = _mask(code)
    late_types = set()
    for m in _TYPE_DEF_RE.finditer(masked):
        name = m.group(1) or m.group(2) or m.group(3)
        if m.start() >= insert_at:
            late_types.add(name)

    protos = []
    for fn in defs:
        words = set(re.findall(r"[A-Za-z_]\w*", fn["signature"]))
        if words & late_types:
            continue
        if fn["prototype"] not in protos:
            protos.append(fn["prototype"])

    line_no = code.count("\n", 0, insert_at) + 1
    body = (code[:insert_at]
            + ("" if not insert_at or code[insert_at - 1] == "\n" else "\n")
            + "\n".join(protos) + "\n"
            + f'#line {line_no} "{q}"\n'
            + code[insert_at:])
    return head + body, protos


def included_headers(code: str) -> list[str]:
    """Every `#include <x.h>` / `"x.h"` target, in order, without duplicates."""
    seen = []
    for m in re.finditer(r'^\s*#\s*include\s*[<"]([^>"]+)[>"]', code, re.M):
        if m.group(1) not in seen:
            seen.append(m.group(1))
    return seen


# ------------------------------------------------------------------ building

def core_dirs(spec: dict) -> tuple[str, str, str]:
    core = os.path.join(CORE_ROOT, "cores", spec["core"])
    variant = os.path.join(CORE_ROOT, "variants", spec["variant"])
    libs = os.path.join(CORE_ROOT, "libraries", spec["core"])
    return core, variant, libs


def available_libraries(spec: dict) -> dict[str, str]:
    """{header: library src dir} for the libraries bundled with this core.
    Keyed by the top-level headers in each library's src/ -- what an
    `#include` names -- as the IDE's library discovery keys them."""
    _, _, libs = core_dirs(spec)
    out = {}
    if not os.path.isdir(libs):
        return out
    for lib in sorted(os.listdir(libs)):
        src = os.path.join(libs, lib, "src")
        if not os.path.isdir(src):
            continue
        for f in sorted(os.listdir(src)):
            if f.endswith(".h"):
                out.setdefault(f, src)
    return out


def _sources(root: str, recursive: bool) -> list[str]:
    found = []
    for dirpath, dirnames, files in os.walk(root):
        dirnames.sort()
        for f in sorted(files):
            if f.endswith(SOURCE_EXTS):
                found.append(os.path.join(dirpath, f))
        if not recursive:
            break
    return found


def resolve_libraries(sketch: str, spec: dict) -> list[str]:
    """The library src dirs the sketch needs, transitively: a header a
    library's own sources include can pull in a further library."""
    avail = available_libraries(spec)
    wanted: list[str] = []
    queue = [h.split("/")[-1] for h in included_headers(sketch)]
    while queue:
        header = queue.pop(0)
        src = avail.get(header)
        if not src or src in wanted:
            continue
        wanted.append(src)
        for path in _sources(src, True) + [
                os.path.join(src, f) for f in os.listdir(src) if f.endswith(".h")]:
            with open(path, encoding="utf-8", errors="replace") as fh:
                queue.extend(h.split("/")[-1] for h in included_headers(fh.read()))
    return wanted


def _flags_for(path: str, spec: dict, f_cpu: int, includes: list[str],
               defines: list[str], warnings: list[str]) -> list[str]:
    cf = CORE_FLAGS[spec["core"]]
    base = [f"-mmcu={spec['mcu']}", *COMMON_FLAGS,
            f"-DF_CPU={int(f_cpu)}L", f"-DARDUINO={ARDUINO_VERSION}",
            f"-DARDUINO_{spec['board']}", "-DARDUINO_ARCH_AVR",
            *cf["defines"], *defines, *warnings,
            *[f"-I{d}" for d in includes]]
    if path.endswith(".cpp"):
        return base + [f"-std={cf['cxx_std']}", *CXX_FLAGS, "-x", "c++"]
    if path.endswith(".S"):
        return base + ASM_FLAGS
    return base + C_FLAGS


def _compile_one(gcc: str, src: str, obj: str, flags: list[str], env: dict,
                 timeout: int) -> tuple[int, str]:
    try:
        r = subprocess.run([gcc, *flags, "-c", src, "-o", obj],
                           capture_output=True, text=True, timeout=timeout,
                           env=env)
    except subprocess.TimeoutExpired:
        return 1, f"{os.path.basename(src)}: compile timed out after {timeout}s"
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def _compile_many(gcc: str, jobs: list[tuple[str, str, list[str]]], env: dict,
                  timeout: int) -> tuple[bool, str]:
    """Compile (src, obj, flags) jobs in parallel. Returns (ok, log)."""
    workers = max(1, min(len(jobs), os.cpu_count() or 2))
    logs = []
    ok = True
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(_compile_one, gcc, s, o, f, env, timeout)
                   for s, o, f in jobs]
        for fut in futures:
            rc, log = fut.result()
            if log.strip():
                logs.append(log)
            if rc != 0:
                ok = False
    return ok, "\n".join(logs)


def _cached_objects(gcc: str, name: str, sources: list[str], spec: dict,
                    f_cpu: int, includes: list[str], defines: list[str],
                    env: dict, timeout: int) -> list[str]:
    """Objects for a fixed source set (the core, or one library), built once
    per configuration and reused by every later request on this instance.

    Built into a private directory and renamed into place, so two concurrent
    requests never link a half-written object: the loser of the rename race
    simply uses the winner's identical build."""
    with open(os.path.join(CORE_ROOT, "VERSION"), "rb") as fh:
        core_version = fh.read()
    key = hashlib.sha256(repr((
        FLAGS_VERSION, core_version, name, spec["mcu"], spec["core"],
        spec["variant"], spec["board"], int(f_cpu), sorted(defines),
        gcc)).encode()).hexdigest()[:24]
    final = os.path.join(CACHE_ROOT, key)
    done = os.path.join(final, ".complete")
    if os.path.exists(done):
        return sorted(os.path.join(final, f) for f in os.listdir(final)
                      if f.endswith(".o"))

    os.makedirs(CACHE_ROOT, exist_ok=True)
    tmp = os.path.join(CACHE_ROOT, f".{key}-{uuid.uuid4().hex}")
    os.makedirs(tmp)
    jobs = []
    for i, src in enumerate(sources):
        obj = os.path.join(tmp, f"{i:03d}-{os.path.basename(src)}.o")
        jobs.append((src, obj, _flags_for(src, spec, f_cpu, includes, defines, ["-w"])))
    ok, log = _compile_many(gcc, jobs, env, timeout)
    if not ok:
        shutil.rmtree(tmp, ignore_errors=True)
        raise ArduinoBuildError(f"the {name} did not compile for {spec['mcu']}",
                                log=log)
    open(os.path.join(tmp, ".complete"), "w").close()
    try:
        os.rename(tmp, final)
    except OSError:
        shutil.rmtree(tmp, ignore_errors=True)   # another request won
    return sorted(os.path.join(final, f) for f in os.listdir(final)
                  if f.endswith(".o"))


_TOOL_PATH_RE = re.compile(
    r"[^\s:'`]*/(ld|as|collect2|cc1plus|cc1|lto1|lto-wrapper|avr-gcc)(?=:)")


def _clean(log: str, work: str) -> str:
    """Tool output with the server's paths taken out: the sketch is named by
    its own filename, the core and libraries by where they live in the core,
    and a tool by its name (`ld:`, not `/tmp/avr/bin/../lib/.../ld:`)."""
    log = log.replace(work + os.sep, "")
    log = log.replace(CORE_ROOT + os.sep, "arduino-core/")
    return _TOOL_PATH_RE.sub(lambda m: m.group(1), log)


def build(code: str, spec: dict, *, bin_dir: str, env: dict,
          f_cpu: int | None = None, defines: dict | None = None,
          fmt: str = "hex", stem: str = "sketch", timeout: int = 25,
          disassemble: bool = False) -> dict:
    """Build a sketch into an image. Raises ArduinoBuildError on a failure the
    user can act on; returns the service's usual success shape otherwise."""
    gcc = os.path.join(bin_dir, "avr-gcc")
    core_dir, variant_dir, _ = core_dirs(spec)
    if not os.path.isdir(core_dir) or not os.path.isdir(variant_dir):
        raise ArduinoBuildError(
            f"the {spec['core']} core is not in this deployment "
            "(arduino-core/ is missing or incomplete)")
    f_cpu = int(f_cpu or spec["default_clock"])

    dflags = []
    for name, value in (defines or {}).items():
        if not re.fullmatch(r"[A-Za-z_]\w*", name or ""):
            raise ArduinoBuildError(f"bad define name: {name!r}")
        if value is not None and re.search(r"[\s\"'\\]", str(value)):
            raise ArduinoBuildError(f"bad define value for {name}: {value!r}")
        dflags.append(f"-D{name}" if value is None else f"-D{name}={value}")

    filename = f"{stem}.ino"
    source, prototypes = prepare_sketch(code, filename)
    libs = resolve_libraries(code, spec)
    includes = [core_dir, variant_dir, *libs]

    work = os.path.join(tempfile.gettempdir(), f"sketch-{uuid.uuid4().hex}")
    os.makedirs(work)
    try:
        # The core's own main() is left out when the sketch brings one: that
        # is how the IDE lets a sketch take over main, via the archive, and a
        # plain object list has to make the same choice explicitly.
        defines_main = any(fn["name"] == "main" for fn in find_function_definitions(code))
        core_sources = [s for s in _sources(core_dir, False)
                        if not (defines_main and os.path.basename(s) == "main.cpp")]
        objects = _cached_objects(
            gcc, "core" + ("-nomain" if defines_main else ""), core_sources,
            spec, f_cpu, [core_dir, variant_dir], dflags, env, timeout)
        for lib in libs:
            objects += _cached_objects(
                gcc, "library " + os.path.basename(os.path.dirname(lib)),
                _sources(lib, True), spec, f_cpu, includes, dflags, env, timeout)

        # The sketch keeps the user's name in diagnostics: `#line` names it,
        # and the file itself is written under that name too.
        sketch_src = os.path.join(work, f"{stem}.ino.cpp")
        with open(sketch_src, "w", encoding="utf-8") as fh:
            fh.write(source)
        sketch_obj = os.path.join(work, f"{stem}.ino.o")
        rc, log = _compile_one(
            gcc, sketch_src, sketch_obj,
            _flags_for(sketch_src, spec, f_cpu, includes, dflags, ["-Wall"]),
            env, timeout)
        log = _clean(log, work)
        if rc != 0:
            missing = re.search(r"fatal error: ([^:\s]+): No such file", log)
            if missing:
                have = sorted({os.path.basename(os.path.dirname(p))
                               for p in available_libraries(spec).values()})
                log += (f"\n{missing.group(1)} is not part of the {spec['core']} "
                        f"core or its bundled libraries ({', '.join(have)}); "
                        "other libraries are not available on this service.")
            raise ArduinoBuildError(log.strip() or "sketch compilation failed", log)

        elf = os.path.join(work, f"{stem}.elf")
        try:
            r = subprocess.run(
                [gcc, f"-mmcu={spec['mcu']}", "-Os", "-g", "-flto",
                 "-fuse-linker-plugin", "-Wl,--gc-sections", "-w",
                 "-o", elf, sketch_obj, *objects, "-lm"],
                capture_output=True, text=True, timeout=timeout, env=env)
        except subprocess.TimeoutExpired:
            raise ArduinoBuildError(f"link timed out after {timeout}s", stage="link")
        link_log = _clean((r.stdout or "") + (r.stderr or ""), work)
        if r.returncode != 0 or not os.path.exists(elf):
            if "undefined reference to `setup'" in link_log or \
               "undefined reference to `loop'" in link_log:
                link_log += ("\nA sketch needs both setup() and loop(), even "
                             "when loop() is empty.")
            over = re.search(r"region `text' overflowed by (\d+) bytes", link_log)
            if over:
                link_log += (f"\nThe sketch is {over.group(1)} bytes too big for "
                             f"the {spec['mcu']}, which has {spec['flash']} bytes "
                             "of flash.")
            raise ArduinoBuildError(link_log.strip() or "link failed",
                                    log + link_log, stage="link")
        log = (log + link_log).strip()

        objcopy = os.path.join(bin_dir, "avr-objcopy")
        if fmt == "bin":
            out, name, args = os.path.join(work, f"{stem}.bin"), f"{stem}.bin", ["-O", "binary"]
        else:
            out, name, args = os.path.join(work, f"{stem}.hex"), f"{stem}.hex", ["-O", "ihex"]
        subprocess.run([objcopy, *args, "-R", ".eeprom", elf, out],
                       capture_output=True, timeout=10, env=env)
        if not os.path.exists(out):
            raise ArduinoBuildError("avr-objcopy produced no image", log, stage="link")
        with open(out, "rb") as fh:
            blob = fh.read()

        mem = ""
        try:
            sized = subprocess.run(
                [os.path.join(bin_dir, "avr-size"), f"--mcu={spec['mcu']}",
                 "--format=avr", elf],
                capture_output=True, text=True, timeout=10, env=env)
            mem = sized.stdout or ""
        except (OSError, subprocess.SubprocessError):
            pass
        m = re.search(r"Program:\s+(\d+) bytes", mem)
        if m and int(m.group(1)) > spec["flash"]:
            raise ArduinoBuildError(
                f"the sketch is {m.group(1)} bytes and the {spec['mcu']} has "
                f"{spec['flash']} bytes of flash", mem, stage="link")

        listing = listing_artifact = None
        if disassemble:
            import listing as listing_mod
            listing_artifact = listing_mod.from_objdump(bin_dir, "avr", elf, env, "avr-gcc")
            listing = listing_artifact.get("asm") or \
                f"(disassembly failed: {listing_artifact.get('error', 'unknown')})"

        return {
            "success": True,
            "c": None,
            "translated": None,
            "unresolved": None,
            "warnings": None,
            "disassembly": listing,
            "listing": listing_artifact,
            "base64": base64.b64encode(blob).decode("ascii"),
            "filename": name,
            "bytes": len(blob),
            "log": log,
            "memory": mem,
            "symbols": None,
            "symbols_error": None,
            "toolchain": ("avr-gcc+ArduinoCore-avr" if spec["core"] == "arduino"
                          else "avr-gcc+ATTinyCore"),
            "mcu": spec["mcu"],
            "board": spec["board"],
            "variant": spec["variant"],
            "f_cpu": f_cpu,
            "prototypes": prototypes,
            "libraries": [os.path.basename(os.path.dirname(p)) for p in libs],
        }
    finally:
        shutil.rmtree(work, ignore_errors=True)
