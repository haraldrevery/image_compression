"""Naming and creating the fresh folder each run writes into.

Every run gets its own brand-new folder inside the output folder the user chose.
That is the whole no-overwrite guarantee: a run cannot destroy anything, because
nothing it writes to existed a moment earlier.  Keeping the logic here, small and
on its own, means it can be tested directly rather than through the GUI.

The name carries the input folder, the job and the minute, so two runs over the
same folder land side by side rather than on top of each other.  If the name is
somehow taken anyway, a numeric suffix is added and the caller is told so it can
warn the user.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

#: What each job calls itself in the folder name.
JOB_SUFFIX = {"min": "min", "compress": "compressed"}

#: Used when the input folder has no name of its own — a filesystem root, or a
#: Windows drive like ``D:\``, whose ``Path.name`` is empty.
FALLBACK_NAME = "images"

#: Characters Windows refuses in a path component.  A name generated on Linux
#: can end up on a USB stick or a network share read from Windows, so they are
#: stripped everywhere rather than only where they would fail today.
_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

#: Windows also refuses these as whole names, with or without an extension.
_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{n}" for n in range(1, 10)),
    *(f"lpt{n}" for n in range(1, 10)),
}

#: How many suffixes to try before giving up.  Only reached if something is
#: creating folders as fast as we can name them.
_MAX_ATTEMPTS = 50


class RunFolderError(OSError):
    """The run folder could not be created."""


def safe_name(name: str) -> str:
    """A path component that is legal on every platform we ship to."""
    cleaned = _ILLEGAL.sub("_", name).strip(" .")
    if not cleaned or cleaned.lower() in _RESERVED:
        return FALLBACK_NAME
    return cleaned


def base_name(input_folder: Path, kind: str, when: datetime | None = None) -> str:
    """``<input folder>_<job>_<date>_<time>`` — the name before any suffix."""
    if kind not in JOB_SUFFIX:
        raise ValueError(f"unknown job kind: {kind}")
    stamp = (when or datetime.now()).strftime("%Y-%m-%d_%H%M")
    return f"{safe_name(input_folder.name)}_{JOB_SUFFIX[kind]}_{stamp}"


@dataclass(frozen=True)
class RunPlan:
    """A name reserved for a run, not yet created on disk."""

    parent: Path
    path: Path
    base: str
    suffix: int  # 0 when the base name was free
    collided: bool

    @property
    def name(self) -> str:
        return self.path.name


def taken(path: Path) -> bool:
    """Is anything at all sitting here?

    ``lexists`` rather than ``exists`` on purpose: a dangling symlink is not a
    free name — creating the folder would fail, and following the link could
    point the run at somewhere else entirely.
    """
    return os.path.lexists(path)


def plan(
    parent: Path, input_folder: Path, kind: str, when: datetime | None = None
) -> RunPlan:
    """Reserve a name under ``parent``. Creates nothing."""
    base = base_name(input_folder, kind, when)
    if not taken(parent / base):
        return RunPlan(parent, parent / base, base, 0, False)

    for suffix in range(2, _MAX_ATTEMPTS + 2):
        candidate = parent / f"{base}_{suffix}"
        if not taken(candidate):
            return RunPlan(parent, candidate, base, suffix, True)

    raise RunFolderError(
        f"Could not find a free name for {base} in {parent} after "
        f"{_MAX_ATTEMPTS} attempts."
    )


def create(reserved: RunPlan) -> RunPlan:
    """Create the folder, re-planning if the name was taken in the meantime.

    ``mkdir`` is deliberately called without ``exist_ok``: succeeding on an
    existing folder is exactly how a run would end up writing into somewhere
    that already holds the user's files.  The plan comes back because the name
    may have moved on, and the caller has to report the one actually used.
    """
    current = reserved
    for _ in range(_MAX_ATTEMPTS):
        try:
            current.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise RunFolderError(
                f"Cannot create the output folder {current.parent}: {exc}"
            ) from exc
        try:
            current.path.mkdir()
        except FileExistsError:
            # Something appeared between planning and now.  Take the next name.
            current = plan_after(current)
            continue
        except OSError as exc:
            raise RunFolderError(f"Cannot create {current.path}: {exc}") from exc
        return current

    raise RunFolderError(
        f"Could not create a new folder for {reserved.base} in {reserved.parent}."
    )


def plan_after(previous: RunPlan) -> RunPlan:
    """The next free name after ``previous``, keeping its base."""
    for suffix in range(max(previous.suffix, 1) + 1, _MAX_ATTEMPTS + 2):
        candidate = previous.parent / f"{previous.base}_{suffix}"
        if not taken(candidate):
            return RunPlan(previous.parent, candidate, previous.base, suffix, True)
    raise RunFolderError(
        f"Could not find a free name for {previous.base} in {previous.parent}."
    )


def discard_if_empty(path: Path) -> bool:
    """Remove a run folder that was never written to.

    A cancelled run, or one where every image failed, should not leave an empty
    folder behind.  Only ever removes an empty directory, so nothing can be lost
    if the run did in fact write something.
    """
    try:
        path.rmdir()
    except OSError:
        return False  # not empty, not there, or not ours to remove
    return True
