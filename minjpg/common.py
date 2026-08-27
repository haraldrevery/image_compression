"""Pieces shared by both pipelines."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

#: Suffix for in-progress writes.  The scanner recognises and cleans these up,
#: so keep the two in step.
PART_SUFFIX = ".part"


class PipelineError(RuntimeError):
    """A source image could not be turned into the output that was asked for."""


def temp_for(output: Path) -> Path:
    """The in-progress name for ``output``.

    Public because the scanner needs it too: it cleans up leftovers from an
    interrupted run, and the only safe way to know a ``.part`` file is ours is
    to derive its name the same way we would when writing it.
    """
    return output.with_name(output.name + PART_SUFFIX)


def write_atomic(output: Path, data: bytes) -> None:
    """Write via a sibling temp file so a crash never leaves a truncated JPEG."""
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = temp_for(output)
    try:
        temp.write_bytes(data)
        os.replace(temp, output)
    except OSError:
        _discard(temp)
        raise


def copy_atomic(source: Path, output: Path) -> None:
    """Copy ``source`` into place without ever exposing a partial file.

    Copying straight to the destination would, if interrupted, leave a truncated
    JPEG where a valid one used to be.
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    temp = temp_for(output)
    try:
        shutil.copyfile(source, temp)
        os.replace(temp, output)
    except OSError:
        _discard(temp)
        raise


def _discard(temp: Path) -> None:
    try:
        temp.unlink()
    except OSError:
        pass
