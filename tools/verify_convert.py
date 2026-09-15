#!/usr/bin/env python3
"""Exercise the converter: real originals, every format, and the tricky cases.

Usage::

    python tools/verify_convert.py [--data DIR] [--sample N]
"""

from __future__ import annotations

import argparse
import errno
import io
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageCms, IptcImagePlugin, JpegImagePlugin, PngImagePlugin

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from minjpg import convert, formats, pipeline, runfolder, scanner  # noqa: E402
from minjpg.common import PART_SUFFIX, PipelineError, copy_atomic, write_atomic  # noqa: E402
from minjpg.config import (  # noqa: E402
    LAYOUT_BESIDE,
    LAYOUT_SUBFOLDER,
    MIN_SUBDIR,
    ConvertSettings,
    Settings,
)

DEFAULT_DATA = Path(__file__).resolve().parents[2] / "example_data"

failures: list[str] = []
checks = 0


def check(condition: bool, description: str) -> bool:
    global checks
    checks += 1
    if not condition:
        failures.append(description)
        print(f"  FAIL  {description}")
        return False
    return True


def section(title: str) -> None:
    print(f"\n=== {title}")


# --------------------------------------------------------------- real originals


def test_real_originals(data: Path, sample: int, out: Path) -> None:
    section(f"Real originals from {data.name}")
    settings = ConvertSettings(max_long_edge=1600, quality=65, max_size=0)
    sources = [p for p in sorted(data.glob("*.jpg")) if not scanner.is_min_file(p)][:sample]
    print(f"{'file':40}{'source':>14}{'output':>14}{'KB':>8}{'q':>4}  status")
    for source in sources:
        result = convert.convert(source, out / f"{source.stem}.jpg", settings)
        src = f"{result.source_size[0]}x{result.source_size[1]}"
        dst = f"{result.output_size[0]}x{result.output_size[1]}"
        print(f"{source.name[:39]:40}{src:>14}{dst:>14}{result.kilobytes:>8.1f}"
              f"{result.quality:>4}  {result.status}")

        long_edge = max(result.output_size)
        check(long_edge <= 1600, f"{source.name}: long edge {long_edge} exceeds 1600")
        src_ratio = result.source_size[0] / result.source_size[1]
        dst_ratio = result.output_size[0] / result.output_size[1]
        check(abs(src_ratio - dst_ratio) < 0.01,
              f"{source.name}: aspect ratio changed {src_ratio:.3f} -> {dst_ratio:.3f}")
        with Image.open(result.output) as written:
            check(written.info.get("progressive") is not None,
                  f"{source.name}: output is not progressive")
            check(JpegImagePlugin.get_sampling(written) == 2,
                  f"{source.name}: output is not 4:2:0")


# ------------------------------------------------------------- format coverage


def test_formats(tmp: Path, out: Path) -> None:
    section("Format coverage")
    settings = ConvertSettings(max_long_edge=800, quality=70, max_size=0)
    rng = np.random.default_rng(3)
    base = Image.fromarray(rng.integers(0, 256, (900, 1200, 3), dtype=np.uint8))

    made: list[tuple[str, Path]] = []
    for name, kwargs in [
        ("png", {}), ("tiff", {}), ("webp", {}), ("bmp", {}), ("gif", {}),
    ]:
        path = tmp / f"sample.{name}"
        base.save(path, **kwargs)
        made.append((name, path))

    alpha = Image.new("RGBA", (900, 600), (255, 0, 0, 0))
    alpha.paste((0, 0, 255, 255), (0, 0, 450, 600))
    alpha_path = tmp / "alpha.png"
    alpha.save(alpha_path)
    made.append(("png+alpha", alpha_path))

    gray_path = tmp / "gray.png"
    base.convert("L").save(gray_path)
    made.append(("grayscale", gray_path))

    cmyk_path = tmp / "cmyk.jpg"
    base.convert("CMYK").save(cmyk_path)
    made.append(("cmyk jpeg", cmyk_path))

    multi_path = tmp / "multi.tif"
    base.save(multi_path, save_all=True, append_images=[base.transpose(Image.ROTATE_180)])

    # 16-bit greyscale, which convert() would clip to white rather than scale.
    grey16 = np.full((600, 900), 20000, dtype=np.uint16)  # ~30% grey
    grey16[:, 450:] = 50000  # ~76% grey
    for ext in ("png", "tif"):
        path = tmp / f"grey16.{ext}"
        Image.fromarray(grey16).save(path)
        made.append((f"16-bit {ext}", path))

    if formats.HEIF_AVAILABLE:
        heic_path = tmp / "sample.heic"
        base.save(heic_path, quality=90)
        made.append(("heic", heic_path))
    else:
        print("  (HEIC skipped: pillow-heif not installed)")

    for label, path in made:
        try:
            result = convert.convert(path, out / f"fmt-{path.stem}-{label[:4]}.jpg", settings)
        except Exception as exc:
            check(False, f"{label}: conversion raised {exc}")
            continue
        with Image.open(result.output) as written:
            mode_ok = written.mode == "RGB"
        print(f"  {label:16} -> {result.output_size[0]}x{result.output_size[1]} "
              f"{result.kilobytes:6.1f} KB  q{result.quality}")
        check(mode_ok, f"{label}: output mode is not RGB")
        check(max(result.output_size) <= 800, f"{label}: long edge cap ignored")

    # alpha must land on white, not black
    flat = convert.convert(alpha_path, out / "alpha-check.jpg", settings)
    with Image.open(flat.output) as written:
        corner = written.convert("RGB").getpixel((written.width - 4, 4))
    check(min(corner) > 200, f"alpha flattened onto {corner}, expected near-white")

    pa = convert.flatten_alpha(Image.new("PA", (4, 4)))  # palette + alpha, fully clear
    check(pa.mode == "RGB" and pa.getpixel((0, 0)) == (255, 255, 255),
          f"PA transparency flattened onto {pa.getpixel((0, 0))}, expected white")

    for ext in ("png", "tif"):
        r = convert.convert(tmp / f"grey16.{ext}", out / f"grey16-{ext}.jpg", settings)
        with Image.open(r.output) as written:
            grey = np.asarray(written.convert("L"), dtype=float)
        left, right = grey[:, :300].mean(), grey[:, -300:].mean()
        print(f"  16-bit {ext}: {left:.0f}/{right:.0f} (expected ~78/~195), notes={r.notes!r}")
        check(abs(left - 78) <= 4 and abs(right - 195) <= 4,
              f"16-bit {ext} came out {left:.0f}/{right:.0f}, expected about 78/195")
        check("8 bits" in r.notes, "reducing a 16-bit source should be noted")

    # Pages and animation: a JPEG holds one picture, so these are handed back
    # to be kept whole (the batch copies the original) rather than cut down.
    anim = tmp / "anim.gif"
    base.save(anim, save_all=True, append_images=[base.transpose(Image.ROTATE_180)])
    for label, path in (("multipage tiff", multi_path), ("animated gif", anim)):
        try:
            convert.convert(path, out / f"multi-{path.stem}.jpg", settings)
            check(False, f"{label}: converting should hand it back to be kept whole")
        except convert.KeepOriginal as exc:
            print(f"  {label:16} -> kept whole: {exc}")
        check(not (out / f"multi-{path.stem}.jpg").exists(), f"{label}: a first-frame JPEG was written")
    thumb = pipeline.compress(multi_path, out / "multi_min.jpg", Settings())
    check(thumb.output.is_file(), "a multi-page TIFF should still get a thumbnail of page one")


WIDE_GAMUT_CANDIDATES = [
    "/usr/share/color/icc/colord/AdobeRGB1998.icc",
    "/usr/share/color/icc/colord/ProPhotoRGB.icc",
    "/usr/share/color/icc/ghostscript/a98.icc",
]
CMYK_CANDIDATES = [
    "/usr/share/color/icc/colord/FOGRA39L_coated.icc",
    "/usr/share/color/icc/colord/SWOP_TR005_coated_5.icc",
    "/usr/share/color/icc/ghostscript/default_cmyk.icc",
]
GREY_CANDIDATES = [
    "/usr/share/color/icc/ghostscript/default_gray.icc",
    "/usr/share/color/icc/ghostscript/ps_gray.icc",
]


def test_wide_gamut(tmp: Path, out: Path) -> None:
    section("Wide-gamut colour conversion")
    settings = ConvertSettings(max_long_edge=600, quality=90, max_size=0)
    colour = (0, 200, 90)  # saturated green - well outside sRGB in Adobe RGB terms

    profile_path = next((p for p in WIDE_GAMUT_CANDIDATES if Path(p).is_file()), None)
    if profile_path:
        profile = Path(profile_path).read_bytes()
        source = tmp / "wide.jpg"
        # Tagged the way an Adobe RGB camera or export tags it, so the
        # colour-space facts can be seen to change along with the pixels.
        wide_exif = Image.Exif()
        wide_exif.get_ifd(0x8769)[0xA001] = 0xFFFF  # ColorSpace: uncalibrated
        wide_xmp = (
            '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF '
            'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description '
            'rdf:about="" xmlns:photoshop="http://ns.adobe.com/photoshop/1.0/" '
            'xmlns:exif="http://ns.adobe.com/exif/1.0/" '
            'photoshop:ICCProfile="Adobe RGB (1998)" exif:ColorSpace="65535"/>'
            "</rdf:RDF></x:xmpmeta>"
        ).encode()
        Image.new("RGB", (600, 400), colour).save(
            source, icc_profile=profile, exif=wide_exif, xmp=wide_xmp, quality=98
        )

        # What the numbers should become once interpreted in the wide space.
        expected = ImageCms.profileToProfile(
            Image.new("RGB", (8, 8), colour),
            ImageCms.getOpenProfile(profile_path),
            ImageCms.createProfile("sRGB"),
            outputMode="RGB",
        ).getpixel((4, 4))

        result = convert.convert(source, out / "wide.jpg", settings)
        with Image.open(result.output) as written:
            got = written.convert("RGB").getpixel((300, 200))
        name = ImageCms.getProfileDescription(ImageCms.getOpenProfile(profile_path)).strip()
        print(f"  {colour} tagged {name!r}")
        print(f"    expected sRGB {expected}, got {got}")
        check(result.converted_colour, "a wide-gamut profile should be reported as converted")
        check(all(abs(a - b) <= 4 for a, b in zip(got, expected)),
              f"wide-gamut conversion gave {got}, expected about {expected}")
        # Also confirm a transform actually ran rather than the numbers being
        # copied through: Adobe RGB green shifts ~10 in blue, so 4 is a safe bar.
        check(any(abs(a - b) >= 4 for a, b in zip(got, colour)),
              f"output {got} is unchanged from the raw numbers - no conversion happened")
        with Image.open(result.output) as written:
            wide_out = (written.info.get("xmp") or b"").decode("utf-8")
            colour_space = written.getexif().get_ifd(0x8769).get(0xA001)
        print(f"  colour-space facts after conversion: EXIF {colour_space}, "
              f"XMP ICCProfile {'sRGB' if 'sRGB IEC61966-2.1' in wide_out else 'unchanged'}")
        check(colour_space == 1, f"EXIF ColorSpace is {colour_space} after converting to sRGB")
        check('photoshop:ICCProfile="sRGB IEC61966-2.1"' in wide_out
              and 'exif:ColorSpace="1"' in wide_out,
              "the XMP still names the profile the colours were converted away from")
    else:
        print("  (no wide-gamut ICC profile on this system, skipping)")

    # An sRGB-tagged image must pass through untouched
    srgb_path = tmp / "srgb.jpg"
    Image.new("RGB", (600, 400), colour).save(
        srgb_path, icc_profile=ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes(),
        quality=98,
    )
    result = convert.convert(srgb_path, out / "srgb.jpg", settings)
    with Image.open(result.output) as written:
        got = written.convert("RGB").getpixel((300, 200))
    print(f"  sRGB-tagged {colour} -> {got} (converted={result.converted_colour})")
    check(not result.converted_colour, "an sRGB profile should not be reported as converted")
    check(all(abs(a - b) < 8 for a, b in zip(got, colour)),
          f"sRGB-tagged colour drifted to {got}")

    # CMYK and greyscale profiles describe CMYK and greyscale numbers.  They
    # used to be applied after a naive RGB conversion, which always failed.
    for label, candidates, mode, value in (
        ("CMYK", CMYK_CANDIDATES, "CMYK", (20, 180, 40, 10)),
        ("grey", GREY_CANDIDATES, "L", 90),
    ):
        found = next((p for p in candidates if Path(p).is_file()), None)
        if not found:
            print(f"  (no {label} ICC profile on this system, skipping)")
            continue
        tagged = tmp / f"tagged-{label}.jpg"
        Image.new(mode, (600, 400), value).save(
            tagged, icc_profile=Path(found).read_bytes(), quality=98
        )
        expected = ImageCms.profileToProfile(
            Image.new(mode, (8, 8), value), ImageCms.getOpenProfile(found),
            ImageCms.createProfile("sRGB"), outputMode="RGB",
        ).getpixel((4, 4))
        r = convert.convert(tagged, out / f"tagged-{label}.jpg", settings)
        with Image.open(r.output) as written:
            got = written.convert("RGB").getpixel((300, 200))
        print(f"  {label} {value} via {Path(found).name}: expected {expected}, got {got}")
        check(r.converted_colour, f"a {label} profile should be applied, not ignored")
        check(all(abs(a - b) <= 6 for a, b in zip(got, expected)),
              f"{label} conversion gave {got}, expected about {expected}")

    # An intentionally corrupt profile must not fail the file
    broken = tmp / "broken.jpg"
    Image.new("RGB", (600, 400), colour).save(broken, icc_profile=b"not-a-profile")
    try:
        r = convert.convert(broken, out / "broken.jpg", settings)
        print(f"  corrupt ICC profile handled: {r.output_size} {r.kilobytes:.1f} KB")
        check(not r.converted_colour, "a corrupt profile cannot count as converted")
    except Exception as exc:
        check(False, f"corrupt ICC profile raised {exc}")


# ------------------------------------------------------------------- metadata


def test_metadata(tmp: Path, out: Path) -> None:
    section("Metadata handling")
    rng = np.random.default_rng(5)
    image = Image.fromarray(rng.integers(0, 256, (600, 900, 3), dtype=np.uint8))

    exif = Image.Exif()
    exif[0x010F] = "TestMake"           # Make
    exif[0x0110] = "TestModel"          # Model
    exif[0x0112] = 6                    # Orientation: rotate 90 CW
    exif[0x0132] = "2025:07:26 12:00:00"  # DateTime
    exif[0x0201] = 1234                 # stale thumbnail offset
    gps = exif.get_ifd(0x8825)
    gps[1] = "N"
    gps[2] = (59.0, 20.0, 0.0)
    source = tmp / "meta.jpg"
    image.save(source, exif=exif, quality=95)

    with Image.open(source) as opened:
        src_size = opened.size
        src_orientation = opened.getexif().get(0x0112)
    print(f"  source {src_size}, orientation {src_orientation}, GPS present")

    keep = ConvertSettings(max_long_edge=400, quality=70, max_size=0,
                           strip_metadata=False, passthrough=False)
    result = convert.convert(source, out / "meta-keep.jpg", keep)
    with Image.open(result.output) as written:
        kept = written.getexif()
        out_size = written.size
        sub = kept.get_ifd(0x8769)
        kept_gps = kept.get_ifd(0x8825)
    print(f"  kept:  {out_size} tags={len(kept)} make={kept.get(0x010F)!r} "
          f"orientation={kept.get(0x0112)} gps_tags={len(kept_gps)}")
    check(kept.get(0x010F) == "TestMake", "Make tag was lost")
    check(kept.get(0x0132) == "2025:07:26 12:00:00", "DateTime tag was lost")
    check(kept.get(0x0112) == 1,
          f"orientation is {kept.get(0x0112)}, must be 1 after baking in rotation")
    check((kept.get(0x0100), kept.get(0x0101)) == out_size,
          f"EXIF dimensions {kept.get(0x0100)}x{kept.get(0x0101)} != output {out_size}")
    check(0x0201 not in kept, "stale thumbnail pointer survived")
    check(len(kept_gps) > 0, "GPS was dropped but the user asked to keep it")
    if sub:
        check(sub.get(0xA002, out_size[0]) == out_size[0],
              f"PixelXDimension {sub.get(0xA002)} != {out_size[0]}")

    # orientation 6 means the pixels must come out rotated: portrait source
    check(out_size[0] < out_size[1],
          f"orientation 6 was not applied: output {out_size} is not portrait")

    # XMP and IPTC: where Lightroom, Bridge, Capture One and the like put a
    # caption, keywords or a rating.  Both are kept, with the facts the
    # conversion changes brought up to date.
    def lightroom_xmp(extra_attributes: str = "", caption: str = "Harbour at dawn") -> bytes:
        return (
            '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>\n'
            '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF '
            'xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">\n'
            '<rdf:Description rdf:about="" xmlns:xmp="http://ns.adobe.com/xap/1.0/" '
            'xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:lr="http://ns.adobe.com/lightroom/1.0/" '
            'xmlns:tiff="http://ns.adobe.com/tiff/1.0/" '
            'xmlns:exif="http://ns.adobe.com/exif/1.0/" '
            'xmlns:crs="http://ns.adobe.com/camera-raw-settings/1.0/" '
            'xmp:Rating="4" xmp:Label="Red" tiff:Orientation="6" '
            'exif:PixelXDimension="900" exif:PixelYDimension="600" '
            f'crs:Exposure2012="+0.50"{extra_attributes}>\n'
            '<dc:description><rdf:Alt><rdf:li xml:lang="x-default">'
            f"{caption}</rdf:li></rdf:Alt></dc:description>\n"
            "<dc:subject><rdf:Bag><rdf:li>harbour</rdf:li><rdf:li>dawn</rdf:li>"
            "</rdf:Bag></dc:subject>\n"
            "<lr:hierarchicalSubject><rdf:Bag><rdf:li>Places|Harbour</rdf:li>"
            "</rdf:Bag></lr:hierarchicalSubject>\n"
            "</rdf:Description></rdf:RDF></x:xmpmeta>\n"
            + " " * 2048 + '\n<?xpacket end="w"?>'
        ).encode("utf-8")

    def with_iptc(jpeg: bytes) -> bytes:
        """Splice in an APP13 block the way Photoshop and Lightroom write one."""
        def dataset(number: int, value: bytes) -> bytes:
            return b"\x1c\x02" + bytes([number]) + len(value).to_bytes(2, "big") + value

        iim = (b"\x1c\x01\x5a\x00\x03\x1b%G"  # 1:90 character set: UTF-8
               + dataset(120, "Harbour at dawn".encode()) + dataset(25, b"harbour")
               + dataset(25, b"dawn"))
        block = b"8BIM\x04\x04\x00\x00" + len(iim).to_bytes(4, "big") + iim + b"\x00" * (len(iim) % 2)
        payload = b"Photoshop 3.0\x00" + block
        return jpeg[:2] + b"\xff\xed" + (len(payload) + 2).to_bytes(2, "big") + payload + jpeg[2:]

    def metadata_order(path: Path) -> list[str]:
        """The metadata segments before the image data, read straight off the bytes."""
        data, found, position = path.read_bytes(), [], 2
        while data[position] == 0xFF and data[position + 1] not in (0xDA, 0xD9):
            marker = data[position + 1]
            head = data[position + 4 : position + 33]
            if marker == 0xE1:
                found.append("EXIF" if head.startswith(b"Exif") else "XMP")
            elif marker == 0xED:
                found.append("IPTC")
            position += 2 + int.from_bytes(data[position + 2 : position + 4], "big")
        return found

    def png_with_xmp(path: Path, packet: bytes) -> None:
        info = PngImagePlugin.PngInfo()
        info.add_itxt("XML:com.adobe.xmp", packet.decode("utf-8"))
        image.save(path, pnginfo=info)

    tagged = tmp / "lightroom.jpg"
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", exif=exif, xmp=lightroom_xmp(), quality=95)
    tagged.write_bytes(with_iptc(buffer.getvalue()))
    r = convert.convert(tagged, out / "lightroom.jpg", keep)
    with Image.open(r.output) as written:
        xmp_out = (written.info.get("xmp") or b"").decode("utf-8")
        iptc_out = IptcImagePlugin.getiptcinfo(written) or {}
        lr_size = written.size
    print(f"  Lightroom-style source: kept {r.kept_metadata!r}, {lr_size}, notes={r.notes!r}")
    for expected in ("Harbour at dawn", "<rdf:li>harbour</rdf:li>", 'xmp:Rating="4"',
                     'xmp:Label="Red"', "Places|Harbour", 'crs:Exposure2012="+0.50"'):
        check(expected in xmp_out, f"the XMP lost {expected!r}")
    check('tiff:Orientation="1"' in xmp_out, "XMP orientation must be 1 once the rotation is baked in")
    check(f'exif:PixelXDimension="{lr_size[0]}"' in xmp_out
          and f'exif:PixelYDimension="{lr_size[1]}"' in xmp_out,
          "the XMP dimensions must match the output")
    check(" " * 100 not in xmp_out, "the XMP editing padding should be trimmed")
    check(iptc_out.get((2, 120)) == b"Harbour at dawn", f"IPTC caption is {iptc_out.get((2, 120))!r}")
    check(iptc_out.get((2, 25)) == [b"harbour", b"dawn"], f"IPTC keywords are {iptc_out.get((2, 25))!r}")
    check(r.kept_metadata == "EXIF, XMP, IPTC", f"kept {r.kept_metadata!r}")
    order = metadata_order(r.output)
    check(order == ["EXIF", "XMP", "IPTC"], f"metadata segments are {order}")

    stripped_lr = convert.convert(
        tagged, out / "lightroom-strip.jpg",
        ConvertSettings(max_long_edge=400, quality=70, max_size=0, strip_metadata=True),
    )
    check(metadata_order(stripped_lr.output) == [],
          "'Remove all metadata' left EXIF, XMP or IPTC behind")

    # Every format that carries XMP hands it on.
    carriers = [("webp", {"xmp": lightroom_xmp()}), ("tif", {"tiffinfo": {700: lightroom_xmp()}})]
    if formats.HEIF_AVAILABLE:
        carriers.append(("heic", {"xmp": lightroom_xmp(), "quality": 90}))
    png_with_xmp(tmp / "lightroom.png", lightroom_xmp())
    for ext, kwargs in carriers:
        image.save(tmp / f"lightroom.{ext}", **kwargs)
    for ext in [ext for ext, _ in carriers] + ["png"]:
        r = convert.convert(tmp / f"lightroom.{ext}", out / f"lightroom-{ext}.jpg", keep)
        with Image.open(r.output) as written:
            carried = (written.info.get("xmp") or b"").decode("utf-8")
        check("Harbour at dawn" in carried and 'xmp:Rating="4"' in carried,
              f"{ext}: the XMP was not carried across (kept {r.kept_metadata!r})")
    print(f"  XMP carried across from {', '.join([e for e, _ in carriers] + ['png'])}")

    # Too big for one JPEG segment: the bulk nobody typed goes, the caption,
    # keywords and rating stay - and the row says what happened.
    curve = ' crs:ToneCurvePV2012="' + "0, 0, " * 14000 + '"'
    png_with_xmp(tmp / "lightroom-big.png", lightroom_xmp(curve))
    r = convert.convert(tmp / "lightroom-big.png", out / "lightroom-big.jpg", keep)
    with Image.open(r.output) as written:
        slim = (written.info.get("xmp") or b"").decode("utf-8")
    print(f"  oversized XMP: kept {r.kept_metadata!r}, notes={r.notes!r}")
    check("Harbour at dawn" in slim and 'xmp:Rating="4"' in slim and "harbour" in slim,
          "shrinking the XMP lost the caption, keywords or rating")
    check("ToneCurvePV2012" not in slim and "develop settings" in r.notes,
          "the bulky develop settings should go, and the row should say so")
    png_with_xmp(tmp / "lightroom-huge.png", lightroom_xmp(caption="x" * 70_000))
    r = convert.convert(tmp / "lightroom-huge.png", out / "lightroom-huge.jpg", keep)
    check("XMP could not be kept" in r.notes and "XMP" not in r.kept_metadata,
          f"XMP that cannot fit must be reported: {r.notes!r}")

    # EXIF too big for a JPEG's APP1 segment cannot be kept - and says so.
    bulky = Image.Exif()
    bulky[0x010E] = "x" * 70_000  # ImageDescription
    bulky_path = tmp / "bulky-exif.png"
    image.save(bulky_path, exif=bulky)
    r = convert.convert(bulky_path, out / "bulky.jpg", keep)
    print(f"  oversized EXIF: kept={r.metadata_kept} notes={r.notes!r}")
    check(not r.metadata_kept and "EXIF could not be kept" in r.notes,
          "dropping EXIF the user asked to keep must be reported")

    strip = ConvertSettings(max_long_edge=400, quality=70, max_size=0,
                            strip_metadata=True)
    stripped = convert.convert(source, out / "meta-strip.jpg", strip)
    with Image.open(stripped.output) as written:
        remaining = written.getexif()
        has_icc = bool(written.info.get("icc_profile"))
    print(f"  strip: tags={len(remaining)} icc={has_icc} "
          f"bytes={stripped.byte_size} (kept was {result.byte_size})")
    check(len(remaining) == 0, f"strip_metadata left {len(remaining)} EXIF tags")
    check(not has_icc, "strip_metadata left an ICC profile")


# ------------------------------------------------------- cap and passthrough


def test_cap_and_passthrough(tmp: Path, out: Path) -> None:
    section("Size cap and passthrough")
    rng = np.random.default_rng(11)
    noisy = tmp / "noisy.png"
    Image.fromarray(rng.integers(0, 256, (2000, 3000, 3), dtype=np.uint8)).save(noisy)

    fits = ConvertSettings(max_long_edge=1600, quality=90, max_size=300_000)
    r = convert.convert(noisy, out / "cap-fits.jpg", fits)
    print(f"  cap 300 KB: {r.output_size} {r.kilobytes:.1f} KB q{r.quality} "
          f"over_cap={r.over_cap}")
    check(not r.over_cap and r.byte_size <= 300_000,
          f"searched result {r.byte_size} should be under the 300 KB cap")
    check(r.quality < 90, "quality should have been searched down to meet the cap")

    tight = ConvertSettings(max_long_edge=1600, quality=90, max_size=40_000,
                            quality_floor=40)
    r = convert.convert(noisy, out / "cap-over.jpg", tight)
    print(f"  cap  40 KB: {r.output_size} {r.kilobytes:.1f} KB q{r.quality} "
          f"over_cap={r.over_cap} notes={r.notes!r}")
    check(r.over_cap, "impossible cap should be flagged over_cap")
    check(r.output.is_file() and r.byte_size > 40_000,
          "an over-cap file must still be written")
    check(r.quality == 40, f"over-cap result should sit at the floor, got q{r.quality}")
    check("quality floor 40" in r.notes,
          f"the note should blame the floor the search actually reached: {r.notes!r}")

    # A per-image override skips the search, so the note must name the quality
    # the file really was encoded at rather than a floor it never went near.
    r = convert.convert(noisy, out / "cap-over-override.jpg", tight, quality=95)
    print(f"  override q95: {r.kilobytes:.1f} KB q{r.quality} notes={r.notes!r}")
    check(r.quality == 95, f"the override quality should be used, got q{r.quality}")
    check(r.over_cap, "an over-cap override should still be flagged")
    check("95" in r.notes and "floor" not in r.notes,
          f"the note names q95, not a quality floor: {r.notes!r}")

    # passthrough: a JPEG already inside both limits
    small = Image.fromarray(rng.integers(0, 256, (400, 600, 3), dtype=np.uint8))
    already = tmp / "already.jpg"
    small.save(already, quality=80)
    settings = ConvertSettings(max_long_edge=1600, quality=65,
                               max_size=614_400)
    r = convert.convert(already, out / "already.jpg", settings)
    identical = r.output.read_bytes() == already.read_bytes()
    print(f"  passthrough: copied={r.copied} identical_bytes={identical}")
    check(r.copied, "an in-limits JPEG should be copied, not re-encoded")
    check(identical, "a copied file must be byte-identical to the source")

    r = convert.convert(already, out / "already-strip.jpg",
                        ConvertSettings(max_long_edge=1600, quality=65,
                                        max_size=614_400, strip_metadata=True))
    check(not r.copied, "passthrough must be off when metadata is being stripped")
    r = convert.convert(already, out / "already-override.jpg", settings, quality=50)
    check(not r.copied, "an explicit quality override must force a re-encode")
    print("  passthrough correctly disabled for strip_metadata and overrides")

    # Passthrough trusts the content, not the name: a PNG called .jpg and a
    # CMYK JPEG are re-encoded rather than copied as they are.
    misnamed = tmp / "misnamed.jpg"
    small.save(misnamed, format="PNG")
    cmyk = tmp / "cmyk-small.jpg"
    small.convert("CMYK").save(cmyk, quality=80)
    for path in (misnamed, cmyk):
        r = convert.convert(path, out / f"pt-{path.name}", settings)
        with Image.open(r.output) as written:
            web_ready = written.format == "JPEG" and written.mode == "RGB"
        check(not r.copied and web_ready,
              f"{path.name}: copied={r.copied}; the output must be an RGB JPEG")
    print("  passthrough refused for a PNG named .jpg and for a CMYK JPEG")

    # Quality below the (hidden) floor: the floor gives way instead of the
    # tab refusing a quality nobody can see the reason for.
    low = ConvertSettings(max_long_edge=1600, quality=30, max_size=40_000,
                          quality_floor=40)
    low.validate()
    r = convert.convert(noisy, out / "low-quality.jpg", low)
    print(f"  quality 30 under a floor of 40: q{r.quality} notes={r.notes!r}")
    check(r.quality == 30, f"asking for quality 30 used q{r.quality}")
    check(not r.over_cap or "floor 30" in r.notes, f"the note names the floor used: {r.notes!r}")

    unreadable = tmp / "broken.png"
    unreadable.write_bytes(b"not an image at all")
    try:
        convert.convert(unreadable, out / "unreadable.jpg", settings)
        check(False, "an unreadable file should raise PipelineError")
    except PipelineError as exc:
        print(f"  unreadable file raises PipelineError: {str(exc)[:60]}")
        check(not (out / "unreadable.jpg").exists(), "no file should be written on failure")


# ---------------------------------------------------------------- scanner rules


def snapshot(root: Path) -> dict[Path, tuple[int, int]]:
    """Size and mtime of every file under ``root``, for an untouched check."""
    return {
        p: (p.stat().st_size, p.stat().st_mtime_ns)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def build_tree(root: Path, seed: int) -> None:
    """A small input tree with images, non-images and an empty folder."""
    (root / "sub").mkdir(parents=True)
    (root / "empty").mkdir(parents=True)
    rng = np.random.default_rng(seed)
    tiny = Image.fromarray(rng.integers(0, 256, (60, 80, 3), dtype=np.uint8))
    tiny.save(root / "photo.png")
    tiny.save(root / "photo.tif")
    tiny.save(root / "other.jpg")
    tiny.save(root / "sub" / "nested.png")
    (root / "notes.txt").write_text("keep me")
    (root / "sub" / "clip.bin").write_bytes(b"\x00\x01" * 512)


def run_jobs(result: scanner.ScanResult, runner) -> None:
    """Execute a scan the way the worker thread would."""
    for directory in result.empty_dirs:
        directory.mkdir(parents=True, exist_ok=True)
    for job in result.jobs:
        if job.action == scanner.COPY:
            copy_atomic(job.source, job.output)
        else:
            runner(job)


# --------------------------------------------------------------- run folders


def test_run_folder(tmp: Path) -> None:
    section("Run folder naming and creation")
    parent = tmp / "run-parent"
    parent.mkdir()
    source = tmp / "photos"
    source.mkdir()

    when = datetime(2026, 8, 27, 14, 32)
    base = runfolder.base_name(source, "min", when)
    print(f"  base name: {base}")
    check(base == "photos_min_2026-08-27_1432", f"unexpected base name {base}")
    check(runfolder.base_name(source, "compress", when).endswith("_compressed_2026-08-27_1432"),
          "the compress job should name itself 'compressed'")

    # 1. a free name is used as-is, and nothing is created by planning alone
    first = runfolder.plan(parent, source, "min", when)
    check(not first.collided, "a free name should not report a collision")
    check(not first.path.exists(), "plan() must not create anything")
    runfolder.create(first)
    check(first.path.is_dir(), "create() did not make the folder")

    # 2. second and third runs in the same minute step aside
    second = runfolder.create(runfolder.plan(parent, source, "min", when))
    third = runfolder.create(runfolder.plan(parent, source, "min", when))
    print(f"  three runs -> {[p.name for p in (first.path, second.path, third.path)]}")
    check(second.collided and second.path.name == f"{base}_2", f"expected _2, got {second.path.name}")
    check(third.collided and third.path.name == f"{base}_3", f"expected _3, got {third.path.name}")

    # 3. a plain FILE with the name also forces a suffix
    file_parent = tmp / "file-parent"
    file_parent.mkdir()
    (file_parent / base).write_text("not a folder")
    blocked = runfolder.plan(file_parent, source, "min", when)
    print(f"  file in the way -> {blocked.path.name}")
    check(blocked.path.name == f"{base}_2", "a file with the base name should force a suffix")
    runfolder.create(blocked)
    check((file_parent / base).read_text() == "not a folder",
          "the file that was in the way was modified")

    # 4. a dangling symlink is not a free name either
    link_parent = tmp / "link-parent"
    link_parent.mkdir()
    (link_parent / base).symlink_to(tmp / "nowhere-at-all")
    dangling = runfolder.plan(link_parent, source, "min", when)
    print(f"  dangling symlink -> {dangling.path.name}")
    check(dangling.path.name == f"{base}_2",
          "a dangling symlink must not be treated as a free name")

    # 5. create() must never land inside a folder that appeared after planning
    race_parent = tmp / "race-parent"
    race_parent.mkdir()
    reserved = runfolder.plan(race_parent, source, "min", when)
    (reserved.path).mkdir()  # something else got there first
    (reserved.path / "someones-file.txt").write_text("precious")
    made = runfolder.create(reserved)
    print(f"  lost the race -> {made.path.name}")
    check(made.path != reserved.path, "create() reused a folder that appeared after planning")
    check(not any(made.path.iterdir()), "the folder create() returned is not empty")
    check((reserved.path / "someones-file.txt").read_text() == "precious",
          "create() wrote into the folder that was already there")

    # 6. an empty run folder is cleaned up, a non-empty one never is
    keep = runfolder.create(runfolder.plan(parent, source, "compress", when))
    (keep.path / "result.jpg").write_bytes(b"data")
    check(not runfolder.discard_if_empty(keep.path), "a non-empty run folder must not be removed")
    check((keep.path / "result.jpg").exists(), "discard_if_empty deleted a file")
    empty = runfolder.create(runfolder.plan(parent, source, "compress", when))
    check(runfolder.discard_if_empty(empty.path), "an empty run folder should be removed")

    # 6b. a run folder holding only empty folders goes entirely; one with a
    #     file anywhere inside it stays, file and all
    hollow = runfolder.create(runfolder.plan(parent, source, "compress", when))
    (hollow.path / "a" / "b").mkdir(parents=True)
    check(runfolder.discard_empty_tree(hollow.path), "a run folder of empty folders should go")
    solid = runfolder.create(runfolder.plan(parent, source, "compress", when))
    (solid.path / "a" / "b").mkdir(parents=True)
    (solid.path / "a" / "b" / "f.txt").write_text("x")
    check(not runfolder.discard_empty_tree(solid.path)
          and (solid.path / "a" / "b" / "f.txt").read_text() == "x",
          "discard_empty_tree removed a folder with a file in it")

    # 7. names stay legal on Windows
    awkward = tmp / "a:b*c?"
    awkward.mkdir()
    name = runfolder.base_name(awkward, "min", when)
    print(f"  awkward name -> {name}")
    check(not any(c in name for c in '<>:"/\\|?*'), f"illegal character survived in {name}")


# ---------------------------------------------------------------- folder guards


def test_folder_guards(tmp: Path) -> None:
    section("Input/output folder guards")
    root = tmp / "guard-in"
    build_tree(root, 11)
    outside = tmp / "guard-out"
    outside.mkdir()

    # An output folder inside the input is the one a user actually hits: point the
    # output at the folder being read and a second run re-processes its own results.
    nested_in = outside / "nested-in"
    nested_in.mkdir()
    for source, output, description in [
        (root, root, "output equal to input"),
        (root, root / "inside" / "run", "output inside input"),
        (nested_in, outside, "input inside output"),
    ]:
        try:
            scanner.check_folders(source, output)
            check(False, f"{description} should be refused")
        except scanner.ScanError as exc:
            print(f"  refused: {description} ({str(exc)[:50]}…)")
            check(True, "")

    # A run folder that is merely a sibling of the input is fine, and is what the
    # common "output folder is the input's parent" choice produces.
    for source, output, description in [
        (root, outside / "run", "an unrelated output folder"),
        (root, root.parent / f"{root.name}_min_2026-08-27_1432", "a sibling of the input"),
    ]:
        scanner.check_folders(source, output)
        print(f"  accepted: {description}")
        check(True, "")


# ---------------------------------------------------------------- scanner rules


def test_scanner(tmp: Path) -> None:
    section("Scanner rules - compress tab")
    root = tmp / "scan-in"
    build_tree(root, 13)
    run = tmp / "scan-out" / "run"

    settings = ConvertSettings(recursive=True)
    result = scanner.scan_compress(root, run, settings)
    names = sorted(str(job.output.relative_to(run)) for job in result.jobs)
    print(f"  jobs: {names}")
    print(f"  warnings: {result.warnings}")
    check(result.to_process == 4, f"expected 4 images, got {result.to_process}")
    check(result.to_copy == 2, f"expected 2 copies (notes.txt, clip.bin), got {result.to_copy}")
    check(any(n.endswith("-2.jpg") for n in names),
          "the photo.png / photo.tif clash should produce a -2 name")
    check("sub/nested.jpg" in names, "subfolder structure was not mirrored")
    check("notes.txt" in names and "sub/clip.bin" in names,
          "non-image files should be copied across")
    check([d.name for d in result.empty_dirs] == ["empty"],
          f"the empty subfolder should be mirrored, got {result.empty_dirs}")

    # Where each original goes if it cannot be converted - reserved at scan
    # time, so the stand-in copy can never land on another job's output.
    by_source = {str(j.source.relative_to(root)): j for j in result.jobs}
    other = by_source["other.jpg"]
    check(other.fallback == other.output, "a JPEG falls back onto its own output name")
    check(by_source["photo.png"].fallback == run / "photo.png",
          f"photo.png falls back to its own name, got {by_source['photo.png'].fallback}")
    check(all(j.fallback is None for j in result.jobs if j.action == scanner.COPY),
          "copies need no fallback")
    taken = [j.output for j in result.jobs] + [
        j.fallback for j in result.jobs if j.fallback and j.fallback != j.output
    ]
    check(len({str(p).casefold() for p in taken}) == len(taken),
          "an output and a fallback were given the same name")

    # The incomplete-run marker's name is reserved in every mirror.
    marked = tmp / "scan-marker"
    marked.mkdir()
    (marked / runfolder.MARKER_NAME).write_text("the user's own file")
    marked_result = scanner.scan_compress(marked, tmp / "scan-out" / "marked", ConvertSettings())
    names = [j.output.name for j in marked_result.jobs]
    print(f"  a file named like the marker -> {names}")
    check(runfolder.MARKER_NAME not in names and len(names) == 1,
          "a mirrored file was allowed to take the marker's name")

    # This app's own finished thumbnails are not source material for it either:
    # re-encoding a 70 KB _min.jpg at quality 65 only costs a second generation.
    # They ride along as copies, so the mirror is still complete.
    mixed = tmp / "scan-mixed"
    (mixed / "sub").mkdir(parents=True)
    tiny = Image.fromarray(
        np.random.default_rng(14).integers(0, 256, (60, 80, 3), dtype=np.uint8)
    )
    for name in ("photo.jpg", "photo_min.jpg", "sub/nested_min.jpg"):
        tiny.save(mixed / name)
    mixed_run = tmp / "scan-out" / "mixed"
    mixed_result = scanner.scan_compress(mixed, mixed_run, ConvertSettings())
    actions = {
        str(job.output.relative_to(mixed_run)): job.action for job in mixed_result.jobs
    }
    print(f"  _min inputs: {actions}")
    check(actions.get("photo.jpg") == scanner.PROCESS,
          f"a normal photo is still compressed, got {actions.get('photo.jpg')}")
    check(actions.get("photo_min.jpg") == scanner.COPY,
          f"an existing _min.jpg must be copied, not re-encoded: {actions.get('photo_min.jpg')}")
    check(actions.get("sub/nested_min.jpg") == scanner.COPY,
          "the never-compress-the-compressed rule applies in subfolders too")
    check(len(actions) == 3, f"no file was dropped from the mirror: {sorted(actions)}")

    # non-recursive must not promise folders it never looked in
    shallow = scanner.scan_compress(root, run, ConvertSettings(recursive=False))
    check(not shallow.empty_dirs,
          "a non-recursive scan must not mirror subfolders it ignored")
    check(all("/" not in str(j.output.relative_to(run)) for j in shallow.jobs),
          "a non-recursive scan should stay at the top level")

    # Folders the walk cannot enter are reported, and are not mirrored as
    # empty folders that would pass for complete ones.
    odd = tmp / "scan-odd"
    (odd / "real").mkdir(parents=True)
    (odd / "real" / "keep.txt").write_text("x")
    elsewhere = tmp / "scan-elsewhere"
    elsewhere.mkdir()
    (elsewhere / "outside.txt").write_text("x")
    (odd / "linked").symlink_to(elsewhere, target_is_directory=True)
    locked = odd / "locked"
    locked.mkdir()
    (locked / "hidden.txt").write_text("x")
    can_lock = os.name != "nt" and os.geteuid() != 0
    if can_lock:
        locked.chmod(0o000)
    try:
        odd_run = tmp / "scan-out" / "odd"
        odd_result = scanner.scan_compress(odd, odd_run, ConvertSettings())
    finally:
        locked.chmod(0o755)
    outputs = sorted(j.output.name for j in odd_result.jobs)
    mirrored = sorted(str(p.relative_to(odd_run)) for p in odd_result.empty_dirs)
    print(f"  linked/locked folders: jobs={outputs} empty_dirs={mirrored}")
    print(f"    warnings={odd_result.warnings}")
    check("keep.txt" in outputs and "outside.txt" not in outputs, "a linked folder was followed")
    check("linked" not in mirrored, "a linked folder was mirrored as an empty one")
    check(any("linked folder linked" in w for w in odd_result.warnings),
          "skipping a linked folder must be reported")
    if can_lock:
        check("hidden.txt" not in outputs and "locked" not in mirrored,
              "an unreadable folder was mirrored as an empty one")
        check(any("could not read the folder locked" in w for w in odd_result.warnings),
              "an unreadable folder must be reported")

    # An output that is somehow already there is skipped, never overwritten.
    pre = tmp / "scan-out" / "pre-existing"
    pre.mkdir(parents=True)
    (pre / "notes.txt").write_text("already here")
    pre_result = scanner.scan_compress(root, pre, ConvertSettings())
    check(all(j.output != pre / "notes.txt" for j in pre_result.jobs)
          and (pre / "notes.txt").read_text() == "already here",
          "an existing file in the run folder must be skipped, not overwritten")
    check(any("already exists" in w for w in pre_result.warnings), "that skip must be reported")

    # Renamed thumbnails are still thumbnails.
    for name, expected in (("a_min.jpg", True), ("a_MIN-2.jpg", True), ("a_min-12.png", True),
                           ("admin.jpg", False), ("a_minimal.jpg", False), ("a-2.jpg", False)):
        check(scanner.is_min_file(Path(name)) == expected,
              f"is_min_file({name}) should be {expected}")

    try:
        Settings(max_shrink_rounds=-1).validate()
        check(False, "a negative shrink-round count must be refused")
    except ValueError:
        pass


# --------------------------------------------------- thumbnail layouts + safety


def test_min_layouts(tmp: Path) -> None:
    section("Thumbnail layouts and source safety")
    root = tmp / "min-in"
    build_tree(root, 17)
    rng = np.random.default_rng(17)
    tiny = Image.fromarray(rng.integers(0, 256, (60, 80, 3), dtype=np.uint8))
    tiny.save(root / "already_min.jpg")  # an existing _min must never be a source

    # ---- layout 1: a _min/ folder holding only the thumbnails
    before = snapshot(root)
    run = tmp / "min-out" / "sub-run"
    result = scanner.scan_min(root, run, Settings(min_layout=LAYOUT_SUBFOLDER))
    run.mkdir(parents=True)
    run_jobs(result, lambda job: pipeline.compress(job.source, job.output, Settings()))

    produced = sorted(str(p.relative_to(run)) for p in run.rglob("*") if p.is_file())
    print(f"  {LAYOUT_SUBFOLDER}: {produced}")
    check(all(p.startswith(f"{MIN_SUBDIR}/") for p in produced),
          f"everything should sit under {MIN_SUBDIR}/, got {produced}")
    check(f"{MIN_SUBDIR}/sub/nested_min.jpg" in produced, "subfolders were not mirrored")
    check(result.to_copy == 0, "the subfolder layout must not copy anything")
    check(not any("already_min" in p for p in produced),
          "an existing _min.jpg was picked up as a source")
    check(snapshot(root) == before, "the source tree was modified by a thumbnail run")

    # ---- layout 2: full copy of the input, thumbnails alongside
    before = snapshot(root)
    run2 = tmp / "min-out" / "beside-run"
    result2 = scanner.scan_min(root, run2, Settings(min_layout=LAYOUT_BESIDE))
    run2.mkdir(parents=True)
    run_jobs(result2, lambda job: pipeline.compress(job.source, job.output, Settings()))

    produced2 = sorted(str(p.relative_to(run2)) for p in run2.rglob("*") if p.is_file())
    print(f"  {LAYOUT_BESIDE}: {produced2}")
    for expected in ("photo.png", "photo_min.jpg", "notes.txt",
                     "sub/nested.png", "sub/nested_min.jpg", "sub/clip.bin",
                     "already_min.jpg"):
        check(expected in produced2, f"{expected} missing from the full mirror")
    check((run2 / "empty").is_dir(), "the empty subfolder was not mirrored")
    check(snapshot(root) == before, "the source tree was modified by a mirroring run")

    # every copied file must be byte-identical to its source
    identical = all(
        (run2 / p.relative_to(root)).read_bytes() == p.read_bytes()
        for p in root.rglob("*")
        if p.is_file() and not p.name.endswith((".png", ".tif", ".jpg"))
    )
    check(identical, "a copied non-image file does not match its source byte for byte")
    check((run2 / "notes.txt").read_text() == "keep me", "notes.txt was not copied intact")

    # ---- an input already holding X_min.jpg next to X.jpg
    # The generated thumbnail and the copy of the existing file want the same
    # name.  Both must survive, and the user's own file must keep its name.
    both = tmp / "both-in"
    both.mkdir()
    tiny.save(both / "a.jpg")
    (both / "a_min.jpg").write_bytes((root / "already_min.jpg").read_bytes())
    both_run = tmp / "min-out" / "both-run"
    both_result = scanner.scan_min(both, both_run, Settings(min_layout=LAYOUT_BESIDE))
    both_run.mkdir(parents=True)
    run_jobs(both_result, lambda job: pipeline.compress(job.source, job.output, Settings()))
    landed = sorted(p.name for p in both_run.iterdir())
    print(f"  a.jpg + a_min.jpg -> {landed}")
    check(landed == ["a.jpg", "a_min-2.jpg", "a_min.jpg"], f"unexpected result {landed}")
    check((both_run / "a_min.jpg").read_bytes() == (both / "a_min.jpg").read_bytes(),
          "the user's own a_min.jpg was renamed instead of the generated thumbnail")
    check((both_run / "a.jpg").read_bytes() == (both / "a.jpg").read_bytes(),
          "a.jpg was not copied intact")

    # ---- a fresh run folder means nothing is ever an overwrite
    check(not (result.skipped or result2.skipped or both_result.skipped),
          "a scan into a fresh folder found outputs already there - the guarantee is broken")


# ------------------------------------------------------ pre-flight and guards


def test_preflight(tmp: Path) -> None:
    section("Pre-flight: stale temp files, unwritable output, self-overwrite")
    root = tmp / "pre-in"
    build_tree(root, 23)

    # Scanning never deletes anything.  ".part" is also what Firefox and wget
    # name in-progress downloads, and the output folder is wherever the user
    # pointed us.  Our own temp files have random names and only ever live in a
    # run folder made for that run, so there is nothing of ours to sweep up.
    stale_parent = tmp / "stale-out"
    stale_parent.mkdir()
    foreign = stale_parent / f"browser-download.zip{PART_SUFFIX}"
    lookalike = stale_parent / f"other_min.jpg{PART_SUFFIX}"
    for path in (foreign, lookalike):
        path.write_bytes(b"partial")
    scanner.scan_min(root, stale_parent / "run", Settings())
    print("  scanning leaves .part files in the output folder alone")
    check(all(p.is_file() and p.read_bytes() == b"partial" for p in (foreign, lookalike)),
          "scanning touched a .part file it did not write - it may be someone's download")

    locked = tmp / "locked-out"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        scanner.scan_min(root, locked / "run", Settings())
        check(False, "an unwritable output folder should raise ScanError")
    except scanner.ScanError as exc:
        print(f"  unwritable output refused: {str(exc)[:60]}")
        check(True, "")
    finally:
        locked.chmod(0o700)

    # self-overwrite guard: a naming scheme that maps a source onto itself
    guarded = scanner.scan_spec(scanner.ScanSpec(
        input_folder=root,
        output_root=root,  # deliberately illegal; check_folders would refuse it
        extensions=frozenset({".jpg"}),
        output_name=lambda p: p.name,
    ))
    print(f"  self-overwrite guard: {len(guarded.jobs)} jobs, {len(guarded.warnings)} warning(s)")
    check(len(guarded.jobs) == 0, "jobs that would overwrite their own source must be refused")
    check(any("overwrite the source" in w for w in guarded.warnings),
          "the self-overwrite refusal should be reported")

    # case-insensitive clash guard
    case_root = tmp / "case-in"
    case_root.mkdir()
    rng = np.random.default_rng(29)
    Image.fromarray(rng.integers(0, 256, (60, 80, 3), dtype=np.uint8)).save(case_root / "photo.png")
    shutil.copyfile(case_root / "photo.png", case_root / "photo.PNG")
    clash = scanner.scan_spec(scanner.ScanSpec(
        input_folder=case_root,
        output_root=tmp / "case-out" / "run",
        extensions=frozenset({".png"}),
        output_name=scanner.plain_jpg_name,
    ))
    folded = {str(job.output).casefold() for job in clash.jobs}
    print(f"  case clash: {sorted(j.output.name for j in clash.jobs)}")
    check(len(clash.jobs) == 2, f"expected 2 jobs, got {len(clash.jobs)}")
    check(len(folded) == 2,
          "photo.png and photo.PNG collided once case is ignored - would clobber on Windows")


def test_atomic_writes(tmp: Path) -> None:
    section("Atomic writes")
    rng = np.random.default_rng(19)
    tiny = Image.fromarray(rng.integers(0, 256, (60, 80, 3), dtype=np.uint8))
    source = tmp / "atomic-src.jpg"
    tiny.save(source, quality=80)

    target = tmp / "atomic" / "out.jpg"
    good = b"x" * 100
    write_atomic(target, good)
    check(target.read_bytes() == good, "write_atomic did not write the data")
    check(not list(target.parent.glob(f"*{PART_SUFFIX}")), "write_atomic left a .part behind")

    copy_atomic(source, tmp / "atomic" / "copied.jpg")
    check((tmp / "atomic" / "copied.jpg").read_bytes() == source.read_bytes(),
          "copy_atomic did not reproduce the source exactly")
    check(not list((tmp / "atomic").glob(f"*{PART_SUFFIX}")), "copy_atomic left a .part behind")

    # A failure must leave the existing file intact rather than truncated.
    survivor = tmp / "atomic" / "survivor.jpg"
    write_atomic(survivor, b"original contents")
    try:
        copy_atomic(tmp / "does-not-exist.jpg", survivor)
        check(False, "copying a missing source should raise")
    except OSError:
        pass
    check(survivor.read_bytes() == b"original contents",
          "a failed copy damaged the file that was already there")
    check(not list((tmp / "atomic").glob(f"*{PART_SUFFIX}")), "a failed copy left a .part behind")
    print("  write_atomic and copy_atomic keep existing files intact on failure")

    # A real file that happens to carry an output's old temp name is never used
    # as scratch space: a mirror copying "clash.jpg.part" across next to a
    # generated "clash.jpg" used to lose the copy without a word.
    scratch = tmp / "atomic2"
    scratch.mkdir()
    decoy = scratch / f"clash.jpg{PART_SUFFIX}"
    decoy.write_bytes(b"user data")
    write_atomic(scratch / "clash.jpg", b"generated")
    copy_atomic(source, scratch / "clash2.jpg")
    check(decoy.read_bytes() == b"user data",
          "writing clash.jpg clobbered the user's file clash.jpg.part")

    # A copy cut short must fail before it takes the real name.
    import minjpg.common as common

    real_copyfile = common.shutil.copyfile
    common.shutil.copyfile = lambda a, b: Path(b).write_bytes(Path(a).read_bytes()[:10])
    try:
        copy_atomic(source, scratch / "short.jpg")
        check(False, "a short copy should raise")
    except OSError as exc:
        print(f"  short copy refused: {str(exc)[:60]}")
    finally:
        common.shutil.copyfile = real_copyfile
    check(not (scratch / "short.jpg").exists(),
          "a short copy landed under the real name, looking complete")

    # Timestamps travel with copies and with compressed mirror files.
    old = 978307200  # 2001-01-01
    os.utime(source, (old, old))
    copy_atomic(source, scratch / "dated-copy.jpg")
    converted = convert.convert(
        source, scratch / "dated-convert.jpg",
        ConvertSettings(passthrough=False, max_size=0),
    )
    for path in (scratch / "dated-copy.jpg", converted.output):
        check(int(path.stat().st_mtime) == old, f"{path.name} lost its source's date")

    # Permissions follow the umask like a plain write, not a private 0o600.
    if os.name != "nt":
        umask = os.umask(0)
        os.umask(umask)
        mode = (scratch / "clash.jpg").stat().st_mode & 0o777
        check(mode == 0o666 & ~umask, f"written file has mode {oct(mode)}")

    # A legal 254-character name must not fail because of its temp name.
    long_name = scratch / ("y" * 250 + ".jpg")
    try:
        write_atomic(long_name, b"x")
        check(long_name.read_bytes() == b"x", "a 254-character name was not written")
    except OSError as exc:
        check(False, f"a 254-character name failed: {exc}")

    leftovers = [p.name for p in scratch.iterdir() if p.name.startswith(".minjpg-")]
    check(not leftovers, f"temp files left behind: {leftovers}")
    print("  temp names never clash, short copies never land, dates are kept")

    from minjpg.common import is_disk_full

    full = OSError(errno.ENOSPC, "No space left on device")
    wrapped = PipelineError("cannot write")
    wrapped.__cause__ = full
    check(is_disk_full(full) and is_disk_full(wrapped),
          "a full disk, even wrapped in a pipeline error, should be recognised")
    check(not is_disk_full(OSError("permission denied")) and not is_disk_full(None),
          "only a full disk counts as one")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--sample", type=int, default=6)
    args = parser.parse_args()

    print(f"Encoder: {__import__('minjpg').encoder.cjpeg_version()}")
    print(formats.describe_support())

    tmp = Path(tempfile.mkdtemp(prefix="minjpg-verify-"))
    out = tmp / "out"
    out.mkdir()
    try:
        if args.data.is_dir():
            test_real_originals(args.data, args.sample, out)
        else:
            print(f"(skipping real originals: {args.data} not found)")
        test_formats(tmp, out)
        test_wide_gamut(tmp, out)
        test_metadata(tmp, out)
        test_cap_and_passthrough(tmp, out)
        test_run_folder(tmp)
        test_folder_guards(tmp)
        test_scanner(tmp)
        test_min_layouts(tmp)
        test_preflight(tmp)
        test_atomic_writes(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{checks} checks run")
    if failures:
        print(f"{len(failures)} FAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
