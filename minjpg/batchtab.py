"""Shared machinery for the two batch tabs.

Both tabs do the same dance — scan a folder, run the jobs on a worker thread,
show results in a tree with a before/after preview, allow a per-image redo — and
differ only in *what* the job does and which controls sit at the top.  That
shared part lives here; each tab supplies the rest.
"""

from __future__ import annotations

import dataclasses
import queue
import threading
from datetime import datetime
from pathlib import Path
from tkinter import BooleanVar, StringVar, filedialog, messagebox, ttk
import tkinter as tk
from typing import Callable, Protocol

from PIL import Image, ImageOps, ImageTk

from . import formats, runfolder, scanner
from .common import copy_atomic, is_disk_full
from .logging_setup import get_logger

PREVIEW_MIN = (240, 180)

#: Row statuses highlighted for a second look.
ATTENTION = ("over cap", "at floor", "kept original")

#: Failures listed in the incomplete marker; the log has the rest.
_MARKER_LIST_LIMIT = 500


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

    def __init__(self, source: Path, output: Path, byte_size: int):
        self.source = source
        self.output = output
        self.byte_size = byte_size
        #: Set when this copy stands in for an image that could not be
        #: processed: the original was carried across so nothing is lost.
        self.kept_reason = ""

    @property
    def kilobytes(self) -> float:
        return self.byte_size / 1024


def run_copy(job: scanner.Job) -> CopyResult:
    """Copy one file across.

    ``copy_atomic`` verifies the size before the copy takes its real name, so a
    copy that ran out of disk part-way fails without leaving a truncated file.
    """
    return CopyResult(job.source, job.output, copy_atomic(job.source, job.output))


def marker_text(
    source_root: Path, status: str, failures: list[tuple[str, str]] = ()
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
    if failures:
        lines += ["", f"{len(failures)} file(s) are missing from this folder:"]
        lines += [f"  {path}: {message}" for path, message in failures[:_MARKER_LIST_LIMIT]]
        if len(failures) > _MARKER_LIST_LIMIT:
            lines.append(f"  …and {len(failures) - _MARKER_LIST_LIMIT} more (see minjpg.log)")
    return "\n".join(lines) + "\n"


def _upright(thumb: Image.Image, size: tuple[int, int]) -> tuple[Image.Image, tuple[int, int]]:
    """Turn a preview the way its EXIF says, as every result is turned.

    A phone photo otherwise previewed on its side next to its upright result.
    Broken EXIF must not cost the preview, so any failure leaves it unturned.
    Returns the image and ``size`` swapped to match a quarter turn.
    """
    try:
        turned = ImageOps.exif_transpose(thumb)
    except Exception:
        return thumb, size
    if turned is None:
        return thumb, size
    if turned.size != thumb.size:
        size = (size[1], size[0])
    return turned, size


def _relative(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return path.name


class Worker(threading.Thread):
    """Runs a batch off the UI thread, reporting progress over a queue.

    Every event names the tree row it belongs to.  The tallies are written only
    by this thread and read by the UI once the final ``("finished", worker)``
    event arrives, which the queue delivers after everything else.
    """

    def __init__(
        self,
        rows: list[tuple[str, scanner.Job]],
        run_one: Callable[[scanner.Job], ResultLike],
        events: queue.Queue,
        source_root: Path,
        run_root: Path,
    ):
        super().__init__(daemon=True)
        self.rows = rows
        self.run_one = run_one
        self.events = events
        self.source_root = source_root
        self.run_root = run_root
        self.cancelled = threading.Event()
        self.total = len(rows)
        self.processed = self.done = self.kept = 0
        #: Row id -> (source, why) for everything missing from the run folder.
        self.failed_rows: dict[str, tuple[Path, str]] = {}
        self.stop_reason: str | None = None

    def run(self) -> None:
        try:
            for index, (iid, job) in enumerate(self.rows):
                if self.cancelled.is_set() or self.stop_reason:
                    break
                self.events.put(("progress", self, iid, index, self.total, job.source.name))
                self._run_job(iid, job)
                self.processed = index + 1
        finally:
            self.events.put(("finished", self))

    def _run_job(self, iid: str, job: scanner.Job) -> None:
        try:
            result = run_copy(job) if job.action == scanner.COPY else self.run_one(job)
        except Exception as exc:  # one bad file must not stop the batch
            if job.fallback is not None and not is_disk_full(exc):
                self._keep_original(iid, job, exc)
            else:
                self._fail(iid, job, exc, str(exc))
        else:
            self.done += 1
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
        self.events.put(("kept", iid, result))

    def _fail(self, iid: str, job: scanner.Job, exc: Exception, message: str) -> None:
        self.failed_rows[iid] = (job.source, message)
        # ...except a full disk: every later file would fail the same way.
        if is_disk_full(exc):
            self.stop_reason = "the output disk is full"
        self.events.put(("failed", iid, job.source.name, message))


class BatchTab(ttk.Frame):
    """A folder-in, files-out tab.

    Subclasses implement :meth:`build_controls`, :meth:`perform_scan` and
    :meth:`run_one`, and may override :meth:`describe_result`.
    """

    #: Label used in messages, e.g. "Nothing to convert".
    verb = "process"

    def __init__(self, master: tk.Misc, app) -> None:
        super().__init__(master, padding=8)
        self.app = app
        self.events: queue.Queue = queue.Queue()
        self.worker: Worker | None = None
        # A re-do of a single image, also off the UI thread.  Never runs at the
        # same time as a batch: busy() covers both, and every entry point that
        # matters checks it.
        self.redo_worker: threading.Thread | None = None
        self.scan_result: scanner.ScanResult | None = None
        self.run_plan: runfolder.RunPlan | None = None
        # Everything that shaped the current job list, captured when it was
        # built.  Compared against the live controls before Start.
        self._scan_state: tuple | None = None
        # Row id -> job, in list order.  Worker events name the row id itself:
        # keying them by source path sent both of an image's jobs in the
        # "beside" layout — its copy and its thumbnail — to the same row.
        self.rows: dict[str, scanner.Job] = {}
        self.results: dict[str, ResultLike] = {}
        # The most recent batch to finish, so a successful re-do can clear that
        # run's failures from its incomplete marker.
        self._last_run: Worker | None = None
        self._preview_refs: list[ImageTk.PhotoImage] = []
        self._preview_size = (0, 0)
        self._preview_job: str | None = None  # a render waiting for resizing to settle

        self.status_var = StringVar(value="Pick a folder and press Scan.")
        self.progress_var = StringVar(value="")
        self.override_long_var = StringVar()
        self.override_quality_var = StringVar()

        self.build_controls()
        self._build_body()
        self.after(100, self._drain_events)

    # ------------------------------------------------------- for subclasses

    def build_controls(self) -> None:
        """Pack the tab-specific rows at the top."""
        raise NotImplementedError

    def perform_scan(self) -> scanner.ScanResult:
        """Return the jobs for the current settings, or raise ``ScanError``.

        ``self.run_plan.path`` is the folder to write into; it is planned before
        this runs and does not exist on disk yet.
        """
        raise NotImplementedError

    #: Which job the run folder name describes — see :data:`runfolder.JOB_SUFFIX`.
    job_kind = "min"

    def input_folder(self) -> Path | None:
        """The input folder as typed, or ``None`` if the field is empty."""
        raise NotImplementedError

    def output_parent(self) -> Path | None:
        """The folder the run folder goes inside, or ``None`` if not set."""
        raise NotImplementedError

    def scan_state(self) -> tuple:
        """Every control that decides what a run does, as it stands right now.

        Captured at Scan and compared again at Start.  A control that changes
        the job list — or the settings the jobs are run with — must appear here,
        or ticking it would silently do nothing for that run.  Raw widget values
        rather than parsed ones: a half-typed number still counts as a change,
        and comparing text cannot raise.
        """
        return (self.input_folder(), self.output_parent())

    def plan_run_folder(self) -> runfolder.RunPlan:
        """Reserve a name for the next run. Creates nothing on disk."""
        source = self.input_folder()
        parent = self.output_parent()
        if source is None:
            raise scanner.ScanError("Pick an input folder.")
        if parent is None:
            raise scanner.ScanError("Pick an output folder.")
        if not source.is_dir():
            raise scanner.ScanError(f"Input folder does not exist: {source}")
        try:
            return runfolder.plan(parent, source, self.job_kind)
        except runfolder.RunFolderError as exc:
            raise scanner.ScanError(str(exc)) from exc

    def ensure_ready(self) -> bool:
        """Ask for anything essential that is still missing.

        Called before both scanning and starting.  Returning ``False`` aborts
        quietly — the tab has already told the user why, or the user cancelled.
        """
        return True

    def confirm_start(self) -> bool:
        """Show exactly what is about to happen, before anything is created."""
        result, reserved = self.scan_result, self.run_plan
        if result is None or reserved is None:
            return False

        lines = [f"A new folder will be created:\n\n    {reserved.path}\n"]
        if reserved.collided:
            lines.append(
                f"A folder named '{reserved.base}' is already there,\n"
                f"so this run uses '{reserved.name}' instead.\n"
                f"Nothing already in {reserved.parent} is touched.\n"
            )
        lines.append(f"{result.to_process} image(s) will be written there.")
        if result.to_copy:
            lines.append(
                f"{result.to_copy} other file(s) will be copied across "
                f"({result.copy_bytes / 1e6:.0f} MB)."
            )
        if result.warnings:
            # The log pane is easy to miss, and low disk space in particular has
            # to be seen before the run, not discovered halfway through it.
            lines.append("\nBefore you start:")
            lines += [f"  • {warning}" for warning in result.warnings[:6]]
            if len(result.warnings) > 6:
                lines.append(f"  …and {len(result.warnings) - 6} more in the log")
        lines.append(f"\nNothing in {result.root} is modified.\n\nContinue?")
        # There is no overwrite case to warn about: the scanner skips, with a
        # warning listed above, any output that somehow already exists.
        return bool(messagebox.askokcancel(
            "Start?", "\n".join(lines),
            icon="warning" if result.low_space else "question",
        ))

    def run_one(self, job: scanner.Job) -> ResultLike:
        raise NotImplementedError

    def redo_task(
        self, job: scanner.Job, long_edge: int | None, quality: int | None
    ) -> Callable[[], ResultLike]:
        """Build the callable that re-does one image.

        Called on the UI thread; the callable it returns is run on a worker.
        Anything Tk owns — a ``StringVar``, a widget — has to be read *here* and
        closed over, because touching Tk from another thread is undefined.
        """
        raise NotImplementedError

    def describe_result(self, result: ResultLike) -> tuple[str, str]:
        """``(status, log line)`` for a finished job."""
        return "done", (
            f"{result.source.name} -> {result.output_size[0]}x{result.output_size[1]} "
            f"{result.kilobytes:.1f} KB at quality {result.quality}"
        )

    def describe_copy(self, result: CopyResult) -> tuple[str, str]:
        if result.kept_reason:
            return "kept original", (
                f"{result.source.name} -> original copied unchanged as "
                f"{result.output.name} instead ({result.kept_reason})"
            )
        return "copied", f"{result.source.name} -> copied unchanged, {result.kilobytes:.1f} KB"

    def scan_label(self, result: scanner.ScanResult) -> str:
        label = f"{result.to_process} to {self.verb}"
        if result.to_copy:
            label += f", {result.to_copy} to copy"
        if result.skipped:
            label += f", {len(result.skipped)} skipped"
        return label

    # ------------------------------------------------------------ shared UI

    def _build_body(self) -> None:
        panes = ttk.PanedWindow(self, orient="horizontal")
        panes.pack(fill="both", expand=True)

        left = ttk.Frame(panes)
        panes.add(left, weight=3)
        columns = ("status", "dimensions", "size", "quality")
        self.tree = ttk.Treeview(left, columns=columns, show="tree headings", height=12)
        self.tree.heading("#0", text="File")
        self.tree.column("#0", width=280, stretch=True)
        for name, width, heading in (
            ("status", 110, "Status"),
            ("dimensions", 110, "Dimensions"),
            ("size", 85, "Size"),
            ("quality", 70, "Quality"),
        ):
            self.tree.heading(name, text=heading)
            self.tree.column(name, width=width, anchor="w", stretch=False)
        self.tree.tag_configure("attention", foreground="#b35c00")
        self.tree.tag_configure("failed", foreground="#b00020")
        scroll = ttk.Scrollbar(left, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._render_preview())

        right = ttk.Frame(panes)
        panes.add(right, weight=2)
        self._build_preview(right)

        bottom = ttk.Frame(self)
        bottom.pack(fill="x", pady=(8, 0))
        self.progress = ttk.Progressbar(bottom, mode="determinate")
        self.progress.pack(side="left", fill="x", expand=True)
        ttk.Label(bottom, textvariable=self.progress_var, width=28, anchor="e").pack(
            side="left", padx=(8, 8)
        )
        self.start_button = ttk.Button(bottom, text="Start", command=self.start)
        self.start_button.pack(side="left")
        self.cancel_button = ttk.Button(
            bottom, text="Cancel", command=self.cancel, state="disabled"
        )
        self.cancel_button.pack(side="left", padx=(6, 0))

        log_frame = ttk.LabelFrame(self, text="Log", padding=4)
        log_frame.pack(fill="both", pady=(8, 0))
        self.log = tk.Text(log_frame, height=6, wrap="none")
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=log_scroll.set, state="disabled")
        self.log.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="right", fill="y")

    def _build_preview(self, parent: ttk.Frame) -> None:
        frame = ttk.LabelFrame(parent, text="Preview", padding=6)
        frame.pack(fill="both", expand=True)

        images = ttk.Frame(frame)
        images.pack(fill="both", expand=True)
        images.columnconfigure(0, weight=1, uniform="preview")
        images.columnconfigure(1, weight=1, uniform="preview")
        images.rowconfigure(1, weight=1)

        ttk.Label(images, text="Source", anchor="center").grid(row=0, column=0, sticky="ew")
        ttk.Label(images, text="Result", anchor="center").grid(row=0, column=1, sticky="ew")
        self.before_label = ttk.Label(images, anchor="center", relief="sunken")
        self.before_label.grid(row=1, column=0, sticky="nsew", padx=2, pady=2)
        self.after_label = ttk.Label(images, anchor="center", relief="sunken")
        self.after_label.grid(row=1, column=1, sticky="nsew", padx=2, pady=2)
        self.before_info = ttk.Label(images, anchor="center", text="")
        self.before_info.grid(row=2, column=0, sticky="ew")
        self.after_info = ttk.Label(images, anchor="center", text="")
        self.after_info.grid(row=2, column=1, sticky="ew")
        images.bind("<Configure>", self._on_preview_resize)

        override = ttk.LabelFrame(frame, text="Override selected image", padding=6)
        override.pack(fill="x", pady=(8, 0))
        ttk.Label(override, text="Long edge:").grid(row=0, column=0, sticky="w")
        ttk.Entry(override, textvariable=self.override_long_var, width=8).grid(
            row=0, column=1, padx=(4, 12)
        )
        ttk.Label(override, text="Quality:").grid(row=0, column=2, sticky="w")
        ttk.Entry(override, textvariable=self.override_quality_var, width=8).grid(
            row=0, column=3, padx=(4, 12)
        )
        self.redo_button = ttk.Button(
            override, text="Re-do selected", command=self.reencode_selected
        )
        self.redo_button.grid(row=0, column=4)
        ttk.Label(
            override,
            text="Blank = use settings. Quality skips the search and encodes once.",
            foreground="grey40",
        ).grid(row=1, column=0, columnspan=5, sticky="w", pady=(4, 0))

    # ------------------------------------------------------------- actions

    def refuse_while_busy(self) -> bool:
        """True (and tells the user) if a batch is running.

        Changing the folder, rescanning or re-doing an image mid-run would leave
        the list describing one set of jobs while the worker writes another.
        """
        if not self.busy():
            return False
        messagebox.showinfo(
            "Still running",
            "A batch is running. Wait for it to finish, or press Cancel first.",
        )
        return True

    def browse_into(self, var: StringVar, title: str) -> str | None:
        if self.refuse_while_busy():
            return None
        initial = var.get() or str(Path.home())
        chosen = filedialog.askdirectory(initialdir=initial, title=title)
        if chosen:
            var.set(chosen)
        return chosen or None

    def scan(self) -> None:
        if self.refuse_while_busy():
            return
        if not self.ensure_ready():
            return
        try:
            self.run_plan = self.plan_run_folder()
            result = self.perform_scan()
        except scanner.ScanError as exc:
            self.run_plan = None
            self.scan_result = None
            messagebox.showwarning("Cannot scan", str(exc))
            self.append_log(f"Scan refused: {exc}")
            return
        except OSError as exc:
            self.run_plan = None
            self.scan_result = None
            messagebox.showerror("Cannot scan", str(exc))
            return

        self._show_scan(result)

    def _show_scan(self, result: scanner.ScanResult) -> None:
        self.scan_result = result
        # Read *after* the scan: perform_scan is what folds the widgets into the
        # settings objects, so before it the two would not agree.
        self._scan_state = self.scan_state()
        self.tree.delete(*self.tree.get_children())
        self.rows.clear()
        self.results.clear()
        for job in result.jobs:
            try:
                label = str(job.source.relative_to(result.root))
            except ValueError:
                label = job.source.name
            iid = self.tree.insert(
                "", "end", text=label,
                values=("to copy" if job.action == scanner.COPY else "pending", "", "", ""),
            )
            self.rows[iid] = job

        self.status_var.set(self.scan_label(result))
        self.append_log(f"Scanned {result.root}: {self.scan_label(result)}")
        self.append_log(f"  will create: {result.destination}")
        if self.run_plan is not None and self.run_plan.collided:
            self.append_log(
                f"  note: '{self.run_plan.base}' already exists, so this run will "
                f"use '{self.run_plan.name}' instead"
            )
        if result.to_copy:
            self.append_log(
                f"  copying {result.to_copy} file(s), {result.copy_bytes / 1e6:.0f} MB"
            )
        for warning in result.warnings:
            self.append_log(f"  note: {warning}")
        self.progress.configure(value=0, maximum=max(1, len(result.jobs)))
        self.progress_var.set("")

    def scan_is_stale(self) -> bool:
        """Have the controls moved on since the scan that built this list?

        Starting then would run the old jobs while the controls describe
        different ones — untick "Include subfolders" after a scan and the
        subfolders would still be processed, with nothing to say so.  Folders
        are checked against the scan result itself; everything else through
        :meth:`scan_state`.  A re-scan is cheap; guessing is not.
        """
        if self.scan_result is None or self.run_plan is None:
            return True
        return (
            self.input_folder() != self.scan_result.root
            or self.output_parent() != self.run_plan.parent
            or self.scan_state() != self._scan_state
        )

    def _refresh_plan(self) -> bool:
        """Re-plan and re-scan if the reserved name was taken since the scan.

        The gap between Scan and Start is however long the user takes to read the
        dialog, so the name can go stale.  Re-scanning is what retargets every
        job's output path at the new folder.
        """
        if self.run_plan is None:
            return False
        if not runfolder.taken(self.run_plan.path):
            return True
        try:
            self.run_plan = self.plan_run_folder()
            self._show_scan(self.perform_scan())
        except (scanner.ScanError, OSError) as exc:
            messagebox.showerror("Cannot start", str(exc))
            self.append_log(f"Start refused: {exc}")
            return False
        return True

    def start(self) -> None:
        if self.busy():
            return
        if not self.ensure_ready():
            return
        if not self.scan_result or not self.scan_result.jobs:
            messagebox.showinfo(
                "Nothing to do", f"Scan a folder with images to {self.verb} first."
            )
            return
        if self.scan_is_stale():
            messagebox.showinfo(
                "Scan again first",
                "The folders or settings changed since the last scan.\n"
                "Press Scan so the list matches the options you have chosen.",
            )
            return
        if not self._refresh_plan():
            return
        if not self.confirm_start():
            self.append_log("Cancelled before creating anything.")
            return

        # Nothing has touched the disk until this line.
        try:
            created = runfolder.create(self.run_plan)
        except runfolder.RunFolderError as exc:
            messagebox.showerror("Cannot create the output folder", str(exc))
            self.append_log(f"Start refused: {exc}")
            return

        if created.path != self.run_plan.path:
            # Lost a race for the name in the last instant.  Re-scan so every
            # job points at the folder that actually got created.
            self.append_log(
                f"'{self.run_plan.name}' was taken at the last moment; "
                f"using '{created.name}' instead."
            )
            self.run_plan = created
            try:
                self._show_scan(self.perform_scan())
            except (scanner.ScanError, OSError) as exc:
                runfolder.discard_if_empty(created.path)
                messagebox.showerror("Cannot start", str(exc))
                return

        self.append_log(f"Created {created.path}")
        # Written first and removed last: whatever stops this run — Cancel, a
        # crash, a power cut, the window closing — the folder says it is not a
        # complete copy.  Failing to write it is the last writability check.
        try:
            runfolder.write_marker(created.path, marker_text(
                self.scan_result.root,
                "started, not finished - if minjpg is no longer running, the run "
                "was interrupted (the app closed, crashed or lost power)",
            ))
        except OSError as exc:
            runfolder.discard_empty_tree(created.path)
            messagebox.showerror("Cannot start", f"Cannot write into {created.path}:\n{exc}")
            self.append_log(f"Start refused: {exc}")
            return
        try:
            for directory in self.scan_result.empty_dirs:
                directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.append_log(f"  note: could not mirror an empty folder: {exc}")

        for iid in self.rows:
            self.tree.set(iid, "status", "queued")
            self.tree.item(iid, tags=())
        self.start_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.progress.configure(value=0, maximum=len(self.scan_result.jobs))

        self.worker = Worker(
            list(self.rows.items()), self.run_one, self.events,
            self.scan_result.root, created.path,
        )
        self.worker.start()

    def cancel(self) -> None:
        if self.worker:
            self.worker.cancelled.set()
            self.append_log("Cancelling after the current image…")

    def reencode_selected(self) -> None:
        # A redo writes the same output path the worker may be writing right now.
        if self.refuse_while_busy():
            return
        # A redo goes straight to the pipeline, and write_atomic creates missing
        # parents — so before Start it would conjure the run folder into being
        # without the confirmation that is supposed to precede it, and Start
        # would then find the name taken and quietly move to the next one,
        # stranding whatever the redo wrote.  Only ever re-do into a folder the
        # user has already agreed to.
        if self.run_plan is None or not self.run_plan.path.is_dir():
            messagebox.showinfo(
                "Run the batch first",
                "Re-do writes into the run folder, and that is only created "
                "when you press Start.\n\nRun the batch, then re-do individual "
                "images to try different settings on them.",
            )
            return
        selection = self.tree.selection()
        if not selection:
            messagebox.showinfo("No selection", "Select an image in the list first.")
            return
        iid = selection[0]
        job = self.rows[iid]
        if job.action == scanner.COPY:
            messagebox.showinfo(
                "Nothing to re-do",
                "That row is a file copied across unchanged, not an encoded image.",
            )
            return

        try:
            long_text = self.override_long_var.get().strip()
            quality_text = self.override_quality_var.get().strip()
            long_edge = int(long_text) if long_text else None
            quality = int(quality_text) if quality_text else None
        except ValueError:
            messagebox.showerror("Invalid override", "Long edge and quality must be whole numbers.")
            return
        if quality is not None and not 1 <= quality <= 100:
            messagebox.showerror("Invalid override", "Quality must be between 1 and 100.")
            return
        if long_edge is not None and long_edge < 16:
            messagebox.showerror("Invalid override", "Long edge must be at least 16 px.")
            return

        # Decoding and resizing a full-size photo takes seconds, and doing it
        # here would freeze the whole window — Tk cannot repaint while a
        # callback is running, so the app reads as hung.  Same treatment as a
        # batch: off to a thread, back through the event queue.
        try:
            task = self.redo_task(job, long_edge, quality)
        except scanner.ScanError as exc:
            messagebox.showerror("Re-do failed", str(exc))
            return

        def work() -> None:
            try:
                result = task()
            except Exception as exc:  # reported on the UI thread, not here
                self.events.put(("redo_failed", iid, job.source, str(exc)))
            else:
                self.events.put(("redone", iid, result))

        self.redo_button.configure(state="disabled")
        self.tree.set(iid, "status", "working")
        self.progress_var.set(f"re-doing {job.source.name[:24]}")
        self.redo_worker = threading.Thread(target=work, daemon=True)
        self.redo_worker.start()

    # -------------------------------------------------------------- events

    def _drain_events(self) -> None:
        try:
            while True:
                try:
                    event = self.events.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._handle_event(event)
                except Exception:
                    # One bad event must not take the pump down with it: every
                    # later one — "finished" above all — still has to land, or
                    # the tab sits with Start disabled until a restart.
                    get_logger().exception("could not handle a %r event", event[0])
                    self.append_log(
                        f"Internal error while handling '{event[0]}'; details are in the log file."
                    )
        finally:
            self.after(100, self._drain_events)

    def _handle_event(self, event: tuple) -> None:
        kind = event[0]
        if kind == "progress":
            _, worker, iid, index, total, name = event
            if worker is self.worker:
                self.progress.configure(value=index)
                self.progress_var.set(f"{index + 1} / {total}  {name[:24]}")
            if iid in self.rows:
                self.tree.set(iid, "status", "working")
        elif kind in ("done", "kept"):
            # A row id no longer in the tree means the list was rebuilt after
            # the run ended, while its last events were still queued.
            if event[1] in self.rows:
                self.record_result(event[1], event[2])
        elif kind == "failed":
            _, iid, name, message = event
            if iid in self.rows:
                self.tree.set(iid, "status", "failed")
                self.tree.item(iid, tags=("failed",))
            self.append_log(f"FAILED {name}: {message}")
        elif kind in ("redone", "redo_failed"):
            self._finish_redo(event)
        elif kind == "finished":
            self._finish_run(event[1])

    def _finish_redo(self, event: tuple) -> None:
        """Land a finished re-do back on the UI thread."""
        self.redo_button.configure(state="normal")
        self.progress_var.set("")
        iid = event[1]
        if iid not in self.rows:
            return  # the list was rebuilt underneath it; nothing to update
        previous = self.results.get(iid)
        if event[0] == "redone":
            self._drop_kept_copy(previous, event[2])
            self.record_result(iid, event[2])
            self._clear_run_failure(iid)
            self._render_preview()
            return
        source, message = event[2], event[3]
        if previous is not None:
            # A failed re-do never replaces the file the batch wrote, so the row
            # keeps describing that file rather than claiming it is gone.
            self._show_row(iid, previous)
        else:
            self.tree.set(iid, "status", "failed")
            self.tree.item(iid, tags=("failed",))
        self.append_log(f"Re-do FAILED {Path(source).name}: {message}")
        messagebox.showerror("Re-do failed", message)

    # ------------------------------------------------------------ run end

    def _finish_run(self, worker: Worker) -> None:
        """Wrap up a batch: buttons, the marker, and a summary if one is due."""
        self._last_run = worker
        if worker.stop_reason:
            state = "stopped"
        elif worker.processed < worker.total:
            state = "cancelled"
        else:
            state = "finished"
        # A run that ended just as another started must not reset the new one.
        if worker is self.worker:
            if state == "finished":
                self.progress.configure(value=self.progress["maximum"])
            self.progress_var.set(state)
            self.start_button.configure(state="normal")
            self.cancel_button.configure(state="disabled")
        self.append_log(
            f"{state.capitalize()}: {worker.done} written, {worker.kept} kept as "
            f"originals, {len(worker.failed_rows)} failed"
            + (f" — {worker.stop_reason}" if worker.stop_reason else "")
        )
        if worker.done + worker.kept == 0:
            self._tidy_empty_run(worker)
        else:
            self._update_marker(worker)
        if worker.kept or worker.failed_rows or worker.stop_reason:
            self._summarise(worker)

    @staticmethod
    def _run_complete(worker: Worker) -> bool:
        return worker.processed == worker.total and not worker.failed_rows

    @staticmethod
    def _status_line(worker: Worker) -> str:
        if worker.stop_reason:
            return (f"stopped after {worker.processed} of {worker.total} files: "
                    f"{worker.stop_reason}")
        if worker.processed < worker.total:
            return f"cancelled after {worker.processed} of {worker.total} files"
        return f"finished, but {len(worker.failed_rows)} file(s) could not be written"

    def _update_marker(self, worker: Worker) -> None:
        """Remove the incomplete marker, or rewrite it to say what is missing."""
        try:
            if self._run_complete(worker):
                runfolder.remove_marker(worker.run_root)
                return
            failures = [
                (_relative(source, worker.source_root), message)
                for source, message in worker.failed_rows.values()
            ]
            runfolder.write_marker(
                worker.run_root,
                marker_text(worker.source_root, self._status_line(worker), failures),
            )
        except OSError as exc:
            self.append_log(f"  note: could not update {runfolder.MARKER_NAME}: {exc}")
            return
        self.append_log(
            f"  {worker.run_root.name} is marked incomplete: see {runfolder.MARKER_NAME} in it"
        )

    def _tidy_empty_run(self, worker: Worker) -> None:
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
            self.append_log(f"Removed the empty folder {worker.run_root}")

    def _summarise(self, worker: Worker) -> None:
        """Say plainly, once, what did not go to plan — the log is easy to miss."""
        lines = []
        if worker.stop_reason:
            lines.append(
                f"The run stopped early: {worker.stop_reason}.\n"
                f"{worker.total - worker.processed} file(s) were not processed.\n"
            )
        lines.append(f"{worker.done} file(s) written.")
        if worker.kept:
            lines.append(
                f"{worker.kept} image(s) were copied across unchanged instead of "
                "converted: unreadable, or multi-page and kept whole."
            )
        if worker.failed_rows:
            lines.append(f"{len(worker.failed_rows)} file(s) failed and are NOT in the new folder.")
        if worker.done + worker.kept == 0:
            lines.append("\nNothing was written, so the new folder was removed again.")
        elif not self._run_complete(worker):
            lines.append(
                f"\nThe folder is marked incomplete: {runfolder.MARKER_NAME} "
                "inside it lists what is missing."
            )
        lines.append("\nThe affected rows are highlighted in the list.")
        show = (messagebox.showerror if worker.failed_rows or worker.stop_reason
                else messagebox.showwarning)
        show("Run finished with problems", "\n".join(lines))

    def _drop_kept_copy(self, previous: ResultLike | None, result: ResultLike) -> None:
        """After a successful re-do, remove the original that stood in for it.

        Only ever a file this run copied into its own folder — the real original
        is untouched in the input — and only when the new result went somewhere
        else; a JPEG's stand-in sat on its own output name and is now replaced.
        """
        if not (isinstance(previous, CopyResult) and previous.kept_reason):
            return
        if previous.output == result.output or self.run_plan is None:
            return
        if not previous.output.is_relative_to(self.run_plan.path):
            return
        try:
            previous.output.unlink()
        except OSError as exc:
            self.append_log(f"  note: could not remove {previous.output.name}: {exc}")
        else:
            self.append_log(f"  removed {previous.output.name}, the unconverted copy it replaces")

    def _clear_run_failure(self, iid: str) -> None:
        """A re-do fixed a row the last run failed: keep its marker truthful."""
        run = self._last_run
        if run is None or run.failed_rows.pop(iid, None) is None:
            return
        if self.run_plan is not None and run.run_root == self.run_plan.path:
            self._update_marker(run)

    def record_result(self, iid: str, result: ResultLike) -> None:
        self.results[iid] = result
        _status, message = self._show_row(iid, result)
        self.append_log(message)

    def _show_row(self, iid: str, result: ResultLike) -> tuple[str, str]:
        """Put ``result`` into its row; returns ``(status, log line)``."""
        if isinstance(result, CopyResult):
            status, message = self.describe_copy(result)
        else:
            status, message = self.describe_result(result)
        self.tree.set(iid, "status", status)
        self.tree.set(iid, "dimensions", (
            f"{result.output_size[0]}x{result.output_size[1]}"
            if result.output_size[0] else "—"
        ))
        self.tree.set(iid, "size", f"{result.kilobytes:.1f} KB")
        self.tree.set(iid, "quality", str(result.quality) if result.quality else "—")
        self.tree.item(iid, tags=("attention",) if status in ATTENTION else ())
        return status, message

    # ------------------------------------------------------------- preview

    def _on_preview_resize(self, event) -> None:
        size = (event.width // 2 - 8, event.height - 44)
        if abs(size[0] - self._preview_size[0]) > 16 or abs(size[1] - self._preview_size[1]) > 16:
            self._preview_size = size
            # Dragging the divider fires this many times a second, and every
            # render decodes both images again: wait until it settles.
            if self._preview_job is not None:
                self.after_cancel(self._preview_job)
            self._preview_job = self.after(150, self._render_settled_preview)

    def _render_settled_preview(self) -> None:
        self._preview_job = None
        self._render_preview()

    def _render_preview(self) -> None:
        selection = self.tree.selection()
        self._preview_refs.clear()
        if not selection:
            self._set_preview(self.before_label, self.before_info, None, "")
            self._set_preview(self.after_label, self.after_info, None, "")
            return

        job = self.rows[selection[0]]
        box = (
            max(PREVIEW_MIN[0], self._preview_size[0]),
            max(PREVIEW_MIN[1], self._preview_size[1]),
        )
        # A kept original lives under its own name, not the job's output name.
        result = self.results.get(selection[0])
        self._show(self.before_label, self.before_info, job.source, box)
        self._show(self.after_label, self.after_info, result.output if result else job.output, box)

    def _show(self, label: ttk.Label, info: ttk.Label, path: Path, box: tuple[int, int]) -> None:
        if not path.is_file():
            self._set_preview(label, info, None, "not generated yet")
            return
        try:
            with Image.open(path) as opened:
                size = opened.size  # before draft(), which shrinks what it reports
                # A JPEG decodes straight at 1/2, 1/4 or 1/8 scale; a full decode
                # of a large photo here, on the UI thread, froze the window.  A
                # no-op for formats that cannot do it.
                opened.draft(None, box)
                opened.load()
                thumb = formats.to_8bit(opened).convert("RGB")  # 16-bit would show white
                thumb.thumbnail(box, Image.LANCZOS)
                thumb, size = _upright(thumb, size)
                photo = ImageTk.PhotoImage(thumb)
            byte_size = path.stat().st_size  # inside: the file can vanish meanwhile
        except Exception as exc:
            self._set_preview(label, info, None, f"cannot preview: {exc}")
            return

        self._preview_refs.append(photo)
        self._set_preview(
            label, info, photo, f"{size[0]}x{size[1]} · {byte_size / 1024:.1f} KB",
        )

    @staticmethod
    def _set_preview(label: ttk.Label, info: ttk.Label, photo, text: str) -> None:
        label.configure(image=photo or "")
        label.image = photo
        info.configure(text=text)

    # ---------------------------------------------------------------- misc

    def append_log(self, message: str) -> None:
        # Mirrored to the log file: the in-app pane dies with the window, and a
        # support request needs the per-file detail that was in it.
        get_logger().info("[%s] %s", self.verb, message)
        self.log.configure(state="normal")
        self.log.insert("end", message + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def busy(self) -> bool:
        return bool(
            (self.worker and self.worker.is_alive())
            or (self.redo_worker and self.redo_worker.is_alive())
        )

    @staticmethod
    def int_field(parent: ttk.Frame, label: str, var: StringVar, width: int = 8) -> None:
        ttk.Label(parent, text=label).pack(side="left")
        ttk.Entry(parent, textvariable=var, width=width).pack(side="left", padx=(4, 12))

    @staticmethod
    def check_field(parent: ttk.Frame, label: str, var: BooleanVar) -> None:
        ttk.Checkbutton(parent, text=label, variable=var).pack(side="left", padx=(0, 14))
