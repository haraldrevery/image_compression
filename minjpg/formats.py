"""Input format support: what can be read, and how to read it.

Pillow covers JPEG, PNG, TIFF, WebP, BMP, GIF, PSD, JPEG2000, TGA and more out
of the box.  ``pillow-heif`` adds HEIC/HEIF (iPhone photos) — and AVIF as a
side effect — when it is installed; the app still works without it, just
without those formats.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
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

#: Formats whose extra frames are content — pages or animation — rather than
#: layers (PSD), sizes (ICO) or a camera's embedded preview image (MPO).
PAGED_FORMATS = frozenset({"TIFF", "GIF", "WEBP", "PNG"})


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


def frame_count(image: Image.Image) -> int:
    return max(1, int(getattr(image, "n_frames", 1) or 1))


def describe_frames(image_format: str | None, frames: int) -> str:
    if image_format == "TIFF":
        return f"multi-page TIFF ({frames} pages)"
    return f"animated {image_format or 'image'} ({frames} frames)"


def to_8bit(image: Image.Image) -> Image.Image:
    """Scale 16-bit and floating-point pixels into 0-255, as mode ``L``.

    Pillow's ``convert()`` clips such values instead of scaling them, so a
    16-bit greyscale scan converted straight to RGB came out solid white.
    Returns ``image`` itself when it already has 8 bits per channel.
    """
    mode = image.mode
    if mode == "F":
        values = np.nan_to_num(np.asarray(image, dtype=np.float64))
        scale = 255.0 if values.max(initial=0.0) <= 1.0 else 1.0
        return Image.fromarray(np.clip(values * scale + 0.5, 0, 255).astype(np.uint8))
    if mode.startswith("I;16"):
        values = np.asarray(image).astype(np.uint32)  # numpy handles the byte order
        full = 65535
    elif mode == "I":
        values = np.clip(np.asarray(image).astype(np.int64), 0, None)
        top = int(values.max(initial=0))
        # 8-bit numbers in a 32-bit container, 16-bit ones, or something wider.
        full = 255 if top <= 255 else 65535 if top <= 65535 else top
    else:
        return image
    return Image.fromarray(((values * 255 + full // 2) // full).astype(np.uint8))
