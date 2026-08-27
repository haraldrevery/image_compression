"""Converting an arbitrary image into a web-ready high-resolution JPEG.

This is step 1 of the workflow: whatever comes off the camera or phone becomes a
sRGB JPEG capped at a long edge, which the ``_min.jpg`` pipeline then takes as
its input.  Same encoder and same resize maths as :mod:`minjpg.pipeline`, just
aimed at bigger targets, plus the three things that pipeline never needed:
more input formats, colour-profile conversion, and metadata preservation.
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageCms, ImageOps

from . import encoder, formats, resize
from .config import ConvertSettings
from .common import PipelineError, copy_atomic, write_atomic

#: EXIF tags that stop being true the moment an image is resized.
_ORIENTATION = 0x0112  # 274
_TIFF_WIDTH, _TIFF_HEIGHT = 0x0100, 0x0101  # 256, 257
_THUMBNAIL_TAGS = (0x0201, 0x0202)  # 513, 514 - offset + length of the JPEG thumbnail
_EXIF_IFD = 0x8769
_PIXEL_X, _PIXEL_Y = 0xA002, 0xA003  # 40962, 40963

#: An APP1 segment's payload cannot exceed this (2-byte length field, minus itself).
_APP1_LIMIT = 65_533

_SRGB = ImageCms.createProfile("sRGB")


@dataclass
class ConvertResult:
    source: Path
    output: Path
    source_size: tuple[int, int]
    output_size: tuple[int, int]
    byte_size: int
    quality: int
    copied: bool = False  # passed through untouched
    over_cap: bool = False  # could not meet max_size even at the quality floor
    converted_colour: bool = False  # a non-sRGB profile was converted
    metadata_kept: bool = False
    notes: str = ""

    @property
    def kilobytes(self) -> float:
        return self.byte_size / 1024

    @property
    def status(self) -> str:
        if self.copied:
            return "copied"
        if self.over_cap:
            return "over cap"
        return "done"


def to_srgb(image: Image.Image, profile: bytes | None) -> tuple[Image.Image, bool]:
    """Convert to sRGB, honouring ``profile`` if there is one.

    Without this, a Display P3 or Adobe RGB source is treated as if its numbers
    were already sRGB and comes out dull and hue-shifted.  Broken profiles are
    common in the wild, so a failure here falls back to the raw pixels rather
    than failing the file.
    """
    as_rgb = image.convert("RGB") if image.mode != "RGB" else image
    if not profile:
        return as_rgb, False

    try:
        source_profile = ImageCms.ImageCmsProfile(io.BytesIO(profile))
        if ImageCms.getProfileDescription(source_profile).strip().startswith("sRGB"):
            return as_rgb, False
        converted = ImageCms.profileToProfile(
            as_rgb, source_profile, _SRGB, outputMode="RGB"
        )
        if converted is not None:
            return converted, True
    except Exception:
        pass  # unusable profile - treat the numbers as sRGB, which is the old behaviour
    return as_rgb, False


def flatten_alpha(image: Image.Image) -> Image.Image:
    """Composite transparency onto white."""
    if image.mode in ("RGBA", "LA") or (
        image.mode == "P" and "transparency" in image.info
    ):
        image = image.convert("RGBA")
        background = Image.new("RGBA", image.size, (255, 255, 255, 255))
        return Image.alpha_composite(background, image).convert("RGB")
    return image


def load_source(path: Path) -> tuple[Image.Image, Image.Exif | None, bool]:
    """Open a source as upright sRGB RGB, returning its EXIF alongside.

    Order matters: transparency has to be composited *before* the colour
    conversion, because converting to RGB discards the alpha channel rather than
    compositing it.  White is white in every RGB space, so flattening first is
    safe.
    """
    opened = formats.open_image(path)
    exif = opened.getexif() if opened.info.get("exif") else None
    profile = opened.info.get("icc_profile")
    upright = ImageOps.exif_transpose(opened) or opened
    srgb, converted = to_srgb(flatten_alpha(upright), profile)
    return srgb, exif, converted


def build_exif_payload(exif: Image.Exif, output_size: tuple[int, int]) -> bytes | None:
    """Serialise EXIF for re-injection, fixing what resizing invalidated.

    Returns ``None`` when there is nothing worth writing or the result would
    exceed what an APP1 segment can hold.
    """
    # Rotation is baked into the pixels; leaving the tag would rotate twice.
    exif[_ORIENTATION] = 1
    exif[_TIFF_WIDTH], exif[_TIFF_HEIGHT] = output_size
    for tag in _THUMBNAIL_TAGS:
        exif.pop(tag, None)  # the embedded thumbnail is stale and we cannot rebuild it
    try:
        sub = exif.get_ifd(_EXIF_IFD)
        if sub:
            sub[_PIXEL_X], sub[_PIXEL_Y] = output_size
    except Exception:
        pass

    try:
        payload = exif.tobytes()
    except Exception:
        return None
    if not payload:
        return None
    # Pillow >= 10 includes the marker; older versions return a bare TIFF block.
    if not payload.startswith(b"Exif\x00\x00"):
        payload = b"Exif\x00\x00" + payload
    return payload if len(payload) <= _APP1_LIMIT else None


def inject_exif(jpeg: bytes, payload: bytes) -> bytes:
    """Splice an APP1 EXIF segment into a JPEG produced by cjpeg.

    cjpeg reads PPM, so its output carries no metadata at all; the segment goes
    in after any APP0 (JFIF) segment, which is where readers expect it.
    """
    if jpeg[:2] != b"\xff\xd8":
        return jpeg
    position = 2
    while jpeg[position : position + 2] == b"\xff\xe0":
        position += 2 + int.from_bytes(jpeg[position + 2 : position + 4], "big")
    segment = b"\xff\xe1" + (len(payload) + 2).to_bytes(2, "big") + payload
    return jpeg[:position] + segment + jpeg[position:]


def _search_quality(
    frame: Image.Image, settings: ConvertSettings, overhead: int
) -> tuple[int, bytes, bool]:
    """Encode at the set quality, searching down only if the cap demands it.

    ``overhead`` is the size of the EXIF that will be spliced in afterwards, so
    the cap applies to the file that actually lands on disk.
    """
    quality = settings.quality
    data = encoder.encode(frame, quality, settings.smoothing)
    budget = settings.max_size - overhead
    if not settings.max_size or len(data) <= budget:
        return quality, data, False

    floor_data = encoder.encode(frame, settings.quality_floor, settings.smoothing)
    if len(floor_data) > budget:
        # Nothing in range fits.  Hand back the floor result; the caller writes
        # it and flags the row rather than silently dropping the image.
        return settings.quality_floor, floor_data, True

    best_quality, best_data = settings.quality_floor, floor_data
    low, high = settings.quality_floor + 1, quality - 1
    while low <= high:
        mid = (low + high) // 2
        data = encoder.encode(frame, mid, settings.smoothing)
        if len(data) <= budget:
            best_quality, best_data = mid, data
            low = mid + 1
        else:
            high = mid - 1
    return best_quality, best_data, False


def convert(
    source: Path,
    output: Path,
    settings: ConvertSettings,
    long_edge: int | None = None,
    quality: int | None = None,
) -> ConvertResult:
    """Convert one file. ``long_edge``/``quality`` are per-image GUI overrides."""
    try:
        image, exif, converted_colour = load_source(source)
    except Exception as exc:
        raise PipelineError(f"cannot read: {exc}") from exc

    source_size = image.size
    cap = long_edge if long_edge else settings.max_long_edge
    size = resize.target_size(source_size, cap)

    if _can_pass_through(
        source, settings, size, source_size, long_edge, quality, converted_colour
    ):
        copy_atomic(source, output)
        return ConvertResult(
            source, output, source_size, source_size, output.stat().st_size,
            quality=0, copied=True, metadata_kept=True,
            notes="already within the long edge and size cap",
        )

    frame = resize.resize(image, size, settings.linear_light_resize)

    payload = None
    if exif is not None and not settings.strip_metadata:
        payload = build_exif_payload(exif, size)
    overhead = len(payload) + 4 if payload else 0

    if quality is not None:
        data = encoder.encode(frame, quality, settings.smoothing)
        used_quality, over_cap = quality, bool(
            settings.max_size and len(data) + overhead > settings.max_size
        )
    else:
        used_quality, data, over_cap = _search_quality(frame, settings, overhead)

    if payload:
        data = inject_exif(data, payload)

    write_atomic(output, data)
    notes = []
    if converted_colour:
        notes.append("converted to sRGB")
    if over_cap:
        # An override skips the search entirely, so blaming the quality floor
        # would name a quality the file was never encoded at.
        reason = (
            f"at the quality {used_quality} you asked for"
            if quality is not None
            else f"even at the quality floor {settings.quality_floor}"
        )
        notes.append(
            f"{len(data) / 1024:.1f} KB exceeds the "
            f"{settings.max_size / 1024:.1f} KB cap {reason}"
        )
    return ConvertResult(
        source, output, source_size, size, len(data), used_quality,
        over_cap=over_cap, converted_colour=converted_colour,
        metadata_kept=bool(payload), notes="; ".join(notes),
    )


def _can_pass_through(
    source: Path,
    settings: ConvertSettings,
    size: tuple[int, int],
    source_size: tuple[int, int],
    long_edge: int | None,
    quality: int | None,
    converted_colour: bool,
) -> bool:
    """Is re-encoding this file pointless?

    Only for JPEGs that already fit both limits.  Copying keeps the source's own
    metadata, so it is off when metadata is meant to be stripped, and an
    explicit per-image override always means the user wants a real re-encode.

    It is also off when the source needed a colour conversion: copying an Adobe
    RGB or Display P3 file verbatim would quietly break the promise that
    everything written here is sRGB.
    """
    if not settings.passthrough or settings.strip_metadata or converted_colour:
        return False
    if long_edge is not None or quality is not None:
        return False
    if source.suffix.lower() not in (".jpg", ".jpeg", ".jpe", ".jfif"):
        return False
    if size != source_size:
        return False
    return not settings.max_size or source.stat().st_size <= settings.max_size
