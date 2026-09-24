"""Reading metadata out of a source, and carrying XMP and IPTC across.

cjpeg's output carries no metadata at all, so whatever the source had must be
spliced back in.  Reading all three blocks happens here — EXIF, XMP, which
Lightroom, Bridge, Capture One, darktable and the like use for captions,
keywords, ratings and colour labels, and IPTC-IIM, the older caption and keyword
block many of the same apps still write alongside it.  Writing the EXIF back is
left to :mod:`minjpg.convert`, which knows the output's size; XMP and IPTC are
written here.

Anything a source holds that cannot be carried is reported by name rather than
dropped quietly.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from PIL import ExifTags, Image, IptcImagePlugin

#: JPEG markers for the segments written here.
APP1, APP13 = 0xE1, 0xED

#: A segment's payload cannot exceed this (a 2-byte length that counts itself).
SEGMENT_LIMIT = 65_533

_XMP_HEADER = b"http://ns.adobe.com/xap/1.0/\x00"
_PHOTOSHOP_HEADER = b"Photoshop 3.0\x00"

#: Photoshop image-resource ids: the IPTC block, Photoshop's digest of it, XMP.
_IIM, _IIM_DIGEST, _PSD_XMP = 0x0404, 0x0425, 0x0424
_TIFF_XMP = 700

#: Simple XMP properties the conversion makes untrue, and what they become.
#: Only the conventional prefixes are matched, which is what writers use.
_ALWAYS = {"tiff:Orientation": "1", "photoshop:ColorMode": "3"}  # 3 = RGB
_WHEN_CONVERTED = {"exif:ColorSpace": "1", "photoshop:ICCProfile": "sRGB IEC61966-2.1"}

#: Dropped only when a packet is too big for one JPEG segment: bulk that
#: describes no one's photo.  ``None`` means the whole namespace.
_BULKY = {
    "http://ns.adobe.com/camera-raw-settings/1.0/": None,  # develop settings
    "http://ns.adobe.com/xap/1.0/": {"Thumbnails"},
    "http://ns.adobe.com/photoshop/1.0/": {"DocumentAncestors"},
    "http://ns.adobe.com/xap/1.0/mm/": {"History", "Pantry", "Ingredients", "Manifest"},
}

_PADDING = re.compile(r"\s+(<\?xpacket end=)")
_NAMESPACE = re.compile(r'xmlns:([\w.-]+)\s*=\s*["\']([^"\']+)["\']')

#: The EXIF sub-directories: the camera block (exposure, lens, date taken),
#: GPS, and the interoperability block the camera block points to.
_EXIF_IFD, _GPS_IFD, _INTEROP_IFD = 0x8769, 0x8825, 0xA005

#: A TIFF's own first-directory tags that belong in a JPEG's EXIF.  A TIFF keeps
#: its EXIF in the same directory that describes the file itself, so only these
#: descriptive ones come across; the camera block and GPS follow whole.
_TIFF_DESCRIPTIVE = frozenset({
    269, 270,  # DocumentName, ImageDescription
    271, 272,  # Make, Model
    274,  # Orientation (reset to 1 on the way out, once the rotation is baked in)
    282, 283, 296,  # XResolution, YResolution, ResolutionUnit
    285,  # PageName
    305, 306,  # Software, DateTime
    315, 316,  # Artist, HostComputer
    18246, 18249,  # Rating, RatingPercent, as Windows writes them
    33432,  # Copyright
    40091, 40092, 40093, 40094, 40095,  # Windows title, comment, author, keywords, subject
})

#: TIFF tags that describe the file's own layout, or data that is carried some
#: other way.  Leaving these behind loses nothing anyone wrote; any other tag
#: left behind is named in the result.
_TIFF_STRUCTURE = frozenset({
    254, 255, 256, 257, 258, 259, 262, 263, 266, 273, 277, 278, 279, 280, 281,
    284, 292, 293, 297, 301, 317, 318, 319, 320, 321, 322, 323, 324, 325, 330,
    332, 338, 339, 340, 341, 347, 512, 513, 514, 515, 517, 518, 519, 520, 521,
    529, 530, 531, 532,
    700,  # XMP, read on its own
    33723,  # IPTC, read on its own
    34377,  # Photoshop's resources
    34675,  # ICC profile, applied to the pixels
    37724,  # Photoshop's layers
    50341,  # PrintIM, printer settings
})

#: Where a JPEG keeps the part of an XMP packet too big for one segment.
_EXTENDED_XMP = b"http://ns.adobe.com/xmp/extension/\x00"
_PNG_RAW_PROFILE = "raw profile type "


# ------------------------------------------------------------------ reading


def read_exif(image: Image.Image) -> tuple[Image.Exif | None, list[str]]:
    """The source's EXIF, ready to carry across, and a note for anything left behind.

    Most formats hold EXIF as one block that Pillow hands over whole — PNG
    also in ImageMagick's hex text form.  A TIFF keeps it among the tags that
    describe the file itself, so the descriptive ones, the camera block and GPS
    are lifted into a block of their own.
    """
    if image.info.get("exif") or image.info.get("Raw profile type exif"):
        return image.getexif(), []
    if image.format != "TIFF":
        return None, []
    try:
        return _tiff_exif(image)
    except Exception:
        return None, ["EXIF could not be read"]


def _tiff_exif(image: Image.Image) -> tuple[Image.Exif | None, list[str]]:
    source = image.getexif()
    lifted = Image.Exif()
    left_behind = []
    for tag, value in source.items():
        if tag in _TIFF_DESCRIPTIVE:
            lifted[tag] = value
        elif tag not in _TIFF_STRUCTURE and tag not in (_EXIF_IFD, _GPS_IFD):
            left_behind.append(ExifTags.TAGS.get(tag, f"tag {tag}"))
    for pointer in (_EXIF_IFD, _GPS_IFD):
        if pointer not in source:
            continue
        block = dict(source.get_ifd(pointer))
        if pointer == _EXIF_IFD and _INTEROP_IFD in block:
            block[_INTEROP_IFD] = dict(source.get_ifd(_INTEROP_IFD))
        if block:
            lifted[pointer] = block
    notes = [f"TIFF tags not carried: {', '.join(left_behind)}"] if left_behind else []
    if not len(lifted):
        return None, notes
    # Serialised and read back, so it behaves exactly like EXIF read from a JPEG.
    carried = Image.Exif()
    carried.load(lifted.tobytes())
    return carried, notes


def uncarried_blocks(image: Image.Image) -> list[str]:
    """A note for each metadata block in the source that cannot be carried across.

    Detected rather than carried: an XMP packet's overflow into extra JPEG
    segments (almost always depth maps or develop settings, never a caption),
    and the hex text profiles ImageMagick writes into PNGs for anything but EXIF.
    """
    notes = []
    for marker, data in getattr(image, "applist", None) or ():
        if marker == "APP1" and data.startswith(_EXTENDED_XMP):
            notes.append("extended XMP (the overflow of a very large XMP packet) could not be kept")
            break
    if image.format == "PNG":
        for key in image.info:
            name = key.lower() if isinstance(key, str) else ""
            if name.startswith(_PNG_RAW_PROFILE) and name != _PNG_RAW_PROFILE + "exif":
                notes.append(f"PNG '{key[len(_PNG_RAW_PROFILE):]}' profile could not be kept")
    return notes


def _as_bytes(value: object) -> bytes | None:
    if isinstance(value, bytes):
        return value or None
    if isinstance(value, str):
        return value.encode("utf-8") or None
    if isinstance(value, (tuple, list)) and value and all(
        isinstance(v, int) and 0 <= v < 256 for v in value
    ):
        return bytes(value)
    return None


def _psd_resource(image: Image.Image, resource_id: int) -> bytes | None:
    if image.format != "PSD":
        return None
    for found_id, _name, data in getattr(image, "resources", None) or ():
        if found_id == resource_id:
            return _as_bytes(data)
    return None


def read_xmp(image: Image.Image) -> bytes | None:
    """The source's XMP packet, wherever its format keeps it."""
    for key in ("xmp", "XML:com.adobe.xmp"):  # JPEG, WebP, HEIF; PNG's iTXt
        if found := _as_bytes(image.info.get(key)):
            return found
    tags = getattr(image, "tag_v2", None)  # TIFF
    if tags is not None and _TIFF_XMP in tags:
        if found := _as_bytes(tags[_TIFF_XMP]):
            return found
    return _psd_resource(image, _PSD_XMP)


def read_iptc(image: Image.Image) -> tuple[bytes | None, bytes | None]:
    """The source's IPTC block, and Photoshop's digest of it if it has one.

    The digest tells Adobe apps whether IPTC and XMP still agree, and it only
    means that next to the exact bytes it was taken over — so it travels only
    when those bytes are copied verbatim.
    """
    photoshop = image.info.get("photoshop")  # JPEG's APP13, by resource id
    if isinstance(photoshop, dict) and photoshop.get(_IIM):
        return _as_bytes(photoshop[_IIM]), _as_bytes(photoshop.get(_IIM_DIGEST))
    if iim := _psd_resource(image, _IIM):
        return iim, _psd_resource(image, _IIM_DIGEST)
    if getattr(image, "tag_v2", None) is not None:
        # TIFF: Pillow only offers the parsed form publicly, so rebuild it.
        try:
            parsed = IptcImagePlugin.getiptcinfo(image)
        except Exception:
            parsed = None
        if parsed:
            return _serialise_iim(parsed), None
    return None, None


def _serialise_iim(parsed: dict) -> bytes:
    out = bytearray()
    for (record, dataset), value in sorted(parsed.items()):
        for item in value if isinstance(value, list) else [value]:
            data = bytes(item)
            out += bytes((0x1C, record, dataset))
            if len(data) < 0x8000:
                out += len(data).to_bytes(2, "big")
            else:  # an extended dataset: the length of the length, then the length
                out += (0x8004).to_bytes(2, "big") + len(data).to_bytes(4, "big")
            out += data
    return bytes(out)


# ------------------------------------------------------------------ writing


def segment(marker: int, payload: bytes) -> bytes:
    return b"\xff" + bytes((marker,)) + (len(payload) + 2).to_bytes(2, "big") + payload


def inject(jpeg: bytes, segments: list[bytes]) -> bytes:
    """Splice ready-made segments into a JPEG produced by cjpeg.

    They go in after any APP0 (JFIF) segment, which is where readers expect the
    EXIF; XMP and IPTC follow it in the order given.
    """
    if not segments or jpeg[:2] != b"\xff\xd8":
        return jpeg
    position = 2
    while jpeg[position : position + 2] == b"\xff\xe0":
        position += 2 + int.from_bytes(jpeg[position + 2 : position + 4], "big")
    return jpeg[:position] + b"".join(segments) + jpeg[position:]


def iptc_segment(iim: bytes, digest: bytes | None) -> bytes | None:
    """An APP13 segment carrying the IPTC block, and its digest if it had one.

    Photoshop's other image resources are left behind on purpose: its
    thumbnail, its copy of the EXIF and its ICC profile would all be stale.
    ``None`` when the block will not fit in one segment.
    """
    payload = _PHOTOSHOP_HEADER + _resource(_IIM, iim)
    if digest:
        payload += _resource(_IIM_DIGEST, digest)
    return segment(APP13, payload) if len(payload) <= SEGMENT_LIMIT else None


def _resource(resource_id: int, data: bytes) -> bytes:
    # signature, id, an empty name padded to even, size, data padded to even
    header = b"8BIM" + resource_id.to_bytes(2, "big") + b"\x00\x00"
    return header + len(data).to_bytes(4, "big") + data + b"\x00" * (len(data) % 2)


def xmp_segment(
    xmp: bytes, size: tuple[int, int], converted_colour: bool
) -> tuple[bytes | None, str | None]:
    """An APP1 segment with the XMP brought up to date, and a note if it lost anything.

    Everything is kept as written — captions, keywords, ratings, labels,
    develop settings — except the facts the conversion changed: the rotation
    baked into the pixels, the new dimensions, and the sRGB the colours were
    converted to.  Only a packet too big for the 64 KB a JPEG segment holds
    loses its bulk, and then only bulk that describes no one's photo.
    """
    try:
        text = xmp.decode("utf-8")
    except UnicodeDecodeError:
        return None, "XMP could not be kept (not UTF-8)"
    # The trailing padding exists for editing a file in place; it is dead weight here.
    text = _PADDING.sub(r"\n\1", _correct(text, size, converted_colour))
    if len(payload := _XMP_HEADER + text.encode("utf-8")) <= SEGMENT_LIMIT:
        return segment(APP1, payload), None
    slim = _shed_bulk(text)
    if slim is not None and len(payload := _XMP_HEADER + slim.encode("utf-8")) <= SEGMENT_LIMIT:
        return segment(APP1, payload), (
            "XMP develop settings and edit history dropped to fit a JPEG "
            "(captions, keywords and ratings kept)"
        )
    return None, "XMP could not be kept (too large for a JPEG)"


def _correct(text: str, size: tuple[int, int], converted_colour: bool) -> str:
    width, height = size
    facts = {
        **_ALWAYS,
        "tiff:ImageWidth": str(width), "tiff:ImageLength": str(height),
        "exif:PixelXDimension": str(width), "exif:PixelYDimension": str(height),
    }
    if converted_colour:
        facts.update(_WHEN_CONVERTED)
    for name, value in facts.items():
        text = _set_property(text, name, value)
    return text


def _set_property(text: str, name: str, value: str) -> str:
    """Replace a simple property's value, written as an attribute or an element.

    A property that is not there is left out rather than added.
    """
    quoted = re.escape(name)
    text = re.sub(
        rf"""(\s{quoted}\s*=\s*)(?:"[^"]*"|'[^']*')""",
        lambda match: f'{match.group(1)}"{value}"', text,
    )
    return re.sub(
        rf"(<{quoted}(?:\s[^>]*)?>)[^<]*(</{quoted}>)",
        lambda match: f"{match.group(1)}{value}{match.group(2)}", text,
    )


def _is_bulky(name: str) -> bool:
    if not name.startswith("{"):
        return False
    uri, local = name[1:].split("}", 1)
    if uri not in _BULKY:
        return False
    names = _BULKY[uri]
    return names is None or local in names


def _shed_bulk(text: str) -> str | None:
    """The packet without its bulky properties, or ``None`` if it will not parse.

    Parsing only happens on this rare path, so an ordinary packet is written
    back exactly as it came, bar the corrected facts.
    """
    for prefix, uri in _NAMESPACE.findall(text):
        try:
            ET.register_namespace(prefix, uri)  # keep the familiar prefixes
        except ValueError:
            pass  # one ElementTree reserves; it will pick its own
    try:
        root = ET.fromstring(text.encode("utf-8"))
    except ET.ParseError:
        return None
    for element in list(root.iter()):
        for child in list(element):
            if _is_bulky(child.tag):
                element.remove(child)
        for name in [name for name in element.attrib if _is_bulky(name)]:
            del element.attrib[name]
    body = ET.tostring(root, encoding="unicode")
    return f'<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>\n{body}\n<?xpacket end="w"?>'
