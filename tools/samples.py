"""Generated stand-ins for sample photos and the metadata photo software writes.

The verify suites were written against a folder of real originals
(``../example_data``) that is not part of the repository.  Without it they
skipped whole sections and still reported success.  ``--synthetic`` points them
here instead: seeded, so every run sees the same pixels, and photo-like enough
— smooth tones, hard edges, some texture — to give the quality search
something realistic to work on.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter


def photo(size: tuple[int, int], rng: np.random.Generator) -> Image.Image:
    """An RGB image with a photo's mix of gradients, edges and detail."""
    width, height = size
    tones = rng.integers(0, 256, (max(1, height // 40), max(1, width // 40), 3), dtype=np.uint8)
    image = Image.fromarray(tones).resize(size, Image.BICUBIC).filter(ImageFilter.GaussianBlur(3))
    draw = ImageDraw.Draw(image)
    for _ in range(80):
        x, y = int(rng.integers(0, width)), int(rng.integers(0, height))
        w, h = int(rng.integers(20, 300)), int(rng.integers(20, 300))
        draw.ellipse([x, y, x + w, y + h], fill=tuple(int(v) for v in rng.integers(0, 256, 3)))
    return image.filter(ImageFilter.GaussianBlur(1))


def make_photos(folder: Path, count: int = 6, seed: int = 1) -> Path:
    """``count`` landscape and portrait JPEGs, each with a ``_min.jpg`` beside it.

    The ``_min.jpg`` files stand in for the hand-made Squoosh references.  They
    are plain Pillow thumbnails, so a size comparison against them means
    nothing, but every check on the app's own output still holds.
    """
    folder.mkdir(parents=True, exist_ok=True)
    rng = np.random.default_rng(seed)
    for index in range(count):
        image = photo((3000, 2000) if index % 2 else (2000, 3000), rng)
        image.save(folder / f"IMG_{index:04d}.jpg", quality=92)
        image.thumbnail((1280, 1280))
        image.save(folder / f"IMG_{index:04d}_min.jpg", quality=60)
    return folder


def lightroom_xmp(caption: str = "Harbour at dawn") -> bytes:
    """An XMP packet shaped like Lightroom's: caption, keywords, rating, label."""
    return (
        '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF '
        'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
        '<rdf:Description rdf:about="" xmlns:xmp="http://ns.adobe.com/xap/1.0/" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:tiff="http://ns.adobe.com/tiff/1.0/" '
        'xmp:Rating="4" xmp:Label="Red" tiff:Orientation="1">\n'
        '<dc:description><rdf:Alt><rdf:li xml:lang="x-default">'
        f"{caption}</rdf:li></rdf:Alt></dc:description>\n"
        "<dc:subject><rdf:Bag><rdf:li>harbour</rdf:li><rdf:li>dawn</rdf:li>"
        "</rdf:Bag></dc:subject>\n"
        "</rdf:Description></rdf:RDF></x:xmpmeta>\n"
        '<?xpacket end="w"?>'
    ).encode("utf-8")


def with_iptc(jpeg: bytes, caption: str = "Harbour at dawn") -> bytes:
    """Splice an IPTC caption and keywords into a JPEG, as Photoshop writes them."""

    def dataset(number: int, value: bytes) -> bytes:
        return b"\x1c\x02" + bytes([number]) + len(value).to_bytes(2, "big") + value

    iim = (b"\x1c\x01\x5a\x00\x03\x1b%G"  # 1:90 character set: UTF-8
           + dataset(120, caption.encode()) + dataset(25, b"harbour") + dataset(25, b"dawn"))
    block = b"8BIM\x04\x04\x00\x00" + len(iim).to_bytes(4, "big") + iim + b"\x00" * (len(iim) % 2)
    payload = b"Photoshop 3.0\x00" + block
    return jpeg[:2] + b"\xff\xed" + (len(payload) + 2).to_bytes(2, "big") + payload + jpeg[2:]


def camera_exif(orientation: int = 1) -> Image.Exif:
    """EXIF as a camera writes it: make, model, date taken and a GPS position."""
    exif = Image.Exif()
    exif[0x010F] = "TestMake"
    exif[0x0110] = "TestModel"
    exif[0x0112] = orientation
    exif[0x0132] = "2025:07:26 12:00:00"
    exif.get_ifd(0x8769)[0x9003] = "2025:07:26 11:59:58"  # DateTimeOriginal
    gps = exif.get_ifd(0x8825)
    gps[1] = "N"
    gps[2] = (59.0, 20.0, 0.0)
    return exif
