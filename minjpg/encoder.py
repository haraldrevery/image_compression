"""JPEG encoding through the bundled MozJPEG ``cjpeg``.

The flags below are the command-line equivalent of the Squoosh MozJPEG settings
this app replaces:

===========================  ==========================================
Squoosh option               cjpeg flag
===========================  ==========================================
Channels: YCbCr              default for RGB input
Quantization: ImageMagick    ``-quant-table 3``
Smoothing: 30                ``-smooth 30``
Auto subsample chroma        *no* ``-sample`` flag; MozJPEG decides
Progressive / optimize       ``-progressive -optimize`` (also defaults)
===========================  ==========================================

Input is handed over as a PPM file, which carries no EXIF or ICC data — so the
output has none either, exactly like the existing ``_min.jpg`` files.
"""

from __future__ import annotations

import hashlib
import os
import platform
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

#: sha256 of the binaries committed under ``vendor/``.  Checked before the
#: binary is ever executed, and by ``tools/fetch_cjpeg.py --check``, which
#: imports this dict so there is only one copy of the expected digests.
CHECKSUMS = {
    "cjpeg-linux-x86_64": "585d270cbfd20d16e74851c412d165e09264420f0a391733365a60b6f19a1ab1",
    "cjpeg-windows-x86_64.exe": "dac0b9d64f660a150ef03a6137836592528c30fc3fd5e39511c2cff7a8206ae1",
}

#: Seconds before a wedged cjpeg is killed.  Encoding a 4K frame takes well
#: under a second even without SIMD; anything near these numbers is a hang, and
#: without a limit it would block the worker thread with no way to cancel.
ENCODE_TIMEOUT = 120
VERSION_TIMEOUT = 10


class EncoderError(RuntimeError):
    """Raised when the cjpeg binary is missing, unrecognised, or fails to encode."""


def _vendor_dir() -> Path:
    """Where the bundled binaries live, frozen or not."""
    bundled = getattr(sys, "_MEIPASS", None)
    if bundled:
        return Path(bundled) / "vendor"
    return Path(__file__).resolve().parent.parent / "vendor"


def _binary_name() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = {
        "x86_64": "x86_64",
        "amd64": "x86_64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }.get(machine, machine)
    if system == "windows":
        return f"cjpeg-windows-{arch}.exe"
    return f"cjpeg-{system}-{arch}"


_cjpeg_path: Path | None = None


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify(candidate: Path) -> None:
    """Refuse to execute a binary that is not the one we shipped.

    Costs a few milliseconds once per session.  A binary with no recorded
    checksum (a locally built arm64 one, say) is allowed through — the point is
    to catch a swapped or corrupted file, not to forbid new platforms.
    """
    expected = CHECKSUMS.get(candidate.name)
    if expected is None:
        return
    actual = sha256(candidate)
    if actual != expected:
        raise EncoderError(
            f"The bundled MozJPEG binary does not match its recorded checksum:\n"
            f"  {candidate}\n  expected {expected}\n  actual   {actual}\n"
            "Refusing to run it. Re-create it with tools/fetch_cjpeg.py."
        )


def cjpeg_path() -> Path:
    """Locate the bundled cjpeg, verifying it and making it executable."""
    global _cjpeg_path
    if _cjpeg_path is not None:
        return _cjpeg_path

    candidate = _vendor_dir() / _binary_name()
    if not candidate.is_file():
        raise EncoderError(
            f"Bundled MozJPEG binary not found: {candidate}\n"
            f"Expected a build for {platform.system()} {platform.machine()}. "
            "Run tools/fetch_cjpeg.py to (re)create it."
        )
    _verify(candidate)
    if os.name != "nt" and not os.access(candidate, os.X_OK):
        try:
            candidate.chmod(candidate.stat().st_mode | 0o111)
        except OSError as exc:
            raise EncoderError(f"Cannot make {candidate} executable: {exc}") from exc

    _cjpeg_path = candidate
    return candidate


def cjpeg_version() -> str:
    """Version banner of the bundled binary, for the about/log line."""
    result = _run([str(cjpeg_path()), "-version"], VERSION_TIMEOUT)
    banner = (result.stdout or result.stderr).strip().splitlines()
    if not banner:
        raise EncoderError(f"{cjpeg_path()} printed no version banner")
    return banner[0]


def _run(argv: list[str], timeout: int) -> subprocess.CompletedProcess:
    kwargs = {}
    if os.name == "nt":  # keep a console window from flashing up per encode
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            errors="replace",  # a banner in an odd locale must not raise
            timeout=timeout,
            **kwargs,
        )
    except subprocess.TimeoutExpired as exc:
        # subprocess.run has already killed the child by the time this lands.
        raise EncoderError(f"cjpeg did not finish within {timeout}s and was stopped") from exc


def encode(image: Image.Image, quality: int, smoothing: int = 30) -> bytes:
    """Encode an RGB image and return the JPEG bytes."""
    if image.mode != "RGB":
        raise ValueError(f"encode() expects an RGB image, got {image.mode}")

    binary = cjpeg_path()
    # TemporaryDirectory cleans up whatever is inside it, however we leave: the
    # old hand-rolled unlink/rmdir leaked the whole directory - and a full-size
    # PPM with it - if cjpeg left anything unexpected behind.
    with tempfile.TemporaryDirectory(prefix="minjpg-") as tmpdir:
        ppm = Path(tmpdir) / "in.ppm"
        jpg = Path(tmpdir) / "out.jpg"
        image.save(ppm, format="PPM")
        argv = [
            str(binary),
            "-quality", str(quality),
            "-quant-table", "3",     # ImageMagick table
            "-smooth", str(smoothing),
            "-progressive",
            "-optimize",
            "-outfile", str(jpg),
            str(ppm),
        ]
        result = _run(argv, ENCODE_TIMEOUT)
        if result.returncode != 0 or not jpg.is_file():
            raise EncoderError(
                f"cjpeg failed (exit {result.returncode}): {result.stderr.strip() or 'no output'}"
            )
        return jpg.read_bytes()
