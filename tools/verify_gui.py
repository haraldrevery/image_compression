#!/usr/bin/env python3
"""Drive the real app through the input/output rules that protect your files.

``verify_convert.py`` tests the scanner and the run-folder logic directly.  This
drives the actual Tkinter app instead — building the tabs, scanning, starting
batches and inspecting what landed on disk — because the guarantee the user cares
about ("this cannot touch my originals") is a property of the whole app, not of
any one function.  Needs a display, and sample photos from ``--data`` or
``--synthetic``; without either it skips itself and exits with status 2, so a
skipped run can never pass for a successful one.

Usage::

    python tools/verify_gui.py [--data DIR | --synthetic]

Exit status: 0 all checks passed, 1 a check failed, 2 skipped (nothing verified).
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
import threading
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DEFAULT_DATA = Path(__file__).resolve().parents[2] / "example_data"

failures: list[str] = []
checks = 0


def check(condition: bool, description: str) -> bool:
    global checks
    checks += 1
    if not condition:
        failures.append(description)
        print(f"  FAIL  {description}")
        return False
    print(f"  ok    {description}")
    return True


def section(title: str) -> None:
    print(f"\n=== {title}")


def snapshot(root: Path) -> dict[Path, tuple[int, int]]:
    return {
        p: (p.stat().st_size, p.stat().st_mtime_ns)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def build_input(root: Path, data: Path) -> None:
    """A small tree of real photos plus the non-image files that must survive."""
    from minjpg.scanner import is_min_file

    (root / "sub").mkdir(parents=True)
    (root / "empty").mkdir()
    sources = [p for p in sorted(data.glob("*.jpg")) if not is_min_file(p)]
    if len(sources) < 4:
        raise SystemExit(f"need at least 4 non-_min JPEGs in {data}")
    for photo in sources[:3]:
        shutil.copyfile(photo, root / photo.name)
    shutil.copyfile(sources[3], root / "sub" / sources[3].name)
    (root / "notes.txt").write_text("keep me")
    (root / "sub" / "clip.bin").write_bytes(b"\x00\xff" * 1000)


class Driver:
    """The app, plus the small amount of plumbing needed to drive it headlessly."""

    #: Run folder names carry the minute, so a test that crosses a minute
    #: boundary would stop colliding and quietly skip the suffix check.  Pinning
    #: the clock makes every run in this file contend for the same name, which
    #: is exactly the behaviour under test.
    WHEN = datetime(2026, 8, 27, 14, 32)

    def __init__(self, tmp: Path, data: Path):
        import minjpg.config as config
        from minjpg import runfolder

        # Never touch the real settings file while testing.
        config.config_path = lambda: tmp / "settings.json"

        # No dialog may ever block a headless run.  Tests that care about one
        # install their own stub; everything else is recorded here.
        import tkinter.messagebox as mb

        self.dialogs: list[tuple[str, str, str]] = []
        for kind in ("showinfo", "showwarning", "showerror"):
            setattr(mb, kind, lambda title="", message="", _kind=kind, **_: (
                self.dialogs.append((_kind, str(title), str(message)))
            ))
        mb.askokcancel = lambda *a, **k: True
        # Yes/no questions (create a folder, reset settings) answer "no" unless
        # a test says otherwise, and are recorded like the rest.
        self.questions: list[str] = []
        mb.askyesno = lambda title="", message="", **_: self.questions.append(str(title)) or False
        real_base_name = runfolder.base_name
        runfolder.base_name = lambda folder, kind, when=None: real_base_name(
            folder, kind, when or self.WHEN
        )

        from minjpg.app import MinJpgApp

        self.tmp = tmp
        self.input = tmp / "photos"
        self.output = tmp / "output"
        build_input(self.input, data)
        self.output.mkdir()

        self.app = MinJpgApp()
        self.app.withdraw()

    def pump(self, seconds: float = 0.3) -> None:
        end = time.time() + seconds
        while time.time() < end:
            self.app.update()
            time.sleep(0.01)

    def run(self, tab, label: str):
        """Scan, confirm and run a batch the way a user would."""
        tab.scan()
        self.pump()
        plan = tab.run_plan
        print(f"  [{label}] will create {plan.path.name}  ({tab.status_var.get()})")
        tab.confirm_start = lambda: True  # stands in for the user pressing OK
        tab.start()
        self.pump()
        while tab.busy():
            self.pump(0.2)
        self.pump(0.3)
        return plan

    def close(self) -> None:
        self.app.destroy()


def test_folders_required(driver: Driver) -> None:
    section("Neither tab acts without both folders")
    import tkinter

    tkinter.messagebox.showinfo = lambda *a, **k: None
    for tab, name in ((driver.app.thumbnail_tab, "Thumbnails"),
                      (driver.app.compress_tab, "Compress images")):
        tab.input_var.set("")
        tab.output_var.set("")
        check(not tab.ensure_ready(), f"{name}: refuses with both folders blank")
        tab.input_var.set(str(driver.input))
        check(not tab.ensure_ready(), f"{name}: refuses with only the input set")
        driver.pump(0.5)  # the hint refreshes once typing pauses
        print(f"        hint: {tab.destination_var.get()}")
        tab.input_var.set("")


def test_thumbnail_layouts(driver: Driver) -> None:
    from minjpg.config import LAYOUT_BESIDE, LAYOUT_SUBFOLDER, MIN_SUBDIR

    tab = driver.app.thumbnail_tab
    tab.input_var.set(str(driver.input))
    tab.output_var.set(str(driver.output))
    before = snapshot(driver.input)

    section(f"Thumbnails, {MIN_SUBDIR}/ layout")
    driver.app.layout_var.set(LAYOUT_SUBFOLDER)  # the Settings tab's radio button
    first = driver.run(tab, "min-subfolder")
    produced = sorted(str(p.relative_to(first.path)) for p in first.path.rglob("*") if p.is_file())
    print(f"        {produced}")
    check(bool(produced) and all(p.startswith(f"{MIN_SUBDIR}/") for p in produced),
          f"everything sits under {MIN_SUBDIR}/")
    check(any(p.startswith(f"{MIN_SUBDIR}/sub/") for p in produced),
          "subfolders are mirrored")

    section("A second run in the same minute steps aside")
    second = driver.run(tab, "min-subfolder-2")
    check(second.collided and second.path.name == f"{first.path.name}_2",
          f"the second run stepped aside to {second.path.name}")
    check(any(first.path.rglob("*")), "the first run's folder is untouched")

    section("Thumbnails, next-to-originals layout")
    driver.app.layout_var.set(LAYOUT_BESIDE)  # the Settings tab's radio button
    third = driver.run(tab, "min-beside")
    produced = sorted(str(p.relative_to(third.path)) for p in third.path.rglob("*") if p.is_file())
    print(f"        {produced}")
    check("notes.txt" in produced and "sub/clip.bin" in produced,
          "non-image files are copied across")
    check(any(p.endswith("_min.jpg") for p in produced), "thumbnails sit beside the originals")
    check((third.path / "empty").is_dir(), "the empty subfolder is mirrored")
    check((third.path / "notes.txt").read_text() == "keep me", "notes.txt arrived intact")
    check((third.path / "sub" / "clip.bin").read_bytes()
          == (driver.input / "sub" / "clip.bin").read_bytes(),
          "clip.bin is byte-identical to its source")

    check(snapshot(driver.input) == before,
          "the input tree is unchanged after three thumbnail runs")
    return [first, second, third]


def test_compress_tab(driver: Driver) -> list:
    section("Compress images tab: a full mirror")
    tab = driver.app.compress_tab
    tab.input_var.set(str(driver.input))
    tab.output_var.set(str(driver.output))
    tab.max_size_var.set("300")
    before = snapshot(driver.input)

    plan = driver.run(tab, "compress")
    produced = sorted(str(p.relative_to(plan.path)) for p in plan.path.rglob("*") if p.is_file())
    print(f"        {produced}")
    check("notes.txt" in produced and "sub/clip.bin" in produced,
          "non-image files are carried across")
    check("_compressed_" in plan.path.name, f"the run folder names the job: {plan.path.name}")
    check(all(not p.endswith("_min.jpg") for p in produced),
          "no thumbnails are produced on this tab")
    check(snapshot(driver.input) == before, "the input tree is unchanged")
    return [plan]


def test_dialog_text(driver: Driver) -> None:
    section("What Start tells the user before creating anything")
    import tkinter

    captured: dict[str, str] = {}
    tkinter.messagebox.askokcancel = lambda title, msg, **k: (
        captured.update(title=title, msg=msg), True
    )[1]

    tab = driver.app.thumbnail_tab
    tab.scan()
    driver.pump()
    original_confirm = type(tab).confirm_start
    ok = original_confirm(tab)
    for line in captured["msg"].splitlines():
        print(f"        | {line}")
    check(ok, "the stubbed dialog returned OK")
    check("will be created" in captured["msg"], "it names the folder about to be created")
    check(str(driver.input) in captured["msg"], "it says the input folder is not modified")
    check("already there" in captured["msg"],
          "it warns that the generated name was already taken")


def test_stale_scan(driver: Driver) -> None:
    section("Editing a folder after Scan blocks Start")
    import tkinter

    infos: list[str] = []
    tkinter.messagebox.showinfo = lambda title, msg, **k: infos.append(title)

    tab = driver.app.thumbnail_tab
    tab.input_var.set(str(driver.input))
    tab.output_var.set(str(driver.output))
    tab.scan()
    driver.pump()
    plan = tab.run_plan

    elsewhere = driver.tmp / "somewhere-else"
    elsewhere.mkdir(exist_ok=True)
    tab.input_var.set(str(elsewhere))  # the user edits the field but does not re-scan
    tab.start()
    driver.pump(0.4)
    check("Scan again first" in infos, f"Start refused (dialogs seen: {infos})")
    check(not plan.path.exists(), "nothing was created for the stale scan")
    check(not tab.busy(), "no worker was started")


def test_stale_settings(driver: Driver) -> None:
    section("Changing an option after Scan blocks Start")
    import tkinter
    from minjpg.config import LAYOUT_BESIDE, LAYOUT_SUBFOLDER

    infos: list[str] = []
    tkinter.messagebox.showinfo = lambda title, msg, **k: infos.append(title)

    thumbnails, compress = driver.app.thumbnail_tab, driver.app.compress_tab
    for tab in (thumbnails, compress):
        tab.input_var.set(str(driver.input))
        tab.output_var.set(str(driver.output))
    # Every control below is flipped; later tests must not inherit that.  (They
    # did: a Compress run left without subfolders passed for complete.)
    controls = [
        thumbnails.recursive_var, thumbnails.jpeg_only_var,
        compress.quality_var, compress.long_edge_var, compress.max_size_var,
        compress.smoothing_var, compress.strip_var, compress.passthrough_var,
        compress.recursive_var,
    ]
    saved = [var.get() for var in controls]
    saved_layout = driver.app.layout_var.get()

    # Each of these decides either which files are processed or how, and the
    # worker reads them from the settings object the scan filled in — so a
    # change after the scan has to force another one rather than be ignored.
    cases = [
        (thumbnails, "Include subfolders",
         lambda: thumbnails.recursive_var.set(not thumbnails.recursive_var.get())),
        (thumbnails, "JPEG sources only",
         lambda: thumbnails.jpeg_only_var.set(not thumbnails.jpeg_only_var.get())),
        (thumbnails, "thumbnail layout (Settings tab)",
         lambda: driver.app.layout_var.set(
             LAYOUT_BESIDE if driver.app.layout_var.get() == LAYOUT_SUBFOLDER
             else LAYOUT_SUBFOLDER
         )),
        (compress, "Quality", lambda: compress.quality_var.set("55")),
        (compress, "Max long edge", lambda: compress.long_edge_var.set("2000")),
        (compress, "Max size", lambda: compress.max_size_var.set("250")),
        (compress, "Smoothing", lambda: compress.smoothing_var.set("10")),
        (compress, "Remove all metadata",
         lambda: compress.strip_var.set(not compress.strip_var.get())),
        (compress, "Copy JPEGs that already fit",
         lambda: compress.passthrough_var.set(not compress.passthrough_var.get())),
        (compress, "Include subfolders",
         lambda: compress.recursive_var.set(not compress.recursive_var.get())),
    ]
    for tab, label, change in cases:
        tab.scan()
        driver.pump()
        check(not tab.scan_is_stale(), f"a fresh scan is not stale ({label})")
        change()
        check(tab.scan_is_stale(), f"changing '{label}' needs another scan")

    # …and Start actually refuses, rather than running the list it already had.
    thumbnails.scan()
    driver.pump()
    plan = thumbnails.run_plan
    thumbnails.recursive_var.set(not thumbnails.recursive_var.get())
    infos.clear()
    thumbnails.start()
    driver.pump(0.4)
    check("Scan again first" in infos, f"Start refused (dialogs seen: {infos})")
    check(not plan.path.exists(), "nothing was created for the stale scan")
    check(not thumbnails.busy(), "no worker was started")

    for var, value in zip(controls, saved):
        var.set(value)
    driver.app.layout_var.set(saved_layout)


def test_redo_needs_a_run_folder(driver: Driver) -> None:
    section("Re-do before Start cannot conjure the run folder into being")
    import tkinter

    infos: list[str] = []
    tkinter.messagebox.showinfo = lambda title, msg, **k: infos.append(title)

    tab = driver.app.compress_tab
    tab.input_var.set(str(driver.input))
    tab.output_var.set(str(driver.output))
    tab.scan()
    driver.pump()
    planned = tab.run_plan.path
    check(not planned.exists(), "the planned folder does not exist before Start")

    rows = tab.tree.get_children()
    tab.tree.selection_set(rows[0])
    tab.override_quality_var.set("50")
    infos.clear()
    tab.reencode_selected()
    driver.pump()
    tab.override_quality_var.set("")
    check("Run the batch first" in infos, f"the re-do was refused (dialogs: {infos})")
    check(not planned.exists(), f"nothing created {planned.name} behind the user's back")

    # The name is still free, so Start uses it rather than stepping aside and
    # stranding whatever the re-do had written.
    plan = driver.run(tab, "after-refused-redo")
    check(plan.path == planned,
          f"Start used the folder it planned ({plan.path.name} vs {planned.name})")

    # A re-do after the run is the supported path and must still work.  The run
    # re-scanned, so the tree has been rebuilt and the old row ids are gone.
    # It has to be an encoded row: a copied file has nothing to re-encode.
    from minjpg.scanner import PROCESS

    row = next(r for r in tab.tree.get_children() if tab.rows[r].action == PROCESS)
    tab.tree.selection_set(row)
    target = tab.rows[row].output
    before = target.stat().st_size if target.is_file() else None
    tab.override_quality_var.set("35")
    infos.clear()
    tab.reencode_selected()
    check(tab.redo_worker is not None, "the re-do went to a worker, not the UI thread")
    while tab.busy():
        driver.pump(0.05)
    driver.pump(0.3)
    tab.override_quality_var.set("")
    after = target.stat().st_size if target.is_file() else None
    check(not infos and after is not None and after != before,
          f"a re-do after Start still works ({before} -> {after} bytes, dialogs: {infos})")

    # Proof it is really off the UI thread rather than merely wrapped in one:
    # hold the task open and check the event loop still turns.  Decoding a
    # full-size photo takes seconds, and on the UI thread the window would be
    # frozen for every one of them.
    entered, release = threading.Event(), threading.Event()
    real_task = tab.redo_task

    def slow_task(job, long_edge, quality):
        inner = real_task(job, long_edge, quality)

        def run():
            entered.set()
            release.wait(10)
            return inner()

        return run

    tab.redo_task = slow_task
    tab.tree.selection_set(row)
    tab.reencode_selected()
    check(entered.wait(10), "the held re-do actually started")
    spins = 0
    while spins < 5:
        driver.app.update()  # would never return if the work were on this thread
        spins += 1
    check(spins == 5, "the event loop keeps turning while a re-do is working")
    check(tab.busy(), "the tab reports itself busy during a re-do")
    check(str(tab.redo_button["state"]) == "disabled",
          "the Re-do button is disabled while one is in flight")
    tab.start()  # must be refused: a batch would race the re-do's own output
    check(not (tab.worker and tab.worker.is_alive()),
          "Start is refused while a re-do is in flight")
    release.set()
    while tab.busy():
        driver.pump(0.05)
    driver.pump(0.3)
    tab.redo_task = real_task
    check(str(tab.redo_button["state"]) == "normal", "the Re-do button comes back")
    return [plan]


def test_reporting_and_markers(driver: Driver) -> list:
    section("Every row reports its own result; incomplete runs say so")
    import errno

    from minjpg import runfolder
    from minjpg.config import LAYOUT_BESIDE
    from minjpg.scanner import COPY, PROCESS

    tab = driver.app.thumbnail_tab
    tab.input_var.set(str(driver.input))
    tab.output_var.set(str(driver.output))
    driver.app.layout_var.set(LAYOUT_BESIDE)  # the Settings tab's radio button
    real_run = tab.run_one
    plans = []

    def rows(action=None):
        return [r for r in tab.tree.get_children()
                if action is None or tab.rows[r].action == action]

    def run_with(run_one, label):
        tab.run_one = run_one
        driver.dialogs.clear()
        try:
            plan = driver.run(tab, label)
        finally:
            tab.run_one = real_run
        plans.append(plan)
        return plan, plan.path / runfolder.MARKER_NAME

    # 1. In the "beside" layout every image has two rows, its copy and its
    #    thumbnail.  They share a source path, and used to share one row.
    plan, marker = run_with(real_run, "beside-statuses")
    stuck = [tab.tree.item(r, "text") for r in rows()
             if tab.tree.set(r, "status") in ("queued", "working")]
    check(not stuck, f"no row is left queued once the run is over (stuck: {stuck})")
    check(all(tab.tree.set(r, "status") == "copied" for r in rows(COPY)),
          "copy rows report the copy, not the thumbnail")
    check(all(tab.tree.set(r, "status") in ("done", "shrunk", "at floor") for r in rows(PROCESS)),
          "thumbnail rows report the thumbnail")
    check(not marker.exists(), "a complete run removes its incomplete marker")

    # 2. One thumbnail fails: the marker names it, the user is told, and a
    #    successful re-do of that row makes the folder complete again.
    victim = sorted(driver.input.glob("*.jpg"))[0]

    def flaky(job):
        if job.source == victim:
            raise RuntimeError("simulated encoder crash")
        return real_run(job)

    plan, marker = run_with(flaky, "one-failure")
    check(marker.is_file() and victim.name in marker.read_text(encoding="utf-8"),
          "the marker names the file that failed")
    check(any(kind == "showerror" for kind, _t, _m in driver.dialogs),
          "the user is told the run had problems")
    row = next(r for r in rows(PROCESS) if tab.rows[r].source == victim)
    check(tab.tree.set(row, "status") == "failed", "the failed row says so")
    tab.tree.selection_set(row)
    tab.override_long_var.set("")
    tab.override_quality_var.set("")
    tab.reencode_selected()
    while tab.busy():
        driver.pump(0.05)
    driver.pump(0.3)
    check(tab.tree.set(row, "status") != "failed", "the re-done row reports success")
    check(not marker.exists(), "fixing the only failure removes the marker")

    # 3. A full disk stops the batch instead of failing every remaining file.
    calls = []

    def disk_full(job):
        calls.append(job)
        if len(calls) >= 2:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_run(job)

    plan, marker = run_with(disk_full, "disk-full")
    check(len(calls) == 2, f"the batch stopped at the first full-disk error ({len(calls)} tries)")
    check(marker.is_file() and "stopped" in marker.read_text(encoding="utf-8"),
          "the marker says the run stopped")
    check(tab.progress_var.get() == "stopped",
          f"the progress line says stopped ({tab.progress_var.get()!r})")
    check(any(kind == "showerror" and "stopped early" in msg for kind, _t, msg in driver.dialogs),
          "the user is told the disk is full")
    check(str(tab.start_button["state"]) == "normal", "Start is usable again afterwards")

    # 4. Cancel leaves the marker, because the folder really is incomplete.
    def cancel_now(job):
        tab.worker.cancelled.set()
        return real_run(job)

    plan, marker = run_with(cancel_now, "cancelled")
    check(marker.is_file() and "cancelled" in marker.read_text(encoding="utf-8"),
          "a cancelled run stays marked incomplete")
    check(not driver.dialogs, f"cancelling is not reported as a problem ({driver.dialogs})")

    # 5. Compress tab: an image that cannot be decoded is carried across
    #    unchanged instead of silently missing from the "full mirror".
    ctab = driver.app.compress_tab
    ctab.input_var.set(str(driver.input))
    ctab.output_var.set(str(driver.output))
    broken = driver.input / "broken.png"
    payload = b"\x89PNG\r\n\x1a\n" + b"junk" * 64
    broken.write_bytes(payload)
    driver.dialogs.clear()
    try:
        plan = driver.run(ctab, "kept-original")
    finally:
        broken.unlink()
    plans.append(plan)
    kept = plan.path / "broken.png"
    check(kept.is_file() and kept.read_bytes() == payload,
          "the undecodable image was copied across unchanged")
    row = next(r for r in ctab.tree.get_children() if ctab.rows[r].source.name == "broken.png")
    check(ctab.tree.set(row, "status") == "kept original",
          f"its row says so ({ctab.tree.set(row, 'status')!r})")
    marker_path = plan.path / runfolder.MARKER_NAME
    check(not marker_path.exists(),
          "nothing is missing, so there is no incomplete marker"
          + (f" - it says: {marker_path.read_text(encoding='utf-8')[-300:]!r}"
             if marker_path.exists() else ""))
    check(any(kind == "showwarning" for kind, _t, _m in driver.dialogs),
          "the user is told an original was kept")
    return plans


def test_pump_survives(driver: Driver) -> None:
    section("One bad event cannot freeze a tab")
    import types

    tab = driver.app.compress_tab
    fake_root = driver.tmp / "fake-run"
    fake_root.mkdir()
    fake = types.SimpleNamespace(
        stop_reason=None, processed=0, total=0, done=0, kept=0, failed_rows={},
        missing=[], not_processed=[], rows=[],
        run_root=fake_root, source_root=driver.input, is_alive=lambda: False,
    )
    tab.worker = fake
    tab.start_button.configure(state="disabled")
    tab.events.put(("done",))  # malformed: raises inside the handler
    tab.events.put(("finished", fake))
    driver.pump(0.5)
    check(tab.events.empty(), "the events after the bad one were still handled")
    check(str(tab.start_button["state"]) == "normal", "Start came back once the run ended")
    tab.worker = None


def test_preview_and_hint(driver: Driver) -> None:
    section("The folder hint and the preview stay responsive and truthful")
    from PIL import Image

    tab = driver.app.compress_tab
    tab.input_var.set(str(driver.input))
    tab.output_var.set(str(driver.output))
    driver.pump(0.5)
    hint = tab.destination_var.get()
    check(hint.startswith("Will create:"), f"the hint names the folder once typing pauses ({hint!r})")

    # A photo stored sideways with an EXIF rotation previews upright, as its
    # result is written - not on its side next to an upright result.
    source = sorted(driver.input.glob("*.jpg"))[0]
    rotated = driver.tmp / "rotated-preview.jpg"
    with Image.open(source) as image:
        width, height = image.size
        exif = Image.Exif()
        exif[0x0112] = 6  # rotate 90 degrees clockwise to display
        image.save(rotated, exif=exif, quality=85)
    started = time.perf_counter()
    tab._show(tab.before_label, tab.before_info, rotated, (400, 400))
    elapsed = time.perf_counter() - started
    photo = tab.before_label.image
    info = str(tab.before_info.cget("text"))
    print(f"        {width}x{height} stored -> preview {photo.width()}x{photo.height()}, "
          f"{info!r}, {elapsed * 1000:.0f} ms")
    check((photo.width() > photo.height()) == (height > width),
          "an EXIF-rotated photo previews upright")
    check(info.startswith(f"{height}x{width}"), f"the preview reports the upright size ({info!r})")


def test_empty_run_cleanup(driver: Driver) -> None:
    section("A run that writes nothing leaves nothing behind")
    import minjpg.run as run_module
    from minjpg.config import LAYOUT_BESIDE

    tab = driver.app.thumbnail_tab
    tab.input_var.set(str(driver.input))
    tab.output_var.set(str(driver.output))
    driver.app.layout_var.set(LAYOUT_BESIDE)  # the Settings tab's radio button
    tab.confirm_start = lambda: True

    def boom(job):
        raise OSError("simulated disk failure")

    tab.scan()
    driver.pump()
    plan = tab.run_plan
    saved_copy, saved_run = run_module.run_copy, tab.run_one
    run_module.run_copy, tab.run_one = boom, boom
    try:
        tab.start()
        driver.pump()
        while tab.busy():
            driver.pump(0.2)
        driver.pump(0.4)
    finally:
        run_module.run_copy, tab.run_one = saved_copy, saved_run
    check(not plan.path.exists(), f"the empty run folder was removed ({plan.path.name})")

    kept = driver.run(tab, "successful")
    check(kept.path.is_dir() and any(kept.path.rglob("*")),
          "a run that did write is never cleaned up")


def test_settings_and_reporting(driver: Driver) -> None:
    section("Settings take effect at Scan; rows and dialogs say what happened")
    import tkinter.messagebox as mb

    from PIL import Image, PngImagePlugin

    from minjpg import runfolder
    from minjpg.config import LAYOUT_SUBFOLDER, Settings

    app = driver.app
    thumbs, compress = app.thumbnail_tab, app.compress_tab
    notebook = app.nametowidget(thumbs.winfo_parent())
    titles = [notebook.tab(i, "text") for i in range(len(notebook.tabs()))]
    check(not any(ch.isdigit() for title in titles for ch in title),
          f"tab titles quote no numbers that could go stale: {titles}")

    # Settings-tab fields count at the next Scan, with no Apply step to forget.
    thumbs.input_var.set(str(driver.input))
    thumbs.output_var.set(str(driver.output))
    app.layout_var.set(LAYOUT_SUBFOLDER)
    saved = {name: var.get() for name, var in app.setting_vars.items()}
    app.setting_vars["size_target"].set("30000")
    app.setting_vars["size_hard_cap"].set("32000")
    thumbs.scan()
    driver.pump()
    check(app.settings.size_hard_cap == 32000, "the scan saved what the Settings tab says")
    # Whatever happens to the live settings after the scan, the run it built
    # uses the copy the scan took: a 16 KB cap set now must not reach it.
    app.settings.size_target, app.settings.size_hard_cap = 15000, 16000
    plan = thumbs.run_plan
    thumbs.confirm_start = lambda: True
    thumbs.start()
    driver.pump()
    while thumbs.busy():
        driver.pump(0.2)
    driver.pump(0.3)
    sizes = [p.stat().st_size for p in plan.path.rglob("*_min.jpg")]
    largest = max(sizes, default=0)
    check(bool(sizes) and largest <= 32000,
          f"a hard cap typed on the Settings tab applies at the next scan (largest {largest})")
    check(largest > 16000,
          f"the run used the settings its scan took, not ones changed afterwards (largest {largest})")
    app.setting_vars["size_hard_cap"].set("33000")
    check(thumbs.scan_is_stale(), "editing a Settings-tab number after Scan needs another scan")
    app.setting_vars["size_hard_cap"].set("not a number")
    driver.dialogs.clear()
    thumbs.scan()
    check(any("Hard cap" in message for _k, _t, message in driver.dialogs),
          f"an invalid Settings-tab value refuses the scan and names the field ({driver.dialogs})")
    for name, value in saved.items():
        app.setting_vars[name].set(value)

    # Reset asks first, and changes nothing on "no".
    app.setting_vars["quality_floor"].set("33")
    driver.questions.clear()
    app._reset_settings()
    check(driver.questions and app.setting_vars["quality_floor"].get() == "33",
          "Reset to defaults asks first and leaves the fields alone on 'no'")
    real_ask = mb.askyesno
    mb.askyesno = lambda *a, **k: True
    try:
        app._reset_settings()
    finally:
        mb.askyesno = real_ask
    check(app.setting_vars["quality_floor"].get() == str(Settings().quality_floor),
          "Reset to defaults resets on 'yes'")
    for name, value in saved.items():
        app.setting_vars[name].set(value)

    # A missing output folder is offered, never created unasked.
    missing = driver.tmp / "not-mounted" / "out"
    compress.input_var.set(str(driver.input))
    compress.output_var.set(str(missing))
    driver.pump(0.5)
    check("does not exist yet" in compress.destination_var.get(),
          f"the hint says the output folder is missing ({compress.destination_var.get()!r})")
    driver.questions.clear()
    compress.scan()
    check(driver.questions and not missing.exists() and compress.run_plan is None,
          "declining leaves no folder behind and no scan")
    mb.askyesno = lambda *a, **k: True
    try:
        compress.scan()
    finally:
        mb.askyesno = real_ask
    check(missing.is_dir() and compress.scan_result is not None,
          "accepting creates the folder and the scan goes ahead")

    # Rows say where a renamed output went, and losses get a status of their own.
    odd = driver.tmp / "odd-input"
    odd.mkdir()
    photo = next(p for p in sorted(driver.input.glob("*.jpg")))
    with Image.open(photo) as image:
        small = image.convert("RGB").resize((300, 200))
    small.save(odd / "IMG_1.JPG", quality=90)
    small.save(odd / "IMG_1.BMP")
    info = PngImagePlugin.PngInfo()
    info.add_text("Raw profile type iptc", "\niptc\n       4\n74657374\n")
    small.save(odd / "tagged.png", pnginfo=info)
    small.convert("CMYK").save(odd / "print.jpg", quality=90)
    compress.input_var.set(str(odd))
    compress.output_var.set(str(driver.output))
    driver.dialogs.clear()
    driver.run(compress, "labels-and-losses")
    rows = {compress.tree.item(r, "text"): r for r in compress.tree.get_children()}
    print(f"        rows: {sorted(rows)}")
    check("IMG_1.BMP  →  IMG_1-2.jpg" in rows and "IMG_1.JPG" in rows,
          "a renamed output is shown on its row, and the real JPEG kept its name")
    lost_row = rows.get("tagged.png")
    check(lost_row is not None and compress.tree.set(lost_row, "status") == "metadata lost"
          and "attention" in compress.tree.item(lost_row, "tags"),
          "a file that lost metadata is flagged on its row")
    cmyk_row = rows.get("print.jpg")
    check(cmyk_row is not None and compress.tree.set(cmyk_row, "status") == "check colours",
          "a file whose colours may be off is flagged on its row")
    check(any("lost some of their metadata" in message and "tagged.png" in message
              for _k, _t, message in driver.dialogs),
          f"the end-of-run dialog names metadata losses ({driver.dialogs})")

    # The Compress tab's run keeps its scan's settings too.
    compress.scan()
    driver.pump()
    app.convert_settings.max_long_edge = 100  # a change the scan never saw
    plan = compress.run_plan
    compress.confirm_start = lambda: True
    compress.start()
    driver.pump()
    while compress.busy():
        driver.pump(0.2)
    driver.pump(0.3)
    edges = []
    for output in plan.path.glob("*.jpg"):
        with Image.open(output) as written:
            edges.append(max(written.size))
    check(bool(edges) and min(edges) > 100,
          f"the Compress run used the settings its scan took (long edges {sorted(edges)})")

    # In the "beside" layout an image's copy and thumbnail no longer share a label.
    from minjpg.config import LAYOUT_BESIDE
    app.layout_var.set(LAYOUT_BESIDE)
    thumbs.scan()
    labels = [thumbs.tree.item(r, "text") for r in thumbs.tree.get_children()]
    check(len(labels) == len(set(labels)), "every row in the 'beside' layout has its own label")
    app.layout_var.set(LAYOUT_SUBFOLDER)

    # Start during a re-do says why nothing happens.
    release = threading.Event()
    blocker = threading.Thread(target=release.wait, daemon=True)
    blocker.start()
    compress.redo_worker = blocker
    infos: list[str] = []
    real_info = mb.showinfo  # earlier tests replaced the driver's recorder with their own
    mb.showinfo = lambda title="", message="", **_: infos.append(str(title))
    try:
        compress.start()
    finally:
        mb.showinfo = real_info
        release.set()
        blocker.join()
        compress.redo_worker = None
    check("Still running" in infos,
          f"Start during a re-do explains itself instead of doing nothing ({infos})")
    check(not (driver.output / runfolder.MARKER_NAME).exists(), "nothing stray was written")


def test_nothing_stray(driver: Driver, plans: list) -> None:
    section("Nothing is written outside the run folders")
    expected = {plan.path.name for plan in plans}
    stray = sorted(p.name for p in driver.output.iterdir() if p.name not in expected)
    # Runs made by the later tests are legitimate too; only non-run entries matter.
    unexpected = [name for name in stray if "_min_" not in name and "_compressed_" not in name]
    check(not unexpected, f"the output folder holds only run folders (stray: {unexpected})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--data", type=Path, default=DEFAULT_DATA)
    source.add_argument("--synthetic", action="store_true",
                        help="use generated stand-in photos instead of a sample folder")
    args = parser.parse_args()

    if not args.synthetic and not args.data.is_dir():
        print(f"SKIPPED: no sample images at {args.data} (pass --data DIR or --synthetic)")
        return 2
    try:
        import tkinter

        probe = tkinter.Tk()
        probe.destroy()
    except Exception as exc:
        print(f"SKIPPED: no display available ({exc})")
        return 2

    tmp = Path(tempfile.mkdtemp(prefix="minjpg-gui-"))
    if args.synthetic:
        from samples import make_photos

        args.data = make_photos(tmp / "synthetic-samples")
    driver = Driver(tmp, args.data)
    try:
        test_folders_required(driver)
        plans = test_thumbnail_layouts(driver)
        plans += test_compress_tab(driver)
        test_dialog_text(driver)
        test_stale_scan(driver)
        test_stale_settings(driver)
        plans += test_redo_needs_a_run_folder(driver)
        plans += test_reporting_and_markers(driver)
        test_pump_survives(driver)
        test_preview_and_hint(driver)
        test_empty_run_cleanup(driver)
        test_settings_and_reporting(driver)
        test_nothing_stray(driver, plans)
    finally:
        driver.close()
        shutil.rmtree(tmp, ignore_errors=True)

    print(f"\n{checks} checks run")
    if failures:
        print(f"{len(failures)} FAILED:")
        for failure in failures:
            print(f"  - {failure}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
