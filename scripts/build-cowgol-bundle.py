#!/usr/bin/env python3
"""Build the pinned Cowgol source into a relocatable compiler-service bundle.

Debian/Ubuntu build dependencies: build-essential git python3 ninja-build
lua5.4 bison flex gpp moreutils nasm 64tass. libreadline-dev is only needed to build
the optional CP/M execution oracle. No dependency is installed by this script.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


REPOSITORY = "https://github.com/CrispStrobe/cowgol.git"
# Updated only after the corresponding fork checkpoint is published.
PINNED_COMMIT = "4082aaba5c023b17f469f74d7a84504e3dbbebd4"
ROOT = Path(__file__).resolve().parents[1]
TARGETS = ("cowgol-cgen", "z80-cpm", "i8080-cpm", "dos", "riscv32", "eater6502", "atmega328p")
NATIVE = {"z80-cpm": ("cpmz", "z80", "ncpmz"),
          "i8080-cpm": ("cpm", "8080", "ncpm"), "dos": ("msdos", "8086", "msdos"),
          "eater6502": ("eater6502", "65c02-rom", "eater6502")}


def run(args, cwd=None):
    subprocess.run([str(item) for item in args], cwd=cwd, check=True)


def git(source, *args):
    return subprocess.check_output(["git", "-C", str(source), *args], text=True).strip()


def build(source, destination, revision, oracles, requested=TARGETS):
    if git(source, "rev-parse", "HEAD") != revision:
        raise ValueError("source checkout HEAD must equal the requested pinned revision")
    if git(source, "status", "--porcelain", "--untracked-files=no"):
        raise ValueError("source checkout has tracked modifications; commit them before bundling")
    native = [NATIVE[name] for name in requested if name in NATIVE]
    cpms = any(family in ("cpm", "cpmz") for family, _backend, _linker in native)
    dos = any(family == "msdos" for family, _backend, _linker in native)
    eater = any(family == "eater6502" for family, _backend, _linker in native)
    frontends = sorted({"6502" if family == "eater6502" else "16bit" for family, _backend, _linker in native})
    targets = [f"bin/cowfe-for-{arch}-with-nncgen" for arch in frontends]
    for family, backend, linker in native:
        targets += [f"bin/cowbe-for-{backend}-with-ncgen", f"bin/cowlink-for-{linker}-with-ncgen",
                    f"rt/{family}/+cowgolcoo"]
    if cpms:
        targets.append("bin/zmac")
    if dos:
        targets.append("bin/djlink")
    if oracles:
        targets += (["bin/cpmemu"] if cpms else []) + (["bin/emu2"] if dos else [])
    dependencies = ["cc"]
    if native:
        dependencies += ["make", "ninja", "c++", "lua", "bison", "flex", "gpp", "chronic"]
    if dos:
        dependencies.append("nasm")
    if eater:
        dependencies.append("64tass")
    for name in dependencies:
        if not shutil.which(name):
            raise ValueError(f"missing build dependency: {name}; see this script's dependency list")
    run([sys.executable, source / "scripts/compile.py", "--bootstrap-only"], cwd=source)
    # One multi-target invocation reuses the front end / compiler bootstrap.
    # DJLINK is C++; the serverless host need not have libstdc++ or libgcc_s.
    # Keep only the system libc dependency in the shipped executables.
    linker_flags = "-g -static-libstdc++ -static-libgcc"
    if targets:
        run(["make", f"LDFLAGS={linker_flags}", *targets], cwd=source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cowgol-bundle-", dir=destination.parent) as temporary:
        bundle = Path(temporary) / "bundle"
        bundle.mkdir()
        shutil.copytree(source / "rt", bundle / "rt", ignore=shutil.ignore_patterns("__pycache__", "build.py"))
        (bundle / "bin").mkdir()
        (bundle / "licenses").mkdir()
        shutil.copy2(source / "COPYING", bundle / "licenses/Cowgol-BSD-2-Clause.txt")
        if cpms:
            shutil.copy2(source / "third_party/zmac/COPYING.publicdomain", bundle / "licenses/zmac-public-domain.txt")
        if dos:
            shutil.copy2(source / "third_party/djlink/copying", bundle / "licenses/djlink-GPL.txt")
            shutil.copy2(source / "third_party/djlink/copying.dj", bundle / "licenses/djlink-exception.txt")
            gcc_major = subprocess.check_output(["cc", "-dumpversion"], text=True).strip().split(".")[0]
            gcc_notice = Path(os.environ.get("GCC_RUNTIME_LICENSE", f"/usr/share/doc/gcc-{gcc_major}-base/copyright"))
            gpl_notice = Path(os.environ.get("GCC_GPL_LICENSE", "/usr/share/common-licenses/GPL-3"))
            if not gcc_notice.is_file() or not gpl_notice.is_file():
                raise ValueError("set GCC_RUNTIME_LICENSE and GCC_GPL_LICENSE to the installed GCC runtime notices")
            shutil.copy2(gcc_notice, bundle / "licenses/GCC-runtime-copyright.txt")
            shutil.copy2(gpl_notice, bundle / "licenses/GCC-GPL-3.txt")
            subprocess.run(["nasm", "-v"], check=True, capture_output=True)
            # NASM is BSD-2-Clause; retain its installed package notice.
            nasm_notice = os.environ.get("NASM_LICENSE")
            notice = Path(nasm_notice) if nasm_notice else Path("/usr/share/doc/nasm/copyright")
            if not notice.is_file():
                raise ValueError("set NASM_LICENSE to the installed NASM copyright/licence file")
            shutil.copy2(notice, bundle / "licenses/nasm.txt")
        if eater:
            tass_notice = Path(os.environ.get("TASS64_LICENSE", "/usr/share/doc/64tass/copyright"))
            if not tass_notice.is_file():
                raise ValueError("set TASS64_LICENSE to the installed 64tass copyright/licence file")
            shutil.copy2(tass_notice, bundle / "licenses/64tass-copyright.txt")
            for license_name in ("GPL-2", "LGPL-2", "GPL-3"):
                license_file = Path(os.environ.get("TASS64_LICENSE_DIR", "/usr/share/common-licenses")) / license_name
                if not license_file.is_file():
                    raise ValueError("set TASS64_LICENSE_DIR to a directory containing GPL-2, LGPL-2 and GPL-3 licence texts")
                shutil.copy2(license_file, bundle / f"licenses/64tass-{license_name}.txt")
        for tool in ("cowfe", "cowbe", "cowlink"):
            shutil.copy2(source / f".obj/brickwright/bootstrap/{tool}", bundle / f"bin/{tool}-cgen")
        for family, backend, linker in native:
            frontend = "6502" if family == "eater6502" else "16bit"
            shutil.copy2(source / f"bin/cowfe-for-{frontend}-with-nncgen", bundle / f"bin/cowfe-{family}")
            shutil.copy2(source / f"bin/cowbe-for-{backend}-with-ncgen", bundle / f"bin/cowbe-{family}")
            shutil.copy2(source / f"bin/cowlink-for-{linker}-with-ncgen", bundle / f"bin/cowlink-{family}")
            shutil.copy2(source / f".obj/rt/{family}/+cowgolcoo/cowgol.coo", bundle / f"rt/{family}/cowgol.coo")
        for tool in (["zmac"] if cpms else []) + (["djlink"] if dos else []):
            shutil.copy2(source / f"bin/{tool}", bundle / f"bin/{tool}")
        if dos:
            shutil.copy2(shutil.which("nasm"), bundle / "bin/nasm")
        if eater:
            shutil.copy2(shutil.which("64tass"), bundle / "bin/64tass")
        if oracles:
            # Test programs stay outside the deployed bundle (cpmemu includes
            # GPL z80ex); they are execution witnesses, not service dependencies.
            print(f"Execution oracles available at {source / 'bin'}")
        for path in (bundle / "bin").iterdir():
            path.chmod(0o755)
        manifest = {
            "schema": 1, "source_repository": REPOSITORY,
            "source_commit": revision,
            "targets": list(dict.fromkeys(requested)),
            "nasm_version": subprocess.check_output(["nasm", "-v"], text=True).strip() if dos else None,
            "tass64_version": subprocess.check_output(["64tass", "--version"], text=True).splitlines()[0] if eater else None,
            "host_compiler": subprocess.check_output(["cc", "--version"], text=True).splitlines()[0],
            "linker_flags": linker_flags,
            "sha256": {str(path.relative_to(bundle)): hashlib.sha256(path.read_bytes()).hexdigest()
                       for path in sorted(bundle.rglob("*")) if path.is_file()},
        }
        (bundle / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        if destination.exists():
            raise ValueError(f"destination already exists: {destination}; choose a fresh --output directory")
        bundle.rename(destination)
    print(f"Built {destination}; Cowgol {revision}; {len(manifest['sha256'])} hashed files")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, help="existing clean checkout at the pinned revision")
    parser.add_argument("--revision", default=PINNED_COMMIT, help="exact 40-character Cowgol commit")
    parser.add_argument("--output", type=Path, default=ROOT / "vendor/cowgol")
    parser.add_argument("--oracles", action="store_true", help="also build cpmemu and emu2 for tests")
    parser.add_argument("--targets", nargs="+", choices=TARGETS, default=TARGETS,
                        help="installed targets (default: all); cgen/RV32 need only Python and cc")
    args = parser.parse_args()
    if args.oracles and not args.source:
        parser.error("--oracles requires --source so the emulator executables remain available for tests")
    try:
        if not re.fullmatch("[0-9a-f]{40}", args.revision):
            raise ValueError("--revision must be an exact 40-character Git commit")
        if args.source:
            build(args.source.resolve(), args.output.resolve(), args.revision, args.oracles, args.targets)
        else:
            with tempfile.TemporaryDirectory(prefix="cowgol-source-") as temporary:
                source = Path(temporary) / "cowgol"
                run(["git", "init", source])
                run(["git", "remote", "add", "origin", REPOSITORY], cwd=source)
                run(["git", "fetch", "--depth", "1", "origin", args.revision], cwd=source)
                run(["git", "checkout", "--detach", "FETCH_HEAD"], cwd=source)
                build(source, args.output.resolve(), args.revision, args.oracles, args.targets)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"cowgol bundle: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
