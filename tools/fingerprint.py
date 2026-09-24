#!/usr/bin/env python3
"""Fingerprint everything the app writes, so a change can prove what it changed.

Builds a fixed corpus of awkward inputs once, runs it through both tabs'
scanner and batch worker without a display, and records everything that
reaches the disk: the job list and warnings, each row's outcome, the verdict on
the incomplete-run marker, and every file in each run folder — its hash, size,
whether it kept its source's date, and what metadata a JPEG carries.

``--save`` writes that record.  ``--compare`` runs again and diffs against a
saved record, so a refactor can show that it changed nothing and a fix can show
that it changed only what it was meant to.

The run itself goes through :mod:`minjpg.run`, exactly as the tabs drive it:
``begin`` (marker first, then the empty folders), the worker, then ``settle``
for the verdict on the marker.

The corpus is built on first use and reused afterwards, so every run sees the
same input bytes.  Delete the folder to build a new one — and then save new
baselines from the code they are meant to describe.

Usage::

    python tools/fingerprint.py --save .verify-out/fingerprint/phase0.json
    python tools/fingerprint.py --compare .verify-out/fingerprint/phase0.json

Exit status: 0 saved, or identical to the saved record; 1 differences found;
2 not comparable (a different corpus or different library versions).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import platform
import queue
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image, IptcImagePlugin

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from minjpg import convert, encoder, formats, pipeline, run, runfolder, scanner  # noqa: E402
from minjpg.config import LAYOUT_BESIDE, ConvertSettings, Settings  # noqa: E402
from samples import camera_exif, lightroom_xmp, photo, with_iptc  # noqa: E402

DEFAULT_CORPUS = Path(__file__).resolve().parent.parent / ".verify-out" / "fingerprint" / "corpus"

#: Every corpus file carries this date, so "kept its source's date" is a plain
#: comparison rather than a guess.
CORPUS_MTIME_NS = 1_600_000_000 * 10**9

#: Run folder names carry the minute; pinned so every run plans the same names.
WHEN = datetime(2026, 1, 1, 12, 0)

ADOBE_RGB = Path("/usr/share/color/icc/colord/AdobeRGB1998.icc")

#: name -> (job kind, settings the tab would hand its worker)
SCENARIOS = {
    "compress": ("compress", lambda: ConvertSettings()),
    "compress-strip-no-passthrough": (
        "compress", lambda: ConvertSettings(strip_metadata=True, passthrough=False),
    ),
    "compress-top-level-only": ("compress", lambda: ConvertSettings(recursive=False)),
    "thumbs-subfolder": ("min", lambda: Settings()),
    "thumbs-beside": ("min", lambda: Settings(min_layout=LAYOUT_BESIDE)),
}


# ----------------------------------------------------------------- the corpus


def build_corpus(root: Path) -> None:
    """Write the fixed set of awkward inputs into ``root``, all or nothing."""
    building = root.with_name(root.name + ".building")
    if building.exists():
        raise SystemExit(f"{building} is left over from an interrupted build; delete it first.")
    photos = building / "photos"
    (photos / "sub").mkdir(parents=True)
    (photos / "empty").mkdir()
    rng = np.random.default_rng(20260924)

    def jpeg(image: Image.Image, **kwargs) -> bytes:
        buffer = io.BytesIO()
        image.save(buffer, "JPEG", **kwargs)
        return buffer.getvalue()

    # Large enough to be resized and to need the size-cap search, and carrying
    # everything a photo library writes into a file.
    big = photo((4200, 2800), rng)
    (photos / "IMG_0001.jpg").write_bytes(
        with_iptc(jpeg(big, quality=92, exif=camera_exif(), xmp=lightroom_xmp()))
    )
    # Already inside both limits: copied verbatim while passthrough is on.
    photo((1200, 800), rng).save(photos / "IMG_0002.jpg", quality=80, exif=camera_exif())
    # Stored sideways, with an EXIF rotation to display upright.
    photo((900, 600), rng).save(photos / "rotated.jpg", quality=90, exif=camera_exif(orientation=6))
    # An iPhone folder: the same shot as HEIC and as JPEG.
    pair = photo((1600, 1200), rng)
    pair.save(photos / "IMG_1234.JPG", quality=85)
    if formats.HEIF_AVAILABLE:
        pair.save(photos / "IMG_1234.HEIC", quality=80, xmp=lightroom_xmp())
    # A TIFF with camera EXIF in its own sub-directories: date taken, GPS.
    with Image.open(io.BytesIO(jpeg(pair, exif=camera_exif()))) as carrier:
        pair.save(photos / "camera.tif", exif=carrier.getexif())
    # Two pages: kept whole rather than losing one.
    page = photo((800, 600), rng)
    page.save(photos / "multi.tif", save_all=True, append_images=[page.transpose(Image.ROTATE_180)])
    # 16-bit greyscale, transparency, CMYK, and WebP with XMP.
    grey = np.full((600, 900), 20000, dtype=np.uint16)
    grey[:, 450:] = 50000
    Image.fromarray(grey).save(photos / "grey16.png")
    logo = Image.new("RGBA", (900, 600), (255, 0, 0, 0))
    logo.paste((0, 0, 255, 255), (0, 0, 450, 600))
    logo.save(photos / "logo.png")
    photo((900, 600), rng).convert("CMYK").save(photos / "cmyk.jpg", quality=90)
    photo((900, 600), rng).save(photos / "web.webp", quality=85, xmp=lightroom_xmp())
    # A wide-gamut source, which has to be converted rather than copied.
    if ADOBE_RGB.is_file():
        Image.new("RGB", (900, 600), (40, 200, 60)).save(
            photos / "adobergb.jpg", quality=95, icc_profile=ADOBE_RGB.read_bytes()
        )
    # Two sources wanting the same output name.
    clash = photo((900, 600), rng)
    clash.save(photos / "photo.png")
    clash.save(photos / "photo.tif")
    # Not an image at all, whatever its name says.
    (photos / "broken.jpg").write_bytes(b"\xff\xd8 this is not a jpeg")
    # An existing thumbnail, a name that only looks like one, and the metadata
    # file a Mac leaves next to each photo on a USB drive.
    photo((600, 400), rng).save(photos / "photo_min.jpg", quality=60)
    photo((600, 400), rng).save(photos / "trip_min-2024.png")
    (photos / "._IMG_0001.jpg").write_bytes(b"\x00\x05\x16\x07\x00\x02\x00\x00" + bytes(4088))
    # Someone's own file that happens to have the incomplete marker's name.
    (photos / runfolder.MARKER_NAME).write_text("a file of my own\n")
    # Non-images, and a subfolder.
    (photos / "notes.txt").write_text("keep me\n")
    (photos / "sub" / "clip.bin").write_bytes(bytes(range(256)) * 8)
    photo((1000, 700), rng).save(photos / "sub" / "nested.jpg", quality=88)

    for path in photos.rglob("*"):
        os.utime(path, ns=(CORPUS_MTIME_NS, CORPUS_MTIME_NS))
    building.rename(root)


def corpus_record(root: Path) -> dict:
    record = {}
    for path in sorted((root / "photos").rglob("*")):
        rel = path.relative_to(root / "photos").as_posix()
        record[rel + "/" if path.is_dir() else rel] = "dir" if path.is_dir() else sha256(path)
    return record


def prepare_input(corpus: Path, tmp: Path) -> Path:
    """A working copy of the corpus, plus the folders that cannot be stored in it.

    A linked folder and an unreadable one are made here, per run: an unreadable
    folder with a file inside cannot be deleted later without fixing its
    permissions first, so it has no business sitting in ``.verify-out``.
    """
    source = tmp / "photos"
    shutil.copytree(corpus / "photos", source, symlinks=True)  # copy2 keeps the dates
    # File-manager tags, as KDE Dolphin writes them, on a file that is
    # converted, one passed through, one copied and one kept as it is.
    for name in ("IMG_0001.jpg", "IMG_0002.jpg", "notes.txt", "broken.jpg"):
        try:
            os.setxattr(source / name, "user.xdg.tags", b"holiday,harbour")
            os.setxattr(source / name, "user.baloo.rating", b"8")
        except (AttributeError, OSError):
            break  # not Linux, or a filesystem without extended attributes
    elsewhere = tmp / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "far.txt").write_text("outside the input\n")
    try:
        (source / "linked").symlink_to(elsewhere, target_is_directory=True)
    except OSError:
        pass  # no symlinks without privileges on Windows
    if os.name != "nt" and os.geteuid() != 0:  # root reads it anyway
        locked = source / "locked"
        locked.mkdir()
        (locked / "secret.txt").write_text("unreadable\n")
        locked.chmod(0)
    return source


def unlock(tmp: Path) -> None:
    locked = tmp / "photos" / "locked"
    if locked.exists():
        locked.chmod(0o700)


def input_state(source: Path) -> dict[str, tuple]:
    """Every readable input file's hash, date and tags, to prove the runs left them alone."""
    state = {}
    for dirpath, _dirnames, filenames in os.walk(source):
        for name in filenames:
            path = Path(dirpath) / name
            state[path.relative_to(source).as_posix()] = (
                sha256(path), path.stat().st_mtime_ns, user_xattrs(path),
            )
    return state


def user_xattrs(path: Path) -> dict[str, str]:
    """The file-manager tags on ``path``: extended attributes in the ``user.`` namespace."""
    try:
        names = [name for name in os.listxattr(path) if name.startswith("user.")]
        return {name: os.getxattr(path, name).decode("utf-8", "replace") for name in sorted(names)}
    except (AttributeError, OSError):
        return {}


# ------------------------------------------------------------------ recording


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Normaliser:
    """Replaces this run's temporary paths, so two runs can be compared."""

    def __init__(self, **paths: Path) -> None:
        self.paths = sorted(((str(p), f"<{name}>") for name, p in paths.items()),
                            key=lambda pair: -len(pair[0]))

    def __call__(self, value):
        if isinstance(value, Path):
            value = str(value)
        if isinstance(value, str):
            for path, label in self.paths:
                value = value.replace(path, label)
            return value
        if isinstance(value, (list, tuple)):
            return [self(item) for item in value]
        if isinstance(value, dict):
            return {key: self(item) for key, item in value.items()}
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return repr(value)


def describe_result(result, normalise: Normaliser) -> dict:
    """Every attribute of a row's result, so new fields show up in a diff by themselves."""
    values = {key: value for key, value in vars(result).items() if key != "source"}
    values["type"] = type(result).__name__
    if hasattr(type(result), "status"):
        values["status"] = result.status
    return normalise(values)


def jpeg_facts(path: Path) -> dict | None:
    """What a JPEG carries that a hash alone would not explain."""
    with path.open("rb") as handle:
        if handle.read(2) != b"\xff\xd8":
            return None
    try:
        with Image.open(path) as image:
            exif = image.getexif()
            taken = exif.get_ifd(0x8769).get(0x9003)
            gps = exif.get_ifd(0x8825)
            xmp = image.info.get("xmp") or b""
            iptc = (IptcImagePlugin.getiptcinfo(image) or {}).get((2, 120))
            if isinstance(iptc, list):
                iptc = b"; ".join(iptc)
            return {
                "size": list(image.size),
                "mode": image.mode,
                "progressive": bool(image.info.get("progressive")),
                "icc_profile": bool(image.info.get("icc_profile")),
                "make": exif.get(0x010F),
                "orientation": exif.get(0x0112),
                "date_taken": taken,
                "gps": bool(gps),
                "xmp_caption": b"Harbour at dawn" in xmp,
                "xmp_rating": b'xmp:Rating="4"' in xmp,
                "iptc_caption": iptc.decode("utf-8", "replace") if iptc else None,
            }
    except Exception as exc:
        return {"unreadable": type(exc).__name__}


def record_tree(run_root: Path, normalise: Normaliser) -> dict | None:
    if not run_root.exists():
        return None
    tree: dict = {}
    for path in sorted(run_root.rglob("*")):
        rel = path.relative_to(run_root).as_posix()
        if path.is_dir():
            tree[rel + "/"] = "dir"
            continue
        if rel == runfolder.MARKER_NAME:
            text = path.read_text(encoding="utf-8")
            kept = [line for line in text.splitlines() if not line.startswith("Updated:")]
            tree[rel] = {"marker": normalise("\n".join(kept))}
            continue
        stat = path.stat()
        entry = {
            "bytes": stat.st_size,
            "sha256": sha256(path),
            "keeps_date": stat.st_mtime_ns == CORPUS_MTIME_NS,
        }
        facts = jpeg_facts(path)
        if facts is not None:
            entry["jpeg"] = facts
        tags = user_xattrs(path)
        if tags:
            entry["xattrs"] = tags
        tree[rel] = entry
    return tree


def run_scenario(kind: str, settings, source: Path, parent: Path, tmp: Path) -> dict:
    parent.mkdir(parents=True)
    reserved = runfolder.plan(parent, source, kind, WHEN)
    normalise = Normaliser(run=reserved.path, input=source, tmp=tmp)
    if kind == "compress":
        scan = scanner.scan_compress(source, reserved.path, settings)

        def run_one(job):
            return convert.convert(job.source, job.output, settings)
    else:
        scan = scanner.scan_min(source, reserved.path, settings)

        def run_one(job):
            return pipeline.compress(job.source, job.output, settings)

    def rel_in(path: Path) -> str:
        return path.relative_to(source).as_posix()

    def rel_out(path: Path | None) -> str | None:
        return None if path is None else path.relative_to(reserved.path).as_posix()

    scan_record = {
        "jobs": {
            f"{rel_in(job.source)} [{job.action}]": {
                "output": rel_out(job.output), "fallback": rel_out(job.fallback),
            }
            for job in scan.jobs
        },
        "warnings": normalise(scan.warnings),
        "skipped": [rel_in(path) for path in scan.skipped],
        "empty_dirs": [rel_out(path) for path in scan.empty_dirs],
        "low_space": scan.low_space,
    }

    # As the tabs do it: create, begin, the worker, then settle the verdict.
    created = runfolder.create(reserved)
    log = run.begin(created.path, scan)
    rows = [(str(index), job) for index, job in enumerate(scan.jobs)]
    events: queue.Queue = queue.Queue()
    worker = run.Worker(rows, run_one, events, scan.root, created.path, audit=scan.mirror)
    worker.run()  # on this thread: the events are all queued by the time it returns
    log += run.settle(worker)

    outcomes: dict[str, dict] = {}
    while not events.empty():
        event = events.get()
        if event[0] in ("done", "kept"):
            outcomes[event[1]] = {"event": event[0], **describe_result(event[2], normalise)}
        elif event[0] == "failed":
            outcomes[event[1]] = {"event": "failed", "message": normalise(event[3])}
    row_record = {
        f"{rel_in(job.source)} [{job.action}]": outcomes.get(iid, {"event": "not run"})
        for iid, job in rows
    }

    return {
        "scan": scan_record,
        "rows": row_record,
        "run": {
            "processed": worker.processed,
            "total": worker.total,
            "written": worker.done,
            "kept_as_original": worker.kept,
            "failed": len(worker.failed_rows),
            "stop_reason": worker.stop_reason,
            "missing": normalise(worker.missing),
            "judged_complete": run.is_complete(worker),
            "log": normalise(log),
        },
        "tree": record_tree(created.path, normalise),
    }


def environment() -> dict:
    import PIL

    try:
        import pillow_heif

        heif = pillow_heif.__version__
    except Exception:
        heif = None
    return {
        "platform": f"{platform.system()} {platform.machine()}",
        "python": platform.python_version(),
        "pillow": PIL.__version__,
        "numpy": np.__version__,
        "pillow_heif": heif,
        "cjpeg": encoder.cjpeg_version(),
        "cjpeg_sha256": encoder.sha256(encoder.cjpeg_path()),
    }


def fingerprint(corpus: Path) -> dict:
    if not corpus.exists():
        print(f"Building the corpus in {corpus} (once; later runs reuse it)")
        corpus.parent.mkdir(parents=True, exist_ok=True)
        build_corpus(corpus)
    record = {"environment": environment(), "corpus": corpus_record(corpus), "scenarios": {}}
    tmp = Path(tempfile.mkdtemp(prefix="minjpg-fingerprint-"))
    try:
        source = prepare_input(corpus, tmp)
        # Whether tags could be set decides what the runs can carry, so a record
        # made where they could not is not comparable with one made where they could.
        record["environment"]["xattrs"] = bool(user_xattrs(source / "notes.txt"))
        before = input_state(source)
        for name, (kind, make_settings) in SCENARIOS.items():
            print(f"  running {name}")
            result = run_scenario(kind, make_settings(), source, tmp / "out" / name, tmp)
            record["scenarios"][name] = result
            run = result["run"]
            print(f"    {len(result['scan']['jobs'])} jobs: {run['written']} written, "
                  f"{run['kept_as_original']} kept as originals, {run['failed']} failed; "
                  f"marker {'removed' if run['judged_complete'] else 'kept'}")
        record["input_untouched"] = input_state(source) == before
        print(f"  input untouched by every run: {record['input_untouched']}")
    finally:
        unlock(tmp)
        shutil.rmtree(tmp, ignore_errors=True)
    return record


# ----------------------------------------------------------------- comparing


def flatten(value, prefix: str = "") -> dict:
    if isinstance(value, dict) and value:
        flat: dict = {}
        for key, item in value.items():
            flat.update(flatten(item, f"{prefix}/{key}" if prefix else str(key)))
        return flat
    return {prefix: value}


def show(value) -> str:
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= 160 else text[:157] + "..."


def print_changes(old: dict, new: dict) -> int:
    a, b = flatten(old), flatten(new)
    removed = sorted(a.keys() - b.keys())
    added = sorted(b.keys() - a.keys())
    changed = sorted(key for key in a.keys() & b.keys() if a[key] != b[key])
    for key in removed:
        print(f"  - {key}: {show(a[key])}")
    # A new, still-empty field on every existing result would bury the real
    # changes; those are counted instead of listed.
    old_entries = {key.rsplit("/", 1)[0] for key in a}
    new_fields: dict[tuple[str, str], int] = {}
    for key in added:
        if b[key] in ([], "", None, False, {}) and key.rsplit("/", 1)[0] in old_entries:
            field = (key.rsplit("/", 1)[-1], show(b[key]))
            new_fields[field] = new_fields.get(field, 0) + 1
        else:
            print(f"  + {key}: {show(b[key])}")
    for (name, value), count in sorted(new_fields.items()):
        print(f"  + new field '{name}' = {value} in {count} place(s)")
    for key in changed:
        if isinstance(a[key], list) and isinstance(b[key], list):
            gone = [item for item in a[key] if item not in b[key]]
            new_items = [item for item in b[key] if item not in a[key]]
            print(f"  ~ {key}:")
            for item in gone:
                print(f"      - {show(item)}")
            for item in new_items:
                print(f"      + {show(item)}")
            if not gone and not new_items:
                print("      (same items, different order)")
        else:
            print(f"  ~ {key}: {show(a[key])} -> {show(b[key])}")
    return len(removed) + len(added) + len(changed)


def compare(saved: dict, fresh: dict) -> int:
    for part, why in (("environment", "library versions or encoder differ"),
                      ("corpus", "the input files differ")):
        if saved[part] != fresh[part]:
            print(f"NOT COMPARABLE: {why}.")
            print_changes(saved[part], fresh[part])
            return 2
    total = 0
    if not fresh["input_untouched"]:
        print("INPUT CHANGED: a run modified the input folder.")
        total += 1
    for name in sorted(saved["scenarios"].keys() | fresh["scenarios"].keys()):
        old, new = saved["scenarios"].get(name, {}), fresh["scenarios"].get(name, {})
        if old == new:
            print(f"{name}: identical")
            continue
        print(f"{name}: CHANGED")
        total += print_changes(old, new)
    if total:
        print(f"\n{total} difference(s) from the saved fingerprint.")
        return 1
    print("\nIdentical to the saved fingerprint.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--save", type=Path, metavar="FILE", help="record a new fingerprint")
    action.add_argument("--compare", type=Path, metavar="FILE", help="diff against a saved one")
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    args = parser.parse_args()

    if args.save and args.save.exists():
        print(f"{args.save} already exists; a baseline is never overwritten. "
              "Pick another name or delete it first.", file=sys.stderr)
        return 2
    if args.compare and not args.compare.is_file():
        print(f"No saved fingerprint at {args.compare}", file=sys.stderr)
        return 2

    fresh = fingerprint(args.corpus)
    if not fresh["input_untouched"]:
        print("INPUT CHANGED: a run modified the input folder.", file=sys.stderr)
        if args.save:
            return 1  # never record a broken state as the reference
    if args.save:
        args.save.parent.mkdir(parents=True, exist_ok=True)
        args.save.write_text(json.dumps(fresh, indent=1, ensure_ascii=False, sort_keys=True) + "\n",
                             encoding="utf-8")
        print(f"Saved {args.save}")
        return 0
    saved = json.loads(args.compare.read_text(encoding="utf-8"))
    # Round-trip through JSON so tuples and lists compare the same way.
    return compare(saved, json.loads(json.dumps(fresh, sort_keys=True)))


if __name__ == "__main__":
    sys.exit(main())
