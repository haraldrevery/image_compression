"""One batch run, from the moment its folder exists to the verdict on it.

Nothing here touches the interface.  The rules that decide whether a run folder
can be trusted — the incomplete marker above all — live here, so they can be
tested without a display and both tabs follow the same ones.
"""

from __future__ import annotations

import dataclasses
import os
import queue
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol

from . import runfolder, scanner
from .common import PipelineError, copy_atomic, full_disk_error
from .encoder import EncoderError

#: Entries listed per section of the incomplete marker; the log has the rest.
_MARKER_LIST_LIMIT = 10_000

#: Encoder failures in a row that stop a batch.  One can be a single bad
#: image; several in a row means the encoder itself is broken — blocked by
#: antivirus, say — and every remaining image would fail the same way.
_ENCODER_FAILURES_TO_STOP = 3


class ResultLike(Protocol):
    """What both pipelines' result objects have in common."""

    source: Path
    output: Path
    output_size: tuple[int, int]
    byte_size: int
    quality: int

    @property
    def kilobytes(self) -> float: ...


class CopyResult:
    """What a plain file copy reports back, shaped like a pipeline result.

    Copies share the tree, the log and the progress bar with encoded images, so
    they have to answer the same questions.  Dimensions and quality are not
    meaningful for an arbitrary file, and the tab renders the blanks as "—".
    """

    quality = 0
    output_size = (0, 0)

    def __init__(self, source: Path, output: Path, byte_size: int, lost: tuple[str, ...] = ()):
        self.source = source
        self.output = output
        self.byte_size = byte_size
        #: Set when this copy stands in for an image that could not be
        #: processed: the original was carried across so nothing is lost.
        self.kept_reason = ""
        #: Anything that could not come across with the file, one note each.
        self.lost = list(lost)

    @property
    def kilobytes(self) -> float:
        return self.byte_size / 1024


def run_copy(job: scanner.Job) -> CopyResult:
    """Copy one file across.

    ``copy_atomic`` verifies the size before the copy takes its real name, so a
    copy that ran out of disk part-way fails without leaving a truncated file.
    """
    written = copy_atomic(job.source, job.output)
    return CopyResult(job.source, job.output, written.byte_size, written.lost)


def marker_text(
    source_root: Path,
    status: str,
    failures: list[tuple[str, str]] = (),
    missing: list[str] = (),
    not_processed: list[str] = (),
) -> str:
    """What the incomplete-run marker says."""
    lines = [
        "This folder is NOT a complete copy of the input yet.",
        "",
        f"Input:    {source_root}",
        f"Status:   {status}",
        f"Updated:  {datetime.now():%Y-%m-%d %H:%M}",
        "",
        "minjpg deletes this file once every file has been written. While it is",
        "here, do not delete the originals on the strength of this folder.",
    ]
    for heading, entries in (
        (f"{len(failures)} file(s) could not be written:",
         [f"{path}: {message}" for path, message in failures]),
        (f"{len(missing)} item(s) in the input are not in this folder:", list(missing)),
        (f"{len(not_processed)} file(s) were not processed before the run ended:",
         list(not_processed)),
    ):
        if entries:
            lines += ["", heading]
            lines += [f"  {entry}" for entry in entries[:_MARKER_LIST_LIMIT]]
            if len(entries) > _MARKER_LIST_LIMIT:
                lines.append(f"  …and {len(entries) - _MARKER_LIST_LIMIT} more (see minjpg.log)")
    return "\n".join(lines) + "\n"


def _relative(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return path.name


def _identity(folder: Path) -> tuple[int, int] | None:
    """Which folder this is on disk — not just its name — or ``None`` if it is gone."""
    try:
        stat = folder.stat()
    except OSError:
        return None
    return stat.st_dev, stat.st_ino


def _is_link(path: Path) -> bool:
    """A symlink, or a Windows junction (which ``is_symlink`` does not catch)."""
    return path.is_symlink() or getattr(os.path, "isjunction", lambda _p: False)(path)


def unaccounted(source_root: Path, carried: set[Path]) -> list[str]:
    """Everything under ``source_root`` that a mirror of it does not hold.

    Walks the input again, on its own and independently of the scanner, so a
    file added during the run, a folder that could not be read, a linked folder
    or a subfolder left out is found however the job list came to miss it.
    Entries are paths relative to the input; folders end in ``/``.
    """
    missing: list[str] = []

    def unreadable(error: OSError) -> None:
        folder = Path(error.filename) if error.filename else source_root
        missing.append(f"{_relative(folder, source_root)}/ (could not be read)")

    for dirpath, dirnames, filenames in os.walk(source_root, onerror=unreadable):
        here = Path(dirpath)
        for name in list(dirnames):
            if _is_link(here / name):
                dirnames.remove(name)
                missing.append(f"{_relative(here / name, source_root)}/ (a linked folder, not followed)")
        for name in filenames:
            path = here / name
            # Broken links and sockets hold nothing to carry across.
            if path not in carried and path.is_file():
                missing.append(_relative(path, source_root))
    return sorted(missing)


class Worker(threading.Thread):
    """Runs a batch off the UI thread, reporting progress over a queue.

    Every event names the tree row it belongs to.  The tallies are written only
    by this thread and read by the UI once the final ``("finished", worker)``
    event arrives, which the queue delivers after everything else.

    ``audit`` is for runs whose folder is meant to mirror the input: once every
    job is done, the input is walked again and anything the folder does not
    hold is listed in ``missing``, which keeps the run from counting as complete.

    The run folder must have been through :func:`begin`: its marker is how the
    worker knows the folder is still the one it started in.
    """

    def __init__(
        self,
        rows: list[tuple[str, scanner.Job]],
        run_one: Callable[[scanner.Job], ResultLike],
        events: queue.Queue,
        source_root: Path,
        run_root: Path,
        audit: bool = False,
    ):
        super().__init__(daemon=True)
        self.rows = rows
        self.run_one = run_one
        self.events = events
        self.source_root = source_root
        self.run_root = run_root
        self.audit = audit
        self.cancelled = threading.Event()
        self.total = len(rows)
        self.processed = self.done = self.kept = 0
        #: Row id -> (source, why) for everything missing from the run folder.
        self.failed_rows: dict[str, tuple[Path, str]] = {}
        self.stop_reason: str | None = None
        #: Input items the finished folder does not hold, from the audit.
        self.missing: list[str] = []
        #: Sources whose jobs never ran, because the run ended first.
        self.not_processed: list[Path] = []
        self._carried: set[Path] = set()
        self._encoder_failures = 0

    def run(self) -> None:
        home = _identity(self.run_root)
        try:
            for index, (iid, job) in enumerate(self.rows):
                if self.cancelled.is_set() or self.stop_reason:
                    break
                # A folder that vanished mid-run — a drive unplugged, say — must
                # not be carried on in somewhere else.  Checking that it exists
                # is not enough: a write in flight when the drive went re-creates
                # the path on the disk underneath, so it must be the *same* folder.
                if not self._still_home(home):
                    self.stop_reason = (
                        f"the output folder {self.run_root} is not there any more, or "
                        "was replaced (was the drive disconnected?)"
                    )
                    break
                self.events.put(("progress", self, iid, index, self.total, job.source.name))
                self._run_job(iid, job)
                self.processed = index + 1
            self.not_processed = [job.source for _iid, job in self.rows[self.processed:]]
            if self.audit and not self.not_processed:
                self._check_nothing_missing()
        finally:
            self.events.put(("finished", self))

    def _still_home(self, home: tuple[int, int] | None) -> bool:
        """Is the run folder still the one this run began in?

        Its identity on disk must match, and the marker ``begin`` put there
        must still be in it: a re-created folder can be handed the very inode
        number the old one had, but it starts out empty.
        """
        return (home is not None and _identity(self.run_root) == home
                and (self.run_root / runfolder.MARKER_NAME).is_file())

    def _check_nothing_missing(self) -> None:
        try:
            self.missing = unaccounted(self.source_root, self._carried)
        except Exception as exc:  # an audit that cannot run must not pass the run
            self.missing = [f"(the check for missing files could not run: {exc})"]

    def _run_job(self, iid: str, job: scanner.Job) -> None:
        try:
            result = run_copy(job) if job.action == scanner.COPY else self.run_one(job)
        except Exception as exc:  # one bad file must not stop the batch
            # Only a file that cannot be converted is carried across as it is.
            # A broken encoder or an output that cannot be written is a failure
            # to report, not a reason to fill the folder with unconverted files.
            if (job.fallback is not None and isinstance(exc, PipelineError)
                    and full_disk_error(exc) is None):
                self._keep_original(iid, job, exc)
            else:
                self._fail(iid, job, exc, str(exc))
        else:
            self.done += 1
            self._carried.add(job.source)
            if job.action != scanner.COPY:
                self._encoder_failures = 0
            self.events.put(("done", iid, result))

    def _keep_original(self, iid: str, job: scanner.Job, reason: Exception) -> None:
        """Carry the original across in place of an image that cannot be converted.

        The run folder is meant to be a full copy of the input; dropping the
        file would leave a hole nobody notices until the originals are gone.
        """
        stand_in = dataclasses.replace(job, output=job.fallback, action=scanner.COPY)
        try:
            result = run_copy(stand_in)
        except Exception as exc:
            self._fail(iid, job, exc, f"{reason}; copying the original instead also failed: {exc}")
            return
        result.kept_reason = str(reason)
        self.kept += 1
        self._carried.add(job.source)
        self.events.put(("kept", iid, result))

    def _fail(self, iid: str, job: scanner.Job, exc: Exception, message: str) -> None:
        self.failed_rows[iid] = (job.source, message)
        # ...except where every later file would fail the same way.
        if (full := full_disk_error(exc)) is not None:
            where = f" (writing {full.filename})" if full.filename else ""
            self.stop_reason = f"there is no space left on the disk{where}"
        elif isinstance(exc, EncoderError):
            self._encoder_failures += 1
            if self._encoder_failures >= _ENCODER_FAILURES_TO_STOP:
                self.stop_reason = (
                    f"the encoder failed {self._encoder_failures} times in a row "
                    f"(last: {exc})"
                )
        self.events.put(("failed", iid, job.source.name, message))


# ------------------------------------------------------------ before and after


def begin(run_root: Path, result: scanner.ScanResult) -> list[str]:
    """Mark a new run folder incomplete, then mirror the input's empty folders.

    The marker goes first and comes off last: whatever stops this run —
    Cancel, a crash, a power cut, the window closing — the folder says it is
    not a complete copy.  Raises ``OSError`` when the marker cannot be written,
    which is the last check that the folder is writable at all.  Returns notes
    for the log.
    """
    runfolder.write_marker(run_root, marker_text(
        result.root,
        "started, not finished - if minjpg is no longer running, the run "
        "was interrupted (the app closed, crashed or lost power)",
    ))
    try:
        for directory in result.empty_dirs:
            directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return [f"  note: could not mirror an empty folder: {exc}"]
    return []


def is_complete(worker: Worker) -> bool:
    """Does the run folder hold everything it set out to?  Only then does the marker go."""
    return (worker.processed == worker.total and not worker.failed_rows
            and not worker.missing)


def status_line(worker: Worker) -> str:
    if worker.stop_reason:
        return (f"stopped after {worker.processed} of {worker.total} files: "
                f"{worker.stop_reason}")
    if worker.processed < worker.total:
        return f"cancelled after {worker.processed} of {worker.total} files"
    problems = []
    if worker.failed_rows:
        problems.append(f"{len(worker.failed_rows)} file(s) could not be written")
    if worker.missing:
        problems.append(f"{len(worker.missing)} item(s) in the input are not in this folder")
    return "finished, but " + " and ".join(problems)


def settle(worker: Worker) -> list[str]:
    """Leave the run folder telling the truth about itself.  Returns log lines.

    A run that wrote nothing leaves nothing behind; otherwise the marker comes
    off if the run is complete, and says what is missing if it is not.
    """
    if worker.done + worker.kept == 0:
        return tidy_empty(worker)
    return update_marker(worker)


def update_marker(worker: Worker) -> list[str]:
    """Remove the incomplete marker, or rewrite it to say what is missing."""
    try:
        if is_complete(worker):
            runfolder.remove_marker(worker.run_root)
            return []
        failures = [
            (_relative(source, worker.source_root), message)
            for source, message in worker.failed_rows.values()
        ]
        not_processed = [_relative(source, worker.source_root) for source in worker.not_processed]
        runfolder.write_marker(worker.run_root, marker_text(
            worker.source_root, status_line(worker), failures, worker.missing, not_processed,
        ))
    except OSError as exc:
        return [f"  note: could not update {runfolder.MARKER_NAME}: {exc}"]
    return [f"  {worker.run_root.name} is marked incomplete: see {runfolder.MARKER_NAME} in it"]


def tidy_empty(worker: Worker) -> list[str]:
    """Leave nothing behind when a run wrote nothing.

    Only the marker — ours — is deleted outright.  Everything else goes
    through ``rmdir``, which refuses a folder with anything in it, so a run
    that did write something is never cleaned up from under the user.
    """
    try:
        runfolder.remove_marker(worker.run_root)
    except OSError:
        pass
    if runfolder.discard_empty_tree(worker.run_root):
        return [f"Removed the empty folder {worker.run_root}"]
    return []
