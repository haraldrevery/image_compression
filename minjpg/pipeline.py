"""Turning one source image into a ``_min.jpg`` that fits the byte budget.

The search replaces what used to be done by hand in Squoosh: fit the image to
the size cap, then find the highest MozJPEG quality that still lands inside the
byte target.  Only if even the quality floor busts the target does the image
get shrunk further, because a smaller image at decent quality looks better than
a full-size one at quality 30.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from . import convert, encoder, resize
from .common import PipelineError, write_atomic
from .config import Settings

__all__ = ["PipelineError", "Result", "compress", "load_source"]


@dataclass
class Result:
    source: Path
    output: Path
    source_size: tuple[int, int]
    output_size: tuple[int, int]
    byte_size: int
    quality: int
    shrink_rounds: int
    hit_quality_floor: bool
    encode_count: int

    @property
    def kilobytes(self) -> float:
        return self.byte_size / 1024


def load_source(path: Path) -> Image.Image:
    """Open an image as upright sRGB, with any transparency flattened onto white."""
    return convert.load_source(path).image


def _search_quality(
    frame: Image.Image, settings: Settings
) -> tuple[int, bytes, int, bool]:
    """Highest quality whose output fits ``size_target``.

    Returns ``(quality, data, encode_count, hit_floor)``.  ``hit_floor`` means
    even ``quality_floor`` exceeded the target, so ``data`` is the floor result
    and the caller should consider shrinking.
    """
    floor, ceiling = settings.quality_floor, settings.quality_ceiling
    encodes = 0

    data = encoder.encode(frame, ceiling, settings.smoothing)
    encodes += 1
    if len(data) <= settings.size_target or floor == ceiling:
        return ceiling, data, encodes, len(data) > settings.size_target

    floor_data = encoder.encode(frame, floor, settings.smoothing)
    encodes += 1
    if len(floor_data) > settings.size_target:
        return floor, floor_data, encodes, True

    best_quality, best_data = floor, floor_data
    low, high = floor + 1, ceiling - 1
    while low <= high:
        mid = (low + high) // 2
        data = encoder.encode(frame, mid, settings.smoothing)
        encodes += 1
        if len(data) <= settings.size_target:
            best_quality, best_data = mid, data
            low = mid + 1
        else:
            high = mid - 1

    return best_quality, best_data, encodes, False


def compress(
    source: Path,
    output: Path,
    settings: Settings,
    long_edge: int | None = None,
    quality: int | None = None,
) -> Result:
    """Produce ``output`` from ``source`` and return what it took.

    ``long_edge`` and ``quality`` are per-image overrides from the GUI; passing
    ``quality`` skips the search and encodes once at that quality (still
    refusing to write anything above the hard cap).
    """
    image = load_source(source)
    source_size = image.size
    cap = long_edge if long_edge else settings.max_long_edge
    size = resize.target_size(source_size, cap, settings.max_short_edge)

    if quality is not None:
        frame = resize.resize(image, size, settings.linear_light_resize)
        data = encoder.encode(frame, quality, settings.smoothing)
        if len(data) > settings.size_hard_cap:
            raise PipelineError(
                f"quality {quality} at {size[0]}x{size[1]} gives "
                f"{len(data) / 1024:.1f} KB, over the {settings.size_hard_cap / 1024:.0f} KB cap"
            )
        write_atomic(output, data)
        return Result(source, output, source_size, size, len(data), quality, 0, False, 1)

    total_encodes = 0
    fallback: tuple[int, bytes, tuple[int, int], int] | None = None

    for round_index in range(settings.max_shrink_rounds + 1):
        frame = resize.resize(image, size, settings.linear_light_resize)
        found_quality, data, encodes, hit_floor = _search_quality(frame, settings)
        total_encodes += encodes

        if not hit_floor:
            write_atomic(output, data)
            return Result(
                source, output, source_size, size, len(data),
                found_quality, round_index, False, total_encodes,
            )

        # Even the quality floor overshot the target.  Remember it if it is at
        # least inside the hard cap, then try again smaller.
        if len(data) <= settings.size_hard_cap and fallback is None:
            fallback = (found_quality, data, size, round_index)

        if round_index == settings.max_shrink_rounds:
            break
        # Area scales roughly with byte count, so sqrt(target/actual) is a good
        # first guess.  Clamp it: at most 20% off in one go so we do not
        # overshoot, at least 2% so a near miss cannot converge asymptotically
        # and stall just above the target.
        estimate = (settings.size_target / len(data)) ** 0.5
        shrunk = resize.scale_size(
            size, min(0.98, max(0.80, estimate)), settings.min_long_edge
        )
        if shrunk == size:  # already at the minimum long edge
            break
        size = shrunk

    if fallback is not None:
        found_quality, data, size, rounds = fallback
        write_atomic(output, data)
        return Result(
            source, output, source_size, size, len(data),
            found_quality, rounds, True, total_encodes,
        )

    raise PipelineError(
        f"cannot fit under {settings.size_hard_cap / 1024:.0f} KB even at quality "
        f"{settings.quality_floor} and {size[0]}x{size[1]}"
    )
