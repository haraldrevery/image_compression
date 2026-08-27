#!/usr/bin/env python3
"""Exercise the converter: real originals, every format, and the tricky cases.

Usage::

    python tools/verify_convert.py [--data DIR] [--sample N]
"""

from __future__ import annotations

import argparse
import io
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageCms, JpegImagePlugin

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
    settings = ConvertSettings(force=True, max_long_edge=1600, quality=65, max_size=0)
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
    settings = ConvertSettings(force=True, max_long_edge=800, quality=70, max_size=0)
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
    made.append(("multipage tiff", multi_path))

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


WIDE_GAMUT_CANDIDATES = [
    "/usr/share/color/icc/colord/AdobeRGB1998.icc",
    "/usr/share/color/icc/colord/ProPhotoRGB.icc",
    "/usr/share/color/icc/ghostscript/a98.icc",
]


def test_wide_gamut(tmp: Path, out: Path) -> None:
    section("Wide-gamut colour conversion")
    settings = ConvertSettings(force=True, max_long_edge=600, quality=90, max_size=0)
    colour = (0, 200, 90)  # saturated green - well outside sRGB in Adobe RGB terms

    profile_path = next((p for p in WIDE_GAMUT_CANDIDATES if Path(p).is_file()), None)
    if profile_path:
        profile = Path(profile_path).read_bytes()
        source = tmp / "wide.jpg"
        Image.new("RGB", (600, 400), colour).save(source, icc_profile=profile, quality=98)

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

    keep = ConvertSettings(force=True, max_long_edge=400, quality=70, max_size=0,
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

    strip = ConvertSettings(force=True, max_long_edge=400, quality=70, max_size=0,
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

    fits = ConvertSettings(force=True, max_long_edge=1600, quality=90, max_size=300_000)
    r = convert.convert(noisy, out / "cap-fits.jpg", fits)
    print(f"  cap 300 KB: {r.output_size} {r.kilobytes:.1f} KB q{r.quality} "
          f"over_cap={r.over_cap}")
    check(not r.over_cap and r.byte_size <= 300_000,
          f"searched result {r.byte_size} should be under the 300 KB cap")
    check(r.quality < 90, "quality should have been searched down to meet the cap")

    tight = ConvertSettings(force=True, max_long_edge=1600, quality=90, max_size=40_000,
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
    settings = ConvertSettings(force=True, max_long_edge=1600, quality=65,
                               max_size=614_400)
    r = convert.convert(already, out / "already.jpg", settings)
    identical = r.output.read_bytes() == already.read_bytes()
    print(f"  passthrough: copied={r.copied} identical_bytes={identical}")
    check(r.copied, "an in-limits JPEG should be copied, not re-encoded")
    check(identical, "a copied file must be byte-identical to the source")

    r = convert.convert(already, out / "already-strip.jpg",
                        ConvertSettings(force=True, max_long_edge=1600, quality=65,
                                        max_size=614_400, strip_metadata=True))
    check(not r.copied, "passthrough must be off when metadata is being stripped")
    r = convert.convert(already, out / "already-override.jpg", settings, quality=50)
    check(not r.copied, "an explicit quality override must force a re-encode")
    print("  passthrough correctly disabled for strip_metadata and overrides")

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
    check(result.overwrites == 0 and result2.overwrites == 0 and both_result.overwrites == 0,
          "a run into a fresh folder reported overwrites - the guarantee is broken")


# ------------------------------------------------------ pre-flight and guards


def test_preflight(tmp: Path) -> None:
    section("Pre-flight: stale temp files, unwritable output, self-overwrite")
    root = tmp / "pre-in"
    build_tree(root, 23)

    # Only temp files this app would itself have written may be deleted.  ".part"
    # is also what Firefox and wget name in-progress downloads, and the output
    # folder is wherever the user pointed us, so a blanket sweep destroys data.
    stale_parent = tmp / "stale-out"
    run = stale_parent / "run"
    (run / "sub").mkdir(parents=True)
    ours = run / f"{MIN_SUBDIR}"
    ours.mkdir()
    ours_part = ours / f"other_min.jpg{PART_SUFFIX}"
    foreign = run / f"browser-download.zip{PART_SUFFIX}"
    nested = run / "sub" / f"deep.iso{PART_SUFFIX}"
    for path in (ours_part, foreign, nested):
        path.write_bytes(b"partial")

    cleaned = scanner.scan_min(root, run, Settings())
    print(f"  stale .part: warnings={cleaned.warnings}")
    check(not ours_part.exists(), "our own leftover .part should have been removed")
    check(any("leftover temp file" in w for w in cleaned.warnings),
          "removing a leftover .part should be reported")
    check(foreign.exists() and foreign.read_bytes() == b"partial",
          "a .part file we did not write must be left alone - it may be someone's download")
    check(nested.exists(), "a nested .part file we did not write must be left alone")

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
        force=True,
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
