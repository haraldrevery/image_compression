"""Downscaling that matches Squoosh's resize defaults.

Squoosh resizes with ``method: 'lanczos3'`` and ``linearRGB: true``
(see ``squoosh-dev/src/features/processors/resize/shared/meta.ts``), i.e. it
converts sRGB to linear light, filters there, and converts back.  Filtering in
linear light keeps high-contrast edges — bright sky against dark rock — from
darkening, which plain sRGB-space resampling does noticeably.

Pillow's ``LANCZOS`` is Lanczos3, so the only thing to add is the transfer
function on either side of it.
"""

from __future__ import annotations

import numpy as np
from PIL import Image

#: uint8 sRGB -> linear-light float32, precomputed once.
_SRGB_TO_LINEAR = np.array(
    [
        (c / 255.0) / 12.92 if (c / 255.0) <= 0.04045 else (((c / 255.0) + 0.055) / 1.055) ** 2.4
        for c in range(256)
    ],
    dtype=np.float32,
)


def _linear_to_srgb(values: np.ndarray) -> np.ndarray:
    """Inverse transfer function, returning uint8.

    Lanczos overshoots at edges, so clamp before the power function — negative
    inputs to ``np.power`` would come back as NaN.
    """
    values = np.clip(values, 0.0, 1.0)
    out = np.where(
        values <= 0.0031308,
        values * 12.92,
        1.055 * np.power(values, 1.0 / 2.4) - 0.055,
    )
    return np.clip(out * 255.0 + 0.5, 0, 255).astype(np.uint8)


def target_size(size: tuple[int, int], max_long: int, max_short: int = 0) -> tuple[int, int]:
    """Largest size within the caps that preserves aspect ratio.

    Never upscales.  ``max_short`` of 0 disables the short-edge cap.
    """
    width, height = size
    scale = 1.0
    long_edge, short_edge = max(width, height), min(width, height)
    if max_long > 0 and long_edge > max_long:
        scale = max_long / long_edge
    if max_short > 0 and short_edge * scale > max_short:
        scale = max_short / short_edge
    if scale >= 1.0:
        return width, height
    return max(1, round(width * scale)), max(1, round(height * scale))


def scale_size(size: tuple[int, int], factor: float, min_long: int) -> tuple[int, int]:
    """Scale a size by ``factor``, refusing to take the long edge below ``min_long``."""
    width, height = size
    long_edge = max(width, height)
    factor = max(factor, min_long / long_edge) if long_edge > min_long else 1.0
    return max(1, round(width * factor)), max(1, round(height * factor))


def resize(image: Image.Image, size: tuple[int, int], linear_light: bool = True) -> Image.Image:
    """Downscale ``image`` (mode RGB) to ``size``.

    With ``linear_light`` the filtering happens in linear light, matching
    Squoosh.  Channels are converted one at a time so peak memory stays at one
    float32 plane rather than three.
    """
    if image.size == size:
        return image
    if not linear_light:
        return image.resize(size, Image.LANCZOS)

    source = np.asarray(image, dtype=np.uint8)
    out = np.empty((size[1], size[0], 3), dtype=np.uint8)
    for channel in range(3):
        plane = _SRGB_TO_LINEAR[source[:, :, channel]]
        resized = np.asarray(
            Image.fromarray(plane, mode="F").resize(size, Image.LANCZOS),
            dtype=np.float32,
        )
        out[:, :, channel] = _linear_to_srgb(resized)
    return Image.fromarray(out, mode="RGB")
