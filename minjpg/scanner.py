"""Working out what a folder needs, and refusing to do anything destructive.

Both tabs use the same core: walk an input folder, work out where each result
goes, and drop anything that would overwrite the wrong thing.  Keeping it in one
place means a guard added here protects both tabs rather than one.

Every run writes into a folder created fresh for it (see :mod:`minjpg.runfolder`),
so in normal operation nothing here can overwrite anything at all — the
destination did not exist a moment ago.  The guards below are the second line of
defence for when that invariant is broken by something outside the app.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import formats, runfolder
from .config import LAYOUT_BESIDE, MIN_SUBDIR, ConvertSettings, Settings

MIN_SUFFIX = "_min"

#: Warn when a batch looks likely to need more than this fraction of free space.
_SPACE_MARGIN = 1.2

#: What a job does with its source.
PROCESS = "process"  # decode, re-encode, write
COPY = "copy"  # byte-for-byte copy, no re-encode


def output_for(source: Path) -> Path:
    """The ``_min.jpg`` that belongs next to ``source``."""
    return source.with_name(min_output_name(source))


def min_output_name(source: Path) -> str:
    return f"{source.stem}{MIN_SUFFIX}.jpg"


def plain_jpg_name(source: Path) -> str:
    return f"{source.stem}.jpg"


def same_name(source: Path) -> str:
    """Used for copies: the file keeps the name it already had."""
    return source.name


#: A thumbnail's stem ends in ``_min`` — or ``_min-2``, ``_min-3``… when a name
#: clash renamed it.  Missing the renamed ones let a re-run over an output
#: folder make thumbnails of thumbnails.
_MIN_STEM = re.compile(rf"{re.escape(MIN_SUFFIX)}(-\d+)?$", re.IGNORECASE)


def is_min_file(path: Path) -> bool:
    return bool(_MIN_STEM.search(path.stem))


@dataclass
class Job:
    source: Path
    output: Path
    action: str = PROCESS
    #: Where the original is copied if it cannot be processed, so a mirror never
    #: silently loses a file.  Reserved at scan time like any output, so the
    #: copy cannot land on another job's file.  ``None`` when there is no mirror.
    fallback: Path | None = None


@dataclass
class ScanResult:
    jobs: list[Job]
    skipped: list[Path]  # sources refused because their output already existed
    root: Path
    output_root: Path
    warnings: list[str] = field(default_factory=list)
    #: Input subfolders that hold no work of their own but must still exist in
    #: the mirror, so an empty folder is not silently dropped.
    empty_dirs: list[Path] = field(default_factory=list)
    #: The batch looks likely to run out of disk space.
    low_space: bool = False

    def __len__(self) -> int:
        return len(self.jobs)

    @property
    def destination(self) -> str:
        return str(self.output_root)

    @property
    def to_process(self) -> int:
        return sum(1 for job in self.jobs if job.action == PROCESS)

    @property
    def to_copy(self) -> int:
        return sum(1 for job in self.jobs if job.action == COPY)

    @property
    def copy_bytes(self) -> int:
        total = 0
        for job in self.jobs:
            if job.action != COPY:
                continue
            try:
                total += job.source.stat().st_size
            except OSError:
                pass
        return total


class ScanError(ValueError):
    """The folders given cannot be scanned as asked."""


@dataclass
class ScanSpec:
    """Everything the scan core needs, independent of which tab asked."""

    input_folder: Path
    output_root: Path  # always somewhere else; there is no in-place mode
    extensions: frozenset[str]
    output_name: Callable[[Path], str]
    #: Folder level inserted under ``output_root`` for processed results only.
    #: ``_min`` for the thumbnails-in-their-own-folder layout, empty otherwise.
    subdir: str = ""
    recursive: bool = True
    exclude_min: bool = False  # never feed a _min.jpg back in as a source
    copy_sources: bool = False  # copy the images themselves across as well
    copy_extras: bool = False  # copy every non-image file across too
    space_per_job: int = 0  # rough bytes per processed output, for the warning
    fallback_copy: bool = False  # reserve a place for each original, see Job.fallback


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
    except (ValueError, OSError):
        return False
    return True


def check_folders(input_folder: Path, output_root: Path) -> None:
    """Refuse folder pairs that would make a run eat its own tail.

    Applies to both tabs.  Overlap in *either* direction is refused: an output
    inside the input means a second run re-processes its own results, and an
    input inside the output means the run is writing into a tree it is reading.
    """
    if not input_folder.is_dir():
        raise ScanError(f"Input folder does not exist: {input_folder}")
    if not str(output_root):
        raise ScanError("Pick an output folder.")
    try:
        same = input_folder.resolve() == output_root.resolve()
    except OSError:
        same = False
    if same:
        raise ScanError("The output folder must be different from the input folder.")
    if _is_within(output_root, input_folder):
        raise ScanError(
            "The output folder is inside the input folder. Results would end up "
            "among the originals and a second run would re-process them. Pick a "
            "folder outside the input."
        )
    if _is_within(input_folder, output_root):
        raise ScanError(
            "The input folder is inside the output folder. Pick an output folder "
            "that does not contain the originals."
        )


def _preflight(root: Path) -> None:
    """Make sure we can actually write under ``root``.

    Failing here beats failing on the first image after the user has walked away.
    ``root`` is the run folder's parent — the folder the user picked — because
    the run folder itself is not created until the batch actually starts.
    """
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ScanError(f"Cannot create the output folder {root}: {exc}") from exc
    if not root.is_dir():
        raise ScanError(f"The output folder is not a directory: {root}")

    # Unique per process: a fixed name would truncate and delete a file of the
    # same name that happened to be the user's.
    probe = root / f".minjpg-write-test-{os.getpid()}"
    try:
        probe.write_bytes(b"")
        probe.unlink()
    except OSError as exc:
        raise ScanError(f"The output folder is not writable: {root}\n{exc}") from exc


def _check_space(root: Path, needed: int, warnings: list[str]) -> bool:
    """Warn, and return True, when the batch looks likely to run out of space."""
    if needed <= 0:
        return False
    try:
        free = shutil.disk_usage(root).free
    except OSError:
        return False
    if free < needed * _SPACE_MARGIN:
        # First, because it is the one warning that should stop someone.
        warnings.insert(
            0, f"only {free / 1e6:.0f} MB free where about {needed / 1e6:.0f} MB may be needed"
        )
        return True
    return False


def _is_link(path: Path) -> bool:
    """A symlink, or a Windows junction (which ``is_symlink`` does not catch)."""
    return path.is_symlink() or getattr(os.path, "isjunction", lambda _p: False)(path)


def _label(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root)) or "."
    except ValueError:
        return str(path)


def _walk(root: Path, recursive: bool, warnings: list[str]) -> list[Path]:
    """Every file and folder under ``root``, sorted, with a warning per skip.

    ``Path.glob`` passed over unreadable folders and linked folders without a
    word, and each then turned up in the mirror as an empty folder that looked
    complete.  Linked folders are still not followed — that way lie loops and
    trees outside the input — but now the user is told.
    """
    found: list[Path] = []
    unreadable: set[Path] = set()

    def report(error: OSError) -> None:
        folder = Path(error.filename) if error.filename else root
        unreadable.add(folder)
        warnings.append(
            f"could not read the folder {_label(folder, root)} "
            f"({error.strerror or error}); its contents are not included"
        )

    for dirpath, dirnames, filenames in os.walk(root, onerror=report):
        here = Path(dirpath)
        kept = []
        for name in dirnames:
            if _is_link(here / name):
                if recursive:
                    warnings.append(
                        f"skipped the linked folder {_label(here / name, root)}; links "
                        "are not followed, so its contents are not included"
                    )
                continue
            kept.append(name)
        dirnames[:] = kept if recursive else []
        found.extend(here / name for name in kept)
        found.extend(here / name for name in filenames)
    return sorted(path for path in found if path not in unreadable)


def scan_spec(spec: ScanSpec) -> ScanResult:
    """Collect the work described by ``spec``, refusing anything destructive."""
    if not spec.input_folder.is_dir():
        raise ScanError(f"Input folder does not exist: {spec.input_folder}")

    warnings: list[str] = []
    # The run folder does not exist yet, so the writability check has to happen
    # on its parent — which is the folder the user actually nominated anyway.
    _preflight(spec.output_root.parent)

    jobs: list[Job] = []
    skipped: list[Path] = []
    # Casefolded, so a case-insensitive filesystem cannot collide.  The
    # incomplete-run marker's name is taken from the start: a mirrored file of
    # that name would be overwritten by the marker, then deleted along with it.
    claimed: set[str] = {str(spec.output_root / runfolder.MARKER_NAME).casefold()}
    estimated = 0
    used_dirs: set[Path] = set()
    seen_dirs: set[Path] = set()

    def claim(target_dir: Path, name: str, source: Path, quiet: bool = False) -> Path:
        """``name`` in ``target_dir``, or ``-2``, ``-3``… if it is already taken."""
        output = target_dir / name
        if str(output).casefold() in claimed:
            index = 2
            stem, suffix = output.stem, output.suffix
            while (
                str(candidate := target_dir / f"{stem}-{index}{suffix}").casefold()
                in claimed
            ):
                index += 1
            if not quiet:
                warnings.append(f"{source.name} renamed to {candidate.name} (name clash)")
            output = candidate
        claimed.add(str(output).casefold())
        return output

    def place(
        source: Path, target_dir: Path, name: str, action: str,
        fallback_dir: Path | None = None,
    ) -> None:
        """Queue one job, resolving clashes and refusing self-overwrites.

        ``fallback_dir`` is where the original goes if it cannot be processed.
        """
        nonlocal estimated
        # Reading and writing the same file would destroy the source.  Cannot
        # happen with a fresh run folder, but the check costs nothing and this
        # is the last place that would notice.
        if _same_file(target_dir / name, source):
            warnings.append(f"skipped {source.name}: the output would overwrite the source")
            return

        output = claim(target_dir, name, source)
        if output.exists():
            # Cannot happen in a run folder that does not exist yet.  If it
            # ever does, skipping is the one answer that destroys nothing.
            warnings.append(f"skipped {source.name}: {output.name} already exists in the new folder")
            skipped.append(source)
            return
        fallback = None
        if fallback_dir is not None:
            # A source that keeps its own name (photo.jpg -> photo.jpg) falls
            # back onto its own output slot.  Any other reserves its own name
            # now — quietly, as it is only used if conversion fails.
            if name.casefold() == source.name.casefold():
                fallback = output
            else:
                fallback = claim(fallback_dir, same_name(source), source, quiet=True)
        jobs.append(Job(source=source, output=output, action=action, fallback=fallback))
        used_dirs.add(target_dir)
        try:
            size = source.stat().st_size
        except OSError:
            size = 0
        # Copies cost their real size; a full-tree copy can be gigabytes, which
        # the processed-output estimate would badly understate.
        estimated += size if action == COPY else (spec.space_per_job or size)

    files: list[tuple[Path, Path, bool]] = []  # (path, mirror dir, is a source image)
    for path in _walk(spec.input_folder, spec.recursive, warnings):
        if path.is_dir():
            # Non-recursive runs never look inside these, so mirroring them
            # would promise a copy of a folder whose contents we ignored.
            if spec.recursive and (spec.copy_extras or spec.copy_sources):
                seen_dirs.add(path)
            continue
        if not path.is_file():
            continue  # sockets, broken symlinks, device nodes: not ours to copy

        try:
            relative_parent = path.parent.relative_to(spec.input_folder)
        except ValueError:
            continue

        is_source = path.suffix.lower() in spec.extensions
        if is_source and spec.exclude_min and is_min_file(path):
            is_source = False  # never treat our own results as new source material
        files.append((path, spec.output_root / relative_parent, is_source))

    # Copies are placed first so a file keeps the name it already had.  The two
    # can collide: with the "beside" layout, an input holding both `a.jpg` and
    # `a_min.jpg` wants to write a generated `a_min.jpg` *and* copy the existing
    # one.  Whichever is placed second gets the `-2` rename, and it must be the
    # generated file — renaming the user's own file instead would be surprising,
    # and the copy would no longer sit where its original did.
    for path, mirror_dir, is_source in files:
        if is_source and spec.copy_sources:
            place(path, mirror_dir, same_name(path), COPY)
        elif not is_source and spec.copy_extras:
            place(path, mirror_dir, same_name(path), COPY)

    for path, mirror_dir, is_source in files:
        if not is_source:
            continue
        processed_dir = (
            spec.output_root / spec.subdir / mirror_dir.relative_to(spec.output_root)
            if spec.subdir
            else mirror_dir
        )
        place(
            path, processed_dir, spec.output_name(path), PROCESS,
            fallback_dir=mirror_dir if spec.fallback_copy else None,
        )

    # Folders that contributed no files still belong in a full mirror.
    empty_dirs = []
    if spec.copy_extras or spec.copy_sources:
        for directory in sorted(seen_dirs):
            target = spec.output_root / directory.relative_to(spec.input_folder)
            if target not in used_dirs:
                empty_dirs.append(target)

    # Nothing is ever deleted here.  Temp files are only written inside a run
    # folder created for that run, so there are no leftovers of ours to sweep
    # up — and a ".part" in the user's folder is someone else's download.
    low_space = _check_space(spec.output_root.parent, estimated, warnings)

    return ScanResult(
        jobs=jobs,
        skipped=skipped,
        root=spec.input_folder,
        output_root=spec.output_root,
        warnings=warnings,
        empty_dirs=empty_dirs,
        low_space=low_space,
    )


def _same_file(a: Path, b: Path) -> bool:
    try:
        if a.exists() and b.exists():
            return os.path.samefile(a, b)
    except OSError:
        pass
    return str(a.resolve()).casefold() == str(b.resolve()).casefold()


def scan_min(input_folder: Path, run_root: Path, settings: Settings) -> ScanResult:
    """Collect ``_min.jpg`` work from ``input_folder`` into ``run_root``.

    ``run_root`` is the freshly planned run folder — this never invents a
    destination of its own.  The layout setting decides whether the thumbnails
    sit in their own ``_min/`` tree or alongside a full copy of the input.

    Sources whose own name ends in ``_min`` are never inputs, so re-running over
    a finished folder is a no-op rather than compressing the compressed.
    """
    check_folders(input_folder, run_root)
    beside = settings.min_layout == LAYOUT_BESIDE
    return scan_spec(
        ScanSpec(
            input_folder=input_folder,
            output_root=run_root,
            extensions=settings.source_extensions(),
            output_name=min_output_name,
            subdir="" if beside else MIN_SUBDIR,
            recursive=settings.recursive,
            exclude_min=True,
            copy_sources=beside,
            copy_extras=beside,
            space_per_job=settings.size_hard_cap,
        )
    )


def scan_compress(
    input_folder: Path, run_root: Path, settings: ConvertSettings
) -> ScanResult:
    """Collect compression work from ``input_folder`` into ``run_root``.

    The run folder becomes a full mirror of the input: every image is replaced
    by its compressed JPEG, and everything else is copied across untouched, so
    nothing in the tree is lost on the way.

    ``_min.jpg`` files are the one exception.  They are this app's own finished
    thumbnails, already far inside any cap here, so re-encoding one only spends
    a second generation of loss on it for no gain — the same "never compress the
    compressed" rule :func:`scan_min` follows.  They ride along as copies
    instead, which keeps the mirror complete.
    """
    check_folders(input_folder, run_root)
    return scan_spec(
        ScanSpec(
            input_folder=input_folder,
            output_root=run_root,
            extensions=formats.source_extensions(),
            output_name=plain_jpg_name,
            recursive=settings.recursive,
            exclude_min=True,  # copied across instead, see above
            copy_sources=False,  # the compressed JPEG replaces the original
            copy_extras=True,
            fallback_copy=True,  # an image that cannot be converted is copied as-is
        )
    )
