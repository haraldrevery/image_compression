#!/usr/bin/env python3
"""Run the pipeline over real originals and compare with the Squoosh output.

Encodes a sample of the originals in ``example_data/`` into a scratch directory
and prints ours vs. the hand-made ``_min.jpg`` next to it, then asserts every
generated file is inside the hard cap and carries the expected MozJPEG
settings (ImageMagick quantization table, 4:2:0, progressive, no metadata).

Usage::

    python tools/verify_against_examples.py [--all] [--sample N] [--data DIR]
"""

from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

from PIL import Image, JpegImagePlugin

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from minjpg import encoder, pipeline, scanner  # noqa: E402
from minjpg.config import Settings  # noqa: E402

DEFAULT_DATA = Path(__file__).resolve().parents[2] / "example_data"

#: MozJPEG's base quantization table 3 ("ImageMagick"), natural order.
IMAGEMAGICK_TABLE = [
    16, 16, 16, 18, 25, 37, 56, 85,
    16, 17, 20, 27, 34, 40, 53, 75,
    16, 20, 24, 31, 43, 62, 91, 135,
    18, 27, 31, 40, 53, 74, 106, 156,
    25, 34, 43, 53, 69, 94, 131, 189,
    37, 40, 62, 74, 94, 124, 169, 238,
    56, 53, 91, 106, 131, 169, 226, 311,
    85, 75, 135, 156, 189, 238, 311, 418,
]


def scaled_table(quality: int) -> list[int]:
    """libjpeg's quality -> table scaling, as MozJPEG applies it.

    The 255 ceiling only applies to baseline JPEGs; these are progressive, so
    entries may run up to 32767 and low qualities really do exceed 255.
    """
    scaling = 5000 // quality if quality < 50 else 200 - quality * 2
    return [max(1, min(32767, (base * scaling + 50) // 100)) for base in IMAGEMAGICK_TABLE]


def derive_quality(table: list[int]) -> tuple[int, int]:
    """Best-matching quality for an observed table, plus the residual error."""
    best = min(
        ((q, sum(abs(a - b) for a, b in zip(table, scaled_table(q)))) for q in range(1, 101)),
        key=lambda pair: pair[1],
    )
    return best


def check_encoding(path: Path, expected_quality: int) -> list[str]:
    """Confirm the file really carries the Squoosh-equivalent settings."""
    problems = []
    with Image.open(path) as image:
        if JpegImagePlugin.get_sampling(image) != 2:
            problems.append("not 4:2:0 chroma subsampling")
        if not image.info.get("progressive"):
            problems.append("not progressive")
        if image.info.get("exif"):
            problems.append("carries EXIF")
        if image.info.get("icc_profile"):
            problems.append("carries an ICC profile")
        quality, error = derive_quality(list(image.quantization[0]))
        if error != 0:
            problems.append(f"quantization table is not the ImageMagick table (error {error})")
        elif quality != expected_quality:
            problems.append(f"table says quality {quality}, expected {expected_quality}")
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--sample", type=int, default=30)
    parser.add_argument("--all", action="store_true", help="use every original")
    parser.add_argument("--seed", type=int, default=7)
    args = parser.parse_args()

    if not args.data.is_dir():
        print(f"No such folder: {args.data}", file=sys.stderr)
        return 2

    settings = Settings()
    settings.force = True
    settings.recursive = False
    pairs = [
        (path, reference)
        for path in sorted(args.data.glob("*.jpg"))
        if not scanner.is_min_file(path) and (reference := scanner.output_for(path)).is_file()
    ]
    if not args.all:
        random.Random(args.seed).shuffle(pairs)
        pairs = sorted(pairs[: args.sample])

    out_dir = Path(__file__).resolve().parent.parent / ".verify-out"
    out_dir.mkdir(exist_ok=True)
    print(f"Encoder: {encoder.cjpeg_version()}")
    print(f"Comparing {len(pairs)} images against {args.data}\n")

    header = f"{'file':38}{'ours':>26}{'squoosh':>22}{'delta':>9}"
    print(header)
    print("-" * len(header))

    failures: list[str] = []
    over_cap = 0
    total_time = 0.0
    ours_bytes = theirs_bytes = 0

    for source, reference in pairs:
        started = time.perf_counter()
        try:
            result = pipeline.compress(source, out_dir / reference.name, settings)
        except Exception as exc:
            failures.append(f"{source.name}: {exc}")
            print(f"{source.name[:37]:38}{'FAILED: ' + str(exc)[:40]}")
            continue
        total_time += time.perf_counter() - started

        with Image.open(reference) as ref:
            ref_size = ref.size
        ref_bytes = reference.stat().st_size
        ours_bytes += result.byte_size
        theirs_bytes += ref_bytes

        if result.byte_size > settings.size_hard_cap:
            over_cap += 1
            failures.append(f"{source.name}: {result.byte_size} bytes exceeds the hard cap")

        problems = check_encoding(result.output, result.quality)
        if problems:
            failures.append(f"{source.name}: " + "; ".join(problems))

        ours = f"{result.output_size[0]}x{result.output_size[1]} {result.kilobytes:5.1f}KB q{result.quality}"
        theirs = f"{ref_size[0]}x{ref_size[1]} {ref_bytes / 1024:5.1f}KB"
        delta = f"{(result.byte_size - ref_bytes) / 1024:+.1f}KB"
        flag = " !" if problems or result.byte_size > settings.size_hard_cap else ""
        print(f"{source.name[:37]:38}{ours:>26}{theirs:>22}{delta:>9}{flag}")

    print("-" * len(header))
    if pairs:
        print(
            f"{len(pairs)} images in {total_time:.1f}s "
            f"({total_time / max(1, len(pairs)):.2f}s each)"
        )
        print(
            f"total: ours {ours_bytes / 1024:.0f} KB vs squoosh {theirs_bytes / 1024:.0f} KB"
        )
    print(f"over hard cap ({settings.size_hard_cap} bytes): {over_cap}")

    if failures:
        print(f"\n{len(failures)} problem(s):")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print("\nAll generated files are within the cap and carry the expected MozJPEG settings.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
