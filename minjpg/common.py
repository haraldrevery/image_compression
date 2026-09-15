"""Pieces shared by both pipelines."""

from __future__ import annotations

import errno
import os
import secrets
import shutil
import time
from pathlib import Path

#: Suffix for in-progress writes.  Temp names are random and created
#: exclusively, so a temp file can never land on something already there — not
#: even a file the user happened to name ``photo.jpg.part`` that a run is
#: copying across next to a generated ``photo.jpg``.
PART_SUFFIX = ".part"
_TEMP_PREFIX = ".minjpg-"

#: On Windows a freshly written file is often held open for a moment by an
#: antivirus scanner or Explorer's thumbnailer, and replacing it fails with
#: PermissionError until they let go.  Retried with a short backoff.
_REPLACE_ATTEMPTS = 5

_BINARY = getattr(os, "O_BINARY", 0)  # Windows only; 0 elsewhere


#: Windows' "disk full" errors arrive mapped to ENOSPC as well.
_DISK_FULL = {errno.ENOSPC, getattr(errno, "EDQUOT", errno.ENOSPC)}


class PipelineError(RuntimeError):
    """A source image could not be turned into the output that was asked for."""


def is_disk_full(exc: BaseException | None) -> bool:
    """Is ``exc``, or anything that led to it, the disk running out of space?

    The cause chain is followed because the pipelines wrap errors.  A batch
    should stop on this rather than fail every remaining file the same way,
    keeping a full disk — possibly the system drive — full for the whole run.
    """
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, OSError) and exc.errno in _DISK_FULL:
            return True
        exc = exc.__cause__ or exc.__context__
    return False


def write_atomic(output: Path, data: bytes, times_from: Path | None = None) -> None:
    """Write ``data`` to ``output`` without ever exposing a partial file.

    The bytes go to a sibling temp file, are flushed to disk, and only then
    take the real name — so neither a crash nor a power cut can leave a
    truncated or empty file that looks finished.  ``times_from`` carries that
    file's timestamps over to the result.
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = _create_temp(output.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _finish(temp, output, times_from)
    except BaseException:
        _discard(temp)
        raise


def copy_atomic(source: Path, output: Path) -> int:
    """Copy ``source`` into place, verified, without exposing a partial file.

    The copy is checked against the source's size *before* it takes the real
    name, so a copy cut short lands nowhere rather than sitting in the output
    looking complete.  Timestamps come across too, as ``cp -p`` would keep them.
    Returns the number of bytes copied.
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    expected = source.stat().st_size
    fd, temp = _create_temp(output.parent)
    try:
        os.close(fd)
        shutil.copyfile(source, temp)
        actual = temp.stat().st_size
        if actual != expected:
            raise OSError(
                f"copy of {source.name} is {actual} bytes but the source is "
                f"{expected} — the destination may be full, or the source changed"
            )
        _fsync_path(temp)
        _finish(temp, output, source)
    except BaseException:
        _discard(temp)
        raise
    return actual


def _create_temp(parent: Path) -> tuple[int, Path]:
    """A new, empty, uniquely named file in ``parent``, opened for writing.

    ``O_EXCL`` is the no-clobber guarantee; mode 0o666 lets the umask decide
    permissions exactly as a plain write would, rather than mkstemp's 0o600.
    The name is short on purpose: deriving it from the output would push a
    legal 250-character name past the filesystem's limit.
    """
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _BINARY
    for _ in range(100):
        temp = parent / f"{_TEMP_PREFIX}{secrets.token_hex(6)}{PART_SUFFIX}"
        try:
            return os.open(temp, flags, 0o666), temp
        except FileExistsError:
            continue
    raise FileExistsError(f"could not create a temporary file in {parent}")


def _fsync_path(path: Path) -> None:
    fd = os.open(path, os.O_RDWR | _BINARY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _finish(temp: Path, output: Path, times_from: Path | None) -> None:
    """Stamp the temp file's times, then move it into place."""
    if times_from is not None:
        try:
            stat = times_from.stat()
            os.utime(temp, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        except OSError:
            pass  # timestamps are a courtesy; the data is what matters
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(temp, output)
            return
        except PermissionError:
            if os.name != "nt" or attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(0.05 * 2**attempt)


def _discard(temp: Path) -> None:
    try:
        temp.unlink()
    except OSError:
        pass
