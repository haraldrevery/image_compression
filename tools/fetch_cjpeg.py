#!/usr/bin/env python3
"""(Re)create the vendored MozJPEG ``cjpeg`` binaries in ``vendor/``.

Linux binaries are built from source: the only published Linux prebuild
(imagemin/mozjpeg-bin) is dynamically linked against a ``libjpeg.so.62`` it does
not ship, so it either fails to start or silently picks up the system
libjpeg-turbo — which is *not* MozJPEG and would produce different files.

Windows uses Mozilla's official ``cjpeg-static.exe`` from the v4.0.3 release.

Usage::

    python tools/fetch_cjpeg.py --check     # verify checksums of what's vendored
    python tools/fetch_cjpeg.py --windows   # re-download the Windows binary
    python tools/fetch_cjpeg.py --linux     # rebuild the Linux binary (needs cmake)
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# The expected digests live with the code that runs the binary, which verifies
# them before every session.  Importing them keeps one copy.
from minjpg.encoder import CHECKSUMS, sha256  # noqa: E402

MOZJPEG_VERSION = "4.0.3"
SOURCE_URL = f"https://github.com/mozilla/mozjpeg/archive/refs/tags/v{MOZJPEG_VERSION}.tar.gz"
WINDOWS_URL = (
    f"https://github.com/mozilla/mozjpeg/releases/download/v{MOZJPEG_VERSION}/"
    f"mozjpeg-v{MOZJPEG_VERSION}-win-x64.zip"
)
WINDOWS_MEMBER = "static/Release/cjpeg-static.exe"

VENDOR = Path(__file__).resolve().parent.parent / "vendor"


def check() -> int:
    status = 0
    for name, expected in CHECKSUMS.items():
        path = VENDOR / name
        if not path.is_file():
            print(f"MISSING  {name}")
            status = 1
            continue
        actual = sha256(path)
        if actual == expected:
            print(f"ok       {name}  {actual[:16]}…")
        else:
            print(f"MISMATCH {name}\n  expected {expected}\n  actual   {actual}")
            status = 1
    return status


def fetch_windows() -> None:
    print(f"Downloading {WINDOWS_URL}")
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / "mozjpeg-win.zip"
        urllib.request.urlretrieve(WINDOWS_URL, archive)
        target = VENDOR / "cjpeg-windows-x86_64.exe"
        with zipfile.ZipFile(archive) as zf, zf.open(WINDOWS_MEMBER) as src, target.open("wb") as dst:
            shutil.copyfileobj(src, dst)
    print(f"Wrote {target}  sha256={sha256(target)}")


def build_linux() -> None:
    """Build a fully static cjpeg.

    SIMD is disabled so no assembler is needed; libjpeg-turbo's SIMD paths are
    bit-exact with the C ones for the islow DCT used here, so this only costs
    encode speed, never output bytes.
    """
    cmake = shutil.which("cmake")
    if not cmake:
        sys.exit(
            "cmake not found. Install it with `pip install cmake` "
            "(or your package manager) and re-run."
        )

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        archive = tmp_path / "mozjpeg.tar.gz"
        print(f"Downloading {SOURCE_URL}")
        urllib.request.urlretrieve(SOURCE_URL, archive)
        with tarfile.open(archive) as tf:
            tf.extractall(tmp_path)
        source = tmp_path / f"mozjpeg-{MOZJPEG_VERSION}"
        build = tmp_path / "build"
        build.mkdir()

        subprocess.run(
            [
                cmake,
                "-DCMAKE_POLICY_VERSION_MINIMUM=3.5",  # mozjpeg still asks for cmake 2.8
                "-DCMAKE_BUILD_TYPE=Release",
                "-DENABLE_SHARED=0",
                "-DENABLE_STATIC=1",
                "-DWITH_SIMD=0",
                "-DWITH_TURBOJPEG=0",
                "-DPNG_SUPPORTED=0",
                "-DCMAKE_EXE_LINKER_FLAGS=-static",
                str(source),
            ],
            cwd=build,
            check=True,
        )
        subprocess.run(
            ["make", f"-j{os.cpu_count() or 2}", "cjpeg-static"], cwd=build, check=True
        )

        target = VENDOR / "cjpeg-linux-x86_64"
        shutil.copy2(build / "cjpeg-static", target)
        if shutil.which("strip"):
            subprocess.run(["strip", str(target)], check=False)
        target.chmod(0o755)
        shutil.copy2(source / "LICENSE.md", VENDOR / "LICENSE.mozjpeg.md")

    print(f"Wrote {target}  sha256={sha256(target)}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="verify vendored checksums")
    parser.add_argument("--linux", action="store_true", help="build the Linux binary")
    parser.add_argument("--windows", action="store_true", help="download the Windows binary")
    args = parser.parse_args()

    VENDOR.mkdir(parents=True, exist_ok=True)
    if args.check or not (args.linux or args.windows):
        return check()
    if args.linux:
        build_linux()
    if args.windows:
        fetch_windows()
    return 0


if __name__ == "__main__":
    sys.exit(main())
