"""Shared machinery for the two batch tabs.

Both tabs do the same dance — scan a folder, run the jobs on a worker thread,
show results in a tree with a before/after preview, allow a per-image redo — and
differ only in *what* the job does and which controls sit at the top.  That
shared part lives here; each tab supplies the rest.
"""

from __future__ import annotations

import queue
import threading
from pathlib import Path
from tkinter import BooleanVar, StringVar, filedialog, messagebox, ttk
import tkinter as tk
from typing import Callable, Protocol

from PIL import Image, ImageTk

from . import runfolder, scanner
from .common import copy_atomic
from .logging_setup import get_logger

PREVIEW_MIN = (240, 180)


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

    @property
    def kilobytes(self) -> float:
        return self.byte_size / 1024


def run_copy(job: scanner.Job) -> CopyResult:
    """Copy one file across, then confirm it really arrived intact.

    ``copy_atomic`` writes via a temp file so an interrupted copy cannot leave a
    truncated file in place.  The size check afterwards catches the case that
    would otherwise be silent: a copy that ran out of disk part-way and still
    returned without raising.
    """
    copy_atomic(job.source, job.output)
    expected = job.source.stat().st_size
    actual = job.output.stat().st_size
    if actual != expected:
        raise OSError(
            f"copy is {actual} bytes but the source is {expected} — "
            "the destination may be full"
        )
    return CopyResult(job.source, job.output, actual)


class Worker(threading.Thread):
    """Runs a batch off the UI thread, reporting progress over a queue."""

    def __init__(
        self,
        jobs: list[scanner.Job],
        run_one: Callable[[scanner.Job], ResultLike],
        events: queue.Queue,
    ):
        super().__init__(daemon=True)
        self.jobs = jobs
        self.run_one = run_one
        self.events = events
        self.cancelled = threading.Event()

    def run(self) -> None:
        done = failed = 0
        for index, job in enumerate(self.jobs):
            if self.cancelled.is_set():
                break
            self.events.put(("progress", index, len(self.jobs), job.source))
            try:
                result = run_copy(job) if job.action == scanner.COPY else self.run_one(job)
            except Exception as exc:  # one bad file must not stop the batch
                failed += 1
                self.events.put(("failed", job.source, str(exc)))
            else:
                done += 1
                self.events.put(("done", result))
        self.events.put(("finished", done, failed, self.cancelled.is_set()))


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
        self.scan_result: scanner.ScanResult | None = None
        self.run_plan: runfolder.RunPlan | None = None
        self.rows: dict[str, scanner.Job] = {}
        self.results: dict[str, ResultLike] = {}
        # Source path -> row id.  A linear search per event made progress
        # reporting quadratic, and a full-tree mirror multiplies the row count.
        self.row_for_source: dict[Path, str] = {}
        self._preview_refs: list[ImageTk.PhotoImage] = []
        self._preview_size = (0, 0)

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
        lines.append(f"\nNothing in {result.root} is modified.\n\nContinue?")

        # Cannot happen with a folder created fresh for this run.  If it ever
        # does, the guarantee this whole design rests on has broken, so say so
        # loudly rather than quietly overwriting the user's files.
        if result.overwrites:
            get_logger().error(
                "INVARIANT BROKEN: %d job(s) target existing files in the fresh "
                "run folder %s", result.overwrites, reserved.path,
            )
            lines.insert(1, (
                f"WARNING: {result.overwrites} existing file(s) would be "
                "OVERWRITTEN. This should be impossible in a new folder — "
                "check the folder before continuing.\n"
            ))

        return bool(messagebox.askokcancel(
            "Start?", "\n".join(lines),
            icon="warning" if result.overwrites else "question",
        ))

    def run_one(self, job: scanner.Job) -> ResultLike:
        raise NotImplementedError

    def redo_one(
        self, job: scanner.Job, long_edge: int | None, quality: int | None
    ) -> ResultLike:
        raise NotImplementedError

    def describe_result(self, result: ResultLike) -> tuple[str, str]:
        """``(status, log line)`` for a finished job."""
        return "done", (
            f"{result.source.name} -> {result.output_size[0]}x{result.output_size[1]} "
            f"{result.kilobytes:.1f} KB at quality {result.quality}"
        )

    def describe_copy(self, result: CopyResult) -> tuple[str, str]:
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
        ttk.Button(override, text="Re-do selected", command=self.reencode_selected).grid(
            row=0, column=4
        )
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
        self.tree.delete(*self.tree.get_children())
        self.rows.clear()
        self.results.clear()
        self.row_for_source.clear()
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
            self.row_for_source.setdefault(job.source, iid)

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
        """Have the folder fields moved on since the scan that built this list?

        Starting then would run the jobs from the old folders while the header
        describes the new ones — the user would be told one thing and given
        another.  A re-scan is cheap; guessing is not.
        """
        if self.scan_result is None or self.run_plan is None:
            return True
        return (
            self.input_folder() != self.scan_result.root
            or self.output_parent() != self.run_plan.parent
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
        if self.worker and self.worker.is_alive():
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
                "The input or output folder changed since the last scan.\n"
                "Press Scan so the list matches the folders you have chosen.",
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

        self.worker = Worker(list(self.scan_result.jobs), self.run_one, self.events)
        self.worker.start()

    def cancel(self) -> None:
        if self.worker:
            self.worker.cancelled.set()
            self.append_log("Cancelling after the current image…")

    def reencode_selected(self) -> None:
        # A redo writes the same output path the worker may be writing right now.
        if self.refuse_while_busy():
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

        try:
            result = self.redo_one(job, long_edge, quality)
        except Exception as exc:
            self.tree.set(iid, "status", "failed")
            self.tree.item(iid, tags=("failed",))
            self.append_log(f"FAILED {job.source.name}: {exc}")
            messagebox.showerror("Re-do failed", str(exc))
            return

        self.record_result(iid, result)
        self._render_preview()

    # -------------------------------------------------------------- events

    def _drain_events(self) -> None:
        try:
            while True:
                self._handle_event(self.events.get_nowait())
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

    def _handle_event(self, event: tuple) -> None:
        kind = event[0]
        if kind == "progress":
            index, total, source = event[1], event[2], event[3]
            self.progress.configure(value=index)
            self.progress_var.set(f"{index + 1} / {total}  {source.name[:24]}")
            iid = self._iid_for(source)
            if iid:
                self.tree.set(iid, "status", "working")
        elif kind == "done":
            iid = self._iid_for(event[1].source)
            if iid:
                self.record_result(iid, event[1])
        elif kind == "failed":
            source, message = event[1], event[2]
            iid = self._iid_for(source)
            if iid:
                self.tree.set(iid, "status", "failed")
                self.tree.item(iid, tags=("failed",))
            self.append_log(f"FAILED {Path(source).name}: {message}")
        elif kind == "finished":
            done, failed, cancelled = event[1], event[2], event[3]
            self.progress.configure(value=self.progress["maximum"])
            self.progress_var.set("cancelled" if cancelled else "finished")
            self.start_button.configure(state="normal")
            self.cancel_button.configure(state="disabled")
            self.append_log(
                f"{'Cancelled' if cancelled else 'Finished'}: {done} written, {failed} failed"
            )
            self._tidy_run_folder(done)

    def _tidy_run_folder(self, done: int) -> None:
        """Leave nothing behind when a run wrote nothing.

        Only ever removes the run folder while it is still empty, so a run that
        did produce something is never cleaned up from under the user.
        """
        if done or self.run_plan is None:
            return
        for directory in sorted(self.scan_result.empty_dirs if self.scan_result else [],
                                key=lambda p: len(p.parts), reverse=True):
            runfolder.discard_if_empty(directory)
        if runfolder.discard_if_empty(self.run_plan.path):
            self.append_log(f"Removed the empty folder {self.run_plan.path}")

    def _iid_for(self, source: Path) -> str | None:
        return self.row_for_source.get(Path(source))

    def record_result(self, iid: str, result: ResultLike) -> None:
        self.results[iid] = result
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
        self.tree.item(iid, tags=("attention",) if status in ("over cap", "at floor") else ())
        self.append_log(message)

    # ------------------------------------------------------------- preview

    def _on_preview_resize(self, event) -> None:
        size = (event.width // 2 - 8, event.height - 44)
        if abs(size[0] - self._preview_size[0]) > 16 or abs(size[1] - self._preview_size[1]) > 16:
            self._preview_size = size
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
        self._show(self.before_label, self.before_info, job.source, box)
        self._show(self.after_label, self.after_info, job.output, box)

    def _show(self, label: ttk.Label, info: ttk.Label, path: Path, box: tuple[int, int]) -> None:
        if not path.is_file():
            self._set_preview(label, info, None, "not generated yet")
            return
        try:
            with Image.open(path) as opened:
                opened.load()
                size = opened.size
                thumb = opened.convert("RGB")
                thumb.thumbnail(box, Image.LANCZOS)
                photo = ImageTk.PhotoImage(thumb)
        except Exception as exc:
            self._set_preview(label, info, None, f"cannot preview: {exc}")
            return

        self._preview_refs.append(photo)
        self._set_preview(
            label, info, photo,
            f"{size[0]}x{size[1]} · {path.stat().st_size / 1024:.1f} KB",
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
        return bool(self.worker and self.worker.is_alive())

    @staticmethod
    def int_field(parent: ttk.Frame, label: str, var: StringVar, width: int = 8) -> None:
        ttk.Label(parent, text=label).pack(side="left")
        ttk.Entry(parent, textvariable=var, width=width).pack(side="left", padx=(4, 12))

    @staticmethod
    def check_field(parent: ttk.Frame, label: str, var: BooleanVar) -> None:
        ttk.Checkbutton(parent, text=label, variable=var).pack(side="left", padx=(0, 14))
