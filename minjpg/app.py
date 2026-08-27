"""Tkinter front end: batch ``_min.jpg`` generation and format conversion."""

from __future__ import annotations

import dataclasses
import traceback
from pathlib import Path
from tkinter import BooleanVar, StringVar, Tk, messagebox, ttk

from . import __version__, convert, encoder, formats, pipeline, scanner
from .batchtab import BatchTab
from .logging_setup import describe_log, get_logger, setup_logging
from .config import (
    LAYOUT_BESIDE,
    LAYOUT_SUBFOLDER,
    MIN_SUBDIR,
    ConvertSettings,
    Settings,
    load_convert_settings,
    load_settings,
    save_settings,
)


class FolderTab(BatchTab):
    """A tab whose input and output folders must both be chosen explicitly.

    Neither folder has a default and neither is guessed.  Everything a run
    writes goes inside a folder created for that run alone, and the header
    always spells out the folder that is about to be created, so the
    destination is never a surprise.
    """

    #: One-line description shown above the folder fields.
    banner = ""

    def build_folder_header(self) -> None:
        ttk.Label(self, text=self.banner, foreground="grey30", justify="left").pack(
            anchor="w", pady=(0, 6)
        )
        for label, var, title in (
            ("Input folder:", self.input_var, "Pick the folder of images to read"),
            ("Output folder:", self.output_var, "Pick where the new folder should be created"),
        ):
            row = ttk.Frame(self)
            row.pack(fill="x", pady=(0, 4))
            ttk.Label(row, text=label, width=13).pack(side="left")
            ttk.Entry(row, textvariable=var).pack(
                side="left", fill="x", expand=True, padx=(6, 6)
            )
            ttk.Button(
                row, text="Browse…",
                command=lambda v=var, t=title: self._browse_folder(v, t),
            ).pack(side="left")

    def build_destination_label(self) -> None:
        ttk.Label(
            self, textvariable=self.destination_var, foreground="#1a5c1a",
        ).pack(fill="x", pady=(0, 6))
        for var in (self.input_var, self.output_var):
            var.trace_add("write", lambda *_: self.update_destination())
        self.update_destination()

    def _browse_folder(self, var: StringVar, title: str) -> None:
        if self.browse_into(var, title) and self.input_var.get() and self.output_var.get():
            self.scan()

    def input_folder(self) -> Path | None:
        text = self.input_var.get().strip()
        return Path(text).expanduser() if text else None

    def output_parent(self) -> Path | None:
        text = self.output_var.get().strip()
        return Path(text).expanduser() if text else None

    def update_destination(self) -> None:
        """Name the folder that will be created, or say what is still missing."""
        source, parent = self.input_folder(), self.output_parent()
        if source is None and parent is None:
            self.destination_var.set("Pick an input folder and an output folder.")
            return
        if source is None:
            self.destination_var.set("Pick an input folder.")
            return
        if parent is None:
            self.destination_var.set("Pick an output folder.")
            return
        try:
            reserved = self.plan_run_folder()
        except (scanner.ScanError, OSError) as exc:
            self.destination_var.set(str(exc))
            return
        note = f"   (a folder named '{reserved.base}' already exists)" if reserved.collided else ""
        self.destination_var.set(f"Will create:  {reserved.path}{note}")

    def ensure_ready(self) -> bool:
        """Never touch anything without both folders named."""
        for folder, complaint in (
            (self.input_folder(), "Pick an input folder first."),
            (self.output_parent(), "Pick an output folder first."),
        ):
            if folder is None:
                messagebox.showinfo("Folder needed", complaint)
                return False
        return True


class ThumbnailTab(FolderTab):
    """Thumbnails: web original -> ``_min.jpg`` under the byte budget."""

    verb = "compress"
    job_kind = "min"
    banner = (
        "Makes small *_min.jpg thumbnails — every one under 70 KB, long edge 1280 px.\n"
        "Originals are never modified; everything is written into a new folder."
    )

    def __init__(self, master, app) -> None:
        self.settings = app.settings
        self.input_var = StringVar(value=self.settings.last_folder)
        self.output_var = StringVar(value=self.settings.output_parent)
        self.recursive_var = BooleanVar(value=self.settings.recursive)
        self.force_var = BooleanVar(value=self.settings.force)
        self.jpeg_only_var = BooleanVar(value=self.settings.jpeg_only)
        self.destination_var = StringVar()
        self.layout_var = StringVar(value=self.settings.min_layout)
        super().__init__(master, app)

    def build_controls(self) -> None:
        self.build_folder_header()

        options = ttk.Frame(self)
        options.pack(fill="x", pady=(6, 2))
        self.check_field(options, "Include subfolders", self.recursive_var)
        self.check_field(options, "Force re-encode", self.force_var)
        self.check_field(options, "JPEG sources only", self.jpeg_only_var)
        ttk.Button(options, text="Scan", command=self.scan).pack(side="left")
        ttk.Label(options, textvariable=self.status_var).pack(side="right")

        self.layout_label = ttk.Label(self, text="", foreground="grey40")
        self.layout_label.pack(fill="x")
        self.build_destination_label()

    def update_destination(self) -> None:
        # The layout decides the shape of the run folder, so the hint has to move
        # with it — it is set on the Settings tab, out of sight from here.
        self.layout_var.set(self.app.settings.min_layout)
        if self.layout_var.get() == LAYOUT_BESIDE:
            self.layout_label.configure(
                text=f"Layout: the whole input folder is copied across, with each "
                     f"*_min.jpg next to its original.  (Settings tab)"
            )
        else:
            self.layout_label.configure(
                text=f"Layout: thumbnails only, in a {MIN_SUBDIR}/ folder mirroring "
                     f"the input structure.  (Settings tab)"
            )
        super().update_destination()

    def scan_state(self) -> tuple:
        # min_layout lives on the Settings tab, out of sight from here, and
        # decides the whole shape of the run folder — so it counts too.
        return super().scan_state() + (
            self.recursive_var.get(),
            self.force_var.get(),
            self.jpeg_only_var.get(),
            self.app.settings.min_layout,
        )

    def perform_scan(self) -> scanner.ScanResult:
        folder = self.input_folder()
        if folder is None or not folder.is_dir():
            raise scanner.ScanError("Pick an existing input folder first.")
        self.settings = self.app.settings
        self.settings.recursive = self.recursive_var.get()
        self.settings.force = self.force_var.get()
        self.settings.jpeg_only = self.jpeg_only_var.get()
        self.settings.output_parent = self.output_var.get().strip()
        self.settings.last_folder = str(folder)
        save_settings(settings=self.settings)
        return scanner.scan_min(folder, self.run_plan.path, self.settings)

    def run_one(self, job: scanner.Job):
        return pipeline.compress(job.source, job.output, self.app.settings)

    def redo_task(self, job: scanner.Job, long_edge, quality):
        # A plain dataclass, so the worker can read it; nothing Tk-owned here.
        settings = self.app.settings
        return lambda: pipeline.compress(
            job.source, job.output, settings, long_edge, quality
        )

    def describe_result(self, result) -> tuple[str, str]:
        status = "at floor" if result.hit_quality_floor else "done"
        if result.shrink_rounds and not result.hit_quality_floor:
            status = "shrunk"
        detail = f"{result.encode_count} encode" + ("s" if result.encode_count != 1 else "")
        if result.shrink_rounds:
            detail += f", {result.shrink_rounds} shrink round" + (
                "s" if result.shrink_rounds != 1 else ""
            )
        return status, (
            f"{result.source.name} -> {result.output_size[0]}x{result.output_size[1]} "
            f"{result.kilobytes:.1f} KB at quality {result.quality} ({detail})"
        )


class CompressTab(FolderTab):
    """Regular compression: any format -> a web-ready sRGB JPEG."""

    verb = "compress"
    job_kind = "compress"
    banner = (
        "Compresses full-size images — any format in, sRGB JPEG out, "
        "long edge 3840 px, around 600 KB.  Originals are never modified.\n"
        "The new folder becomes a full copy of the input: every image replaced by its "
        "compressed JPEG, every other file carried across untouched."
    )

    def __init__(self, master, app) -> None:
        settings = app.convert_settings
        self.input_var = StringVar(value=settings.input_folder)
        self.output_var = StringVar(value=settings.output_parent)
        self.destination_var = StringVar()
        self.long_edge_var = StringVar(value=str(settings.max_long_edge))
        self.quality_var = StringVar(value=str(settings.quality))
        self.max_size_var = StringVar(value=str(settings.max_size // 1024))
        self.smoothing_var = StringVar(value=str(settings.smoothing))
        self.strip_var = BooleanVar(value=settings.strip_metadata)
        self.recursive_var = BooleanVar(value=settings.recursive)
        self.force_var = BooleanVar(value=settings.force)
        self.passthrough_var = BooleanVar(value=settings.passthrough)
        super().__init__(master, app)

    def build_controls(self) -> None:
        self.build_folder_header()

        first = ttk.Frame(self)
        first.pack(fill="x", pady=(6, 2))
        self.int_field(first, "Max long edge:", self.long_edge_var)
        self.int_field(first, "Quality:", self.quality_var, width=5)
        self.int_field(first, "Max size (KB, 0 = off):", self.max_size_var)
        self.int_field(first, "Smoothing:", self.smoothing_var, width=5)
        ttk.Button(first, text="Scan", command=self.scan).pack(side="left")

        second = ttk.Frame(self)
        second.pack(fill="x", pady=(2, 4))
        self.check_field(second, "Include subfolders", self.recursive_var)
        self.check_field(second, "Force re-convert", self.force_var)
        self.check_field(second, "Copy JPEGs that already fit", self.passthrough_var)
        self.check_field(second, "Remove all metadata", self.strip_var)
        ttk.Label(second, textvariable=self.status_var).pack(side="right")

        ttk.Label(
            self,
            text=(
                f"Everything is converted to sRGB. {formats.describe_support()}. "
                "EXIF is kept unless 'Remove all metadata' is ticked."
            ),
            foreground="grey40",
        ).pack(fill="x")
        self.build_destination_label()

    def current_settings(self) -> ConvertSettings:
        """Read the inline fields into a validated settings object."""
        settings = dataclasses.replace(self.app.convert_settings)
        try:
            settings.max_long_edge = int(self.long_edge_var.get())
            settings.quality = int(self.quality_var.get())
            settings.max_size = int(self.max_size_var.get()) * 1024
            settings.smoothing = int(self.smoothing_var.get())
        except ValueError as exc:
            raise scanner.ScanError(
                "Long edge, quality, max size and smoothing must be whole numbers."
            ) from exc
        settings.strip_metadata = self.strip_var.get()
        settings.recursive = self.recursive_var.get()
        settings.force = self.force_var.get()
        settings.passthrough = self.passthrough_var.get()
        settings.input_folder = self.input_var.get().strip()
        settings.output_parent = self.output_var.get().strip()
        try:
            settings.validate()
        except ValueError as exc:
            raise scanner.ScanError(str(exc)) from exc
        return settings

    def scan_state(self) -> tuple:
        # The worker encodes with app.convert_settings, which only perform_scan
        # refreshes — so every field here, not just the two that change the job
        # list, has to force a re-scan before it can take effect.
        return super().scan_state() + (
            self.long_edge_var.get(),
            self.quality_var.get(),
            self.max_size_var.get(),
            self.smoothing_var.get(),
            self.strip_var.get(),
            self.recursive_var.get(),
            self.force_var.get(),
            self.passthrough_var.get(),
        )

    def perform_scan(self) -> scanner.ScanResult:
        settings = self.current_settings()
        folder = self.input_folder()
        if folder is None:
            raise scanner.ScanError("Pick an input folder first.")
        result = scanner.scan_compress(folder, self.run_plan.path, settings)
        self.app.convert_settings = settings
        save_settings(convert=settings)
        return result

    def run_one(self, job: scanner.Job):
        return convert.convert(job.source, job.output, self.app.convert_settings)

    def redo_task(self, job: scanner.Job, long_edge, quality):
        # current_settings() reads the entry widgets, so it has to happen on the
        # UI thread and be closed over rather than called from the worker.
        settings = self.current_settings()
        return lambda: convert.convert(
            job.source, job.output, settings, long_edge, quality
        )

    def describe_result(self, result) -> tuple[str, str]:
        message = (
            f"{result.source.name} -> {result.output_size[0]}x{result.output_size[1]} "
            f"{result.kilobytes:.1f} KB"
        )
        if result.copied:
            message += " (copied unchanged)"
        else:
            message += f" at quality {result.quality}"
            if result.metadata_kept:
                message += ", EXIF kept"
        if result.notes:
            message += f" — {result.notes}"
        return result.status, message


class MinJpgApp(Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"minjpg {__version__}")
        self.geometry("1220x820")
        self.minsize(960, 640)

        self.log_file = setup_logging()
        get_logger().info("minjpg %s starting", __version__)

        self.settings, reset = load_settings()
        self.convert_settings, convert_reset = load_convert_settings()

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=8, pady=8)

        self.thumbnail_tab = ThumbnailTab(notebook, self)
        notebook.add(self.thumbnail_tab, text="Thumbnails (_min.jpg, \u226470 KB)")

        self.compress_tab = CompressTab(notebook, self)
        notebook.add(self.compress_tab, text="Compress images (\u22643840 px, \u2264600 KB)")

        settings_frame = ttk.Frame(notebook, padding=12)
        notebook.add(settings_frame, text="Settings")
        self._build_settings_tab(settings_frame)

        self._check_encoder()
        self._report_reset(self.thumbnail_tab, "Thumbnail", reset)
        self._report_reset(self.compress_tab, "Compress", convert_reset)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _report_reset(self, tab: BatchTab, label: str, reason: str | None) -> None:
        """Say so when a settings section could not be read.

        Silently reverting to defaults leaves the user wondering why their tuned
        values evaporated.
        """
        if not reason:
            return
        message = f"{label} settings were reset to defaults: {reason}"
        get_logger().warning(message)
        tab.append_log(message)

    def report_callback_exception(self, exc_type, exc_value, exc_tb) -> None:
        """Tk swallows callback exceptions to stderr, which the built app lacks."""
        get_logger().error(
            "unhandled exception in the interface",
            exc_info=(exc_type, exc_value, exc_tb),
        )
        messagebox.showerror(
            "Something went wrong",
            f"{exc_type.__name__}: {exc_value}\n\n{describe_log()}",
        )

    # ------------------------------------------------------------ settings

    def _build_settings_tab(self, parent: ttk.Frame) -> None:
        ttk.Label(
            parent,
            text="These settings drive the Thumbnails tab. "
                 "The Compress images tab has its own settings on its own tab.",
            foreground="grey40",
        ).grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))

        self.setting_vars: dict[str, StringVar] = {}
        rows = [
            ("max_long_edge", "Max long edge (px)", "Output never exceeds this on its longest side."),
            ("max_short_edge", "Max short edge (px)", "0 disables the short-edge cap."),
            ("size_target", "Size target (bytes)", "The quality search aims at or below this."),
            ("size_hard_cap", "Hard cap (bytes)", "Never exceeded; 71680 = 70 KiB."),
            ("quality_floor", "Quality floor", "Lowest quality the search will accept."),
            ("quality_ceiling", "Quality ceiling", "Squoosh's default is 75."),
            ("smoothing", "Smoothing", "MozJPEG -smooth; your Squoosh setting was 30."),
            ("min_long_edge", "Minimum long edge (px)", "Floor for the shrink fallback."),
            ("max_shrink_rounds", "Max shrink rounds", "Retries when the quality floor still busts the cap."),
        ]
        for index, (name, label, hint) in enumerate(rows, start=1):
            var = StringVar(value=str(getattr(self.settings, name)))
            self.setting_vars[name] = var
            ttk.Label(parent, text=label).grid(row=index, column=0, sticky="w", pady=2)
            ttk.Entry(parent, textvariable=var, width=12).grid(
                row=index, column=1, sticky="w", padx=(8, 12)
            )
            ttk.Label(parent, text=hint, foreground="grey40").grid(
                row=index, column=2, sticky="w"
            )

        row = len(rows) + 1
        self.linear_var = BooleanVar(value=self.settings.linear_light_resize)
        ttk.Checkbutton(
            parent,
            text="Resize in linear light (matches Squoosh's lanczos3 + linearRGB)",
            variable=self.linear_var,
        ).grid(row=row, column=0, columnspan=3, sticky="w", pady=(10, 2))

        layout = ttk.LabelFrame(parent, text="Where the thumbnails go", padding=8)
        layout.grid(row=row + 1, column=0, columnspan=3, sticky="ew", pady=(12, 0))
        self.layout_var = StringVar(value=self.settings.min_layout)
        ttk.Radiobutton(
            layout,
            text=f"In their own {MIN_SUBDIR}/ folder, mirroring the input structure",
            value=LAYOUT_SUBFOLDER,
            variable=self.layout_var,
        ).pack(anchor="w")
        ttk.Label(
            layout,
            text=f"    <new folder>/{MIN_SUBDIR}/a_min.jpg,  "
                 f"<new folder>/{MIN_SUBDIR}/sub/c_min.jpg\n"
                 "    Only the thumbnails are written. Nothing else is copied.",
            foreground="grey40",
        ).pack(anchor="w", pady=(0, 8))
        ttk.Radiobutton(
            layout,
            text="Next to their originals, with the whole input folder copied across",
            value=LAYOUT_BESIDE,
            variable=self.layout_var,
        ).pack(anchor="w")
        ttk.Label(
            layout,
            text="    <new folder>/a.jpg + a_min.jpg,  <new folder>/sub/c.jpg + c_min.jpg\n"
                 "    Every file in the input tree is copied, images and non-images alike,\n"
                 "    so the new folder stands on its own — and uses about as much disk\n"
                 "    space again as the input folder.",
            foreground="grey40",
        ).pack(anchor="w")

        buttons = ttk.Frame(parent)
        buttons.grid(row=row + 2, column=0, columnspan=3, sticky="w", pady=(12, 0))
        ttk.Button(buttons, text="Apply & save", command=self._apply_settings).pack(side="left")
        ttk.Button(buttons, text="Reset to defaults", command=self._reset_settings).pack(
            side="left", padx=(8, 0)
        )

        self.encoder_label = ttk.Label(parent, text="", foreground="grey40")
        self.encoder_label.grid(row=row + 3, column=0, columnspan=3, sticky="w", pady=(16, 0))

    def _apply_settings(self) -> None:
        # The worker reads app.settings per job, so changing them mid-batch would
        # split one run across two configurations.
        if any(tab.busy() for tab in (self.thumbnail_tab, self.compress_tab)):
            messagebox.showinfo(
                "Still running",
                "Settings cannot change while a batch is running.\n"
                "Wait for it to finish, or press Cancel first.",
            )
            return

        candidate = dataclasses.replace(self.settings)
        try:
            for name, var in self.setting_vars.items():
                setattr(candidate, name, int(var.get()))
            candidate.linear_light_resize = self.linear_var.get()
            candidate.min_layout = self.layout_var.get()
            candidate.validate()
        except ValueError as exc:
            messagebox.showerror("Invalid settings", str(exc))
            return

        self.settings = candidate
        self.thumbnail_tab.settings = candidate
        save_settings(settings=self.settings)
        self.thumbnail_tab.append_log("Settings saved.")
        # The layout changes the shape of the run folder, so the Thumbnails tab's
        # hint is now describing the wrong thing until it is refreshed.
        self.thumbnail_tab.update_destination()

    def _reset_settings(self) -> None:
        defaults = Settings(last_folder=self.settings.last_folder)
        for name, var in self.setting_vars.items():
            var.set(str(getattr(defaults, name)))
        self.linear_var.set(defaults.linear_light_resize)
        self.layout_var.set(defaults.min_layout)
        self._apply_settings()

    # --------------------------------------------------------------- misc

    def _check_encoder(self) -> None:
        try:
            version = encoder.cjpeg_version()
        except Exception as exc:
            get_logger().error("encoder unavailable: %s", exc)
            self.encoder_label.configure(text=f"Encoder unavailable: {exc}", foreground="red")
            messagebox.showerror("MozJPEG unavailable", str(exc))
            for tab in (self.thumbnail_tab, self.compress_tab):
                tab.start_button.configure(state="disabled")
            return

        self.encoder_label.configure(text=f"Encoder: {version} ({encoder.cjpeg_path()})")
        for tab in (self.thumbnail_tab, self.compress_tab):
            tab.append_log(f"Encoder: {version}")
            tab.append_log(formats.describe_support())

    def _on_close(self) -> None:
        busy = [tab for tab in (self.thumbnail_tab, self.compress_tab) if tab.busy()]
        if busy:
            if not messagebox.askokcancel("Quit", "Work is still running. Stop and quit?"):
                return
            for tab in busy:
                # A tab can be busy on a single-image re-do instead, which has
                # no batch to cancel — it is one atomic write and will be gone
                # with the process.
                if tab.worker:
                    tab.worker.cancelled.set()
        self.settings.last_folder = self.thumbnail_tab.input_var.get().strip()
        self.settings.output_parent = self.thumbnail_tab.output_var.get().strip()
        try:
            self.convert_settings = self.compress_tab.current_settings()
        except scanner.ScanError:
            pass  # unparseable field on the way out; keep what was last valid
        save_settings(settings=self.settings, convert=self.convert_settings)
        self.destroy()


def main() -> int:
    try:
        MinJpgApp().mainloop()
    except Exception:
        # setup_logging() may not have run yet if the failure was early enough.
        setup_logging()
        get_logger().exception("minjpg exited with an unhandled exception")
        traceback.print_exc()
        try:
            messagebox.showerror("minjpg could not start", describe_log())
        except Exception:
            pass  # no display, or Tk itself is what failed
        return 1
    return 0
