"""Input format support: what can be read, and how to read it.

Pillow covers JPEG, PNG, TIFF, WebP, BMP, GIF, PSD, JPEG2000, TGA and more out
of the box.  ``pillow-heif`` adds HEIC/HEIF (iPhone photos) — and AVIF as a
side effect — when it is installed; the app still works without it, just
without those formats.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image

HEIF_AVAILABLE = False
HEIF_ERROR: str | None = None

try:
    from pillow_heif import register_heif_opener

    register_heif_opener()
    HEIF_AVAILABLE = True
except Exception as exc:  # ImportError, or a broken libheif
    HEIF_ERROR = str(exc)

#: Formats worth offering as converter input.  Pillow can open more (FITS, DDS,
#: XPM…) but they are not photo sources, and listing them would only widen the
#: net for junk files sitting in a folder.
_BASE_EXTENSIONS = {
    ".jpg", ".jpeg", ".jpe", ".jfif",
    ".png",
    ".webp",
    ".tif", ".tiff",
    ".bmp", ".dib",
    ".gif",
    ".psd",
    ".jp2", ".j2k", ".jpf", ".jpx",
    ".tga",
    ".ppm", ".pgm", ".pnm",
    ".ico",
}
_HEIF_EXTENSIONS = {".heic", ".heif", ".heics", ".heifs", ".hif", ".avif", ".avifs"}


def source_extensions(include_heif: bool = True) -> frozenset[str]:
    extensions = set(_BASE_EXTENSIONS)
    if include_heif and HEIF_AVAILABLE:
        extensions |= _HEIF_EXTENSIONS
    return frozenset(extensions)


def describe_support() -> str:
    """One-line summary for the log and ``--selftest``."""
    if HEIF_AVAILABLE:
        return f"{len(source_extensions())} input formats, HEIC/HEIF/AVIF enabled"
    reason = f" ({HEIF_ERROR})" if HEIF_ERROR else ""
    return f"{len(source_extensions())} input formats, HEIC/HEIF unavailable{reason}"


def open_image(path: Path) -> Image.Image:
    """Open an image, using only the first frame of multi-frame sources.

    Animated GIFs, multipage TIFFs and layered PSDs all decode to a stack; a
    converter to a single JPEG only has one sensible answer for those.
    """
    opened = Image.open(path)
    opened.load()
    return opened
