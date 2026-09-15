"""Converting an arbitrary image into a web-ready high-resolution JPEG.

This is step 1 of the workflow: whatever comes off the camera or phone becomes a
sRGB JPEG capped at a long edge, which the ``_min.jpg`` pipeline then takes as
its input.  Same encoder and same resize maths as :mod:`minjpg.pipeline`, just
aimed at bigger targets, plus the three things that pipeline never needed:
more input formats, colour-profile conversion, and metadata preservation.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageCms, ImageOps

from . import encoder, formats, metadata, resize
from .config import ConvertSettings
from .common import PipelineError, copy_atomic, write_atomic

#: EXIF tags that stop being true the moment an image is resized.
_ORIENTATION = 0x0112  # 274
_TIFF_WIDTH, _TIFF_HEIGHT = 0x0100, 0x0101  # 256, 257
_THUMBNAIL_TAGS = (0x0201, 0x0202)  # 513, 514 - offset + length of the JPEG thumbnail
_EXIF_IFD = 0x8769
_PIXEL_X, _PIXEL_Y = 0xA002, 0xA003  # 40962, 40963
_COLOR_SPACE = 0xA001  # 40961: 1 = sRGB, 0xFFFF = uncalibrated (how Adobe RGB is marked)

_SRGB = ImageCms.createProfile("sRGB")

#: Modes a colour profile can be applied to directly.  A CMYK or greyscale
#: profile describes CMYK or greyscale numbers, not RGB ones.
_PROFILE_MODES = ("RGB", "CMYK", "L")


class KeepOriginal(PipelineError):
    """This source is better carried across unchanged than converted.

    Raised rather than returned so the batch's usual fallback — copying the
    original into the mirror — handles it exactly as it does an unreadable file.
    """


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
    kept_metadata: str = ""  # which blocks were carried across, e.g. "EXIF, XMP, IPTC"

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


@dataclass
class Source:
    """A decoded source, and what was learned about it on the way in."""

    image: Image.Image  # upright, sRGB, mode RGB
    exif: Image.Exif | None
    converted_colour: bool  # a non-sRGB profile was converted
    format: str | None  # what the file really is, whatever its name says
    mode: str  # as decoded, before any conversion
    frames: int
    notes: list[str] = field(default_factory=list)
    xmp: bytes | None = None
    iptc: bytes | None = None
    iptc_digest: bytes | None = None


def _as_rgb(image: Image.Image) -> Image.Image:
    return image if image.mode == "RGB" else image.convert("RGB")


def to_srgb(image: Image.Image, profile: bytes | None) -> tuple[Image.Image, bool]:
    """Convert to sRGB, honouring ``profile`` if there is one.

    Without this, a Display P3 or Adobe RGB source is treated as if its numbers
    were already sRGB and comes out dull and hue-shifted.  The transform is
    tried on the numbers in their own mode first — a CMYK or greyscale profile
    cannot apply to RGB, and converting to RGB beforehand made every such
    transform fail — then on RGB, which is all the old code tried.  Broken
    profiles are common in the wild, so a failure here falls back to the raw
    pixels rather than failing the file.
    """
    if not profile:
        return _as_rgb(image), False
    try:
        source_profile = ImageCms.ImageCmsProfile(io.BytesIO(profile))
        is_srgb = ImageCms.getProfileDescription(source_profile).strip().startswith("sRGB")
    except Exception:
        return _as_rgb(image), False  # unusable profile - treat the numbers as sRGB
    if is_srgb:
        return _as_rgb(image), False

    own = image.mode if image.mode in _PROFILE_MODES else "RGB"
    for mode in dict.fromkeys((own, "RGB")):
        try:
            candidate = image if image.mode == mode else image.convert(mode)
            converted = ImageCms.profileToProfile(
                candidate, source_profile, _SRGB, outputMode="RGB"
            )
        except Exception:
            continue
        if converted is not None:
            return converted, True
    return _as_rgb(image), False


def flatten_alpha(image: Image.Image) -> Image.Image:
    """Composite transparency onto white."""
    if image.mode in ("RGBA", "LA", "PA") or (
        image.mode == "P" and "transparency" in image.info
    ):
        image = image.convert("RGBA")
        background = Image.new("RGBA", image.size, (255, 255, 255, 255))
        return Image.alpha_composite(background, image).convert("RGB")
    return image


def load_source(path: Path) -> Source:
    """Open a source as upright sRGB RGB, noting what it was on the way.

    Order matters: 16-bit and floating-point values are scaled to 8 bits before
    anything else can clip them; transparency is composited *before* the colour
    conversion, because converting to RGB discards the alpha channel rather than
    compositing it (white is white in every RGB space, so flattening first is
    safe); and the profile is applied last, to numbers in their own mode.
    """
    opened = formats.open_image(path)
    source_format, source_mode = opened.format, opened.mode
    frames = formats.frame_count(opened)
    exif = opened.getexif() if opened.info.get("exif") else None
    profile = opened.info.get("icc_profile")
    notes = []
    try:
        xmp = metadata.read_xmp(opened)
        iptc, iptc_digest = metadata.read_iptc(opened)
    except Exception:
        # Metadata that cannot be read must never cost the image itself.
        xmp = iptc = iptc_digest = None
        notes.append("XMP/IPTC could not be read")
    upright = ImageOps.exif_transpose(opened) or opened
    image = formats.to_8bit(upright)
    if image is not upright:
        notes.append(f"{source_mode} source reduced to 8 bits")
    srgb, converted = to_srgb(flatten_alpha(image), profile)
    return Source(
        srgb, exif, converted, source_format, source_mode, frames, notes,
        xmp=xmp, iptc=iptc, iptc_digest=iptc_digest,
    )


def build_exif_payload(
    exif: Image.Exif, output_size: tuple[int, int], srgb: bool = False
) -> bytes | None:
    """Serialise EXIF for re-injection, fixing what the conversion invalidated.

    ``srgb`` means the colours were converted from another profile, so a
    colour-space tag still saying "uncalibrated" (Adobe RGB) would be a lie.
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
            if srgb:
                sub[_COLOR_SPACE] = 1
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
    return payload if len(payload) <= metadata.SEGMENT_LIMIT else None


def _metadata_segments(
    loaded: Source, size: tuple[int, int], notes: list[str]
) -> tuple[list[bytes], list[str]]:
    """The EXIF, XMP and IPTC segments to splice in, and which of them made it.

    cjpeg reads PPM, so its output carries no metadata at all.  Anything the
    user asked to keep that could not be kept is added to ``notes`` rather
    than dropped quietly.
    """
    segments: list[bytes] = []
    kept: list[str] = []
    if loaded.exif is not None:
        payload = build_exif_payload(loaded.exif, size, srgb=loaded.converted_colour)
        if payload is None:
            notes.append("EXIF could not be kept (too large for a JPEG, or unreadable)")
        else:
            segments.append(metadata.segment(metadata.APP1, payload))
            kept.append("EXIF")
    if loaded.xmp:
        xmp, note = metadata.xmp_segment(loaded.xmp, size, loaded.converted_colour)
        if note:
            notes.append(note)
        if xmp:
            segments.append(xmp)
            kept.append("XMP")
    if loaded.iptc:
        iptc = metadata.iptc_segment(loaded.iptc, loaded.iptc_digest)
        if iptc:
            segments.append(iptc)
            kept.append("IPTC")
        else:
            notes.append("IPTC could not be kept (too large for a JPEG)")
    return segments, kept


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

    floor = settings.effective_floor
    if floor >= quality:
        return quality, data, True  # no room to search downwards

    floor_data = encoder.encode(frame, floor, settings.smoothing)
    if len(floor_data) > budget:
        # Nothing in range fits.  Hand back the floor result; the caller writes
        # it and flags the row rather than silently dropping the image.
        return floor, floor_data, True

    best_quality, best_data = floor, floor_data
    low, high = floor + 1, quality - 1
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
        loaded = load_source(source)
    except Exception as exc:
        raise PipelineError(f"cannot read: {exc}") from exc

    if loaded.frames > 1 and loaded.format in formats.PAGED_FORMATS:
        # A JPEG holds one picture.  Converting the first frame would quietly
        # drop the rest from a folder that is meant to be a full copy.
        raise KeepOriginal(
            f"{formats.describe_frames(loaded.format, loaded.frames)}: "
            "kept as it is so no frame is lost"
        )

    image = loaded.image
    source_size = image.size
    cap = long_edge if long_edge else settings.max_long_edge
    size = resize.target_size(source_size, cap)

    if _can_pass_through(source, settings, size, source_size, long_edge, quality, loaded):
        copied = copy_atomic(source, output)
        return ConvertResult(
            source, output, source_size, source_size, copied,
            quality=0, copied=True, metadata_kept=True,
            notes="already within the long edge and size cap",
        )

    frame = resize.resize(image, size, settings.linear_light_resize)

    notes = list(loaded.notes)
    segments, kept = (
        ([], []) if settings.strip_metadata else _metadata_segments(loaded, size, notes)
    )
    # The cap applies to the file that lands on disk, metadata and all.
    overhead = sum(len(segment) for segment in segments)

    if quality is not None:
        data = encoder.encode(frame, quality, settings.smoothing)
        used_quality, over_cap = quality, bool(
            settings.max_size and len(data) + overhead > settings.max_size
        )
    else:
        used_quality, data, over_cap = _search_quality(frame, settings, overhead)

    if segments:
        data = metadata.inject(data, segments)

    # The run folder is a mirror of the input, so the file keeps its source's
    # date: for anything without EXIF, that date is the only one there is.
    write_atomic(output, data, times_from=source)
    if loaded.converted_colour:
        notes.append("converted to sRGB")
    if over_cap:
        # An override skips the search entirely, so blaming the quality floor
        # would name a quality the file was never encoded at.
        reason = (
            f"at the quality {used_quality} you asked for"
            if quality is not None
            else f"even at the quality floor {settings.effective_floor}"
        )
        notes.append(
            f"{len(data) / 1024:.1f} KB exceeds the "
            f"{settings.max_size / 1024:.1f} KB cap {reason}"
        )
    return ConvertResult(
        source, output, source_size, size, len(data), used_quality,
        over_cap=over_cap, converted_colour=loaded.converted_colour,
        metadata_kept=bool(kept), kept_metadata=", ".join(kept), notes="; ".join(notes),
    )


def _can_pass_through(
    source: Path,
    settings: ConvertSettings,
    size: tuple[int, int],
    source_size: tuple[int, int],
    long_edge: int | None,
    quality: int | None,
    loaded: Source,
) -> bool:
    """Is re-encoding this file pointless?

    Only for JPEGs that already fit both limits — judged by content rather than
    name, since a PNG or HEIC saved as ".jpg" would otherwise be copied as it
    is, and only RGB or greyscale ones, since a CMYK JPEG is no web image.
    Copying keeps the source's own metadata, so it is off when metadata is
    meant to be stripped, and an explicit per-image override always means the
    user wants a real re-encode.

    It is also off when the source needed a colour conversion: copying an Adobe
    RGB or Display P3 file verbatim would quietly break the promise that
    everything written here is sRGB.
    """
    if not settings.passthrough or settings.strip_metadata or loaded.converted_colour:
        return False
    if long_edge is not None or quality is not None:
        return False
    if source.suffix.lower() not in (".jpg", ".jpeg", ".jpe", ".jfif"):
        return False
    if loaded.format not in ("JPEG", "MPO") or loaded.mode not in ("RGB", "L"):
        return False
    if size != source_size:
        return False
    return not settings.max_size or source.stat().st_size <= settings.max_size
