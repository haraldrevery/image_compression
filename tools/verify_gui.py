#!/usr/bin/env python3
"""Drive the real app through the input/output rules that protect your files.

``verify_convert.py`` tests the scanner and the run-folder logic directly.  This
drives the actual Tkinter app instead — building the tabs, scanning, starting
batches and inspecting what landed on disk — because the guarantee the user cares
about ("this cannot touch my originals") is a property of the whole app, not of
any one function.  Needs a display; skips itself cleanly without one.

Usage::

    python tools/verify_gui.py [--data DIR]
"""

from __future__ import annotations

import argparse
import shutil
import sys
import tempfile
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
        print(f"        hint: {tab.destination_var.get()}")
        tab.input_var.set("")


def test_thumbnail_layouts(driver: Driver) -> None:
    from minjpg.config import LAYOUT_BESIDE, LAYOUT_SUBFOLDER, MIN_SUBDIR

    tab = driver.app.thumbnail_tab
    tab.input_var.set(str(driver.input))
    tab.output_var.set(str(driver.output))
    before = snapshot(driver.input)

    section(f"Thumbnails, {MIN_SUBDIR}/ layout")
    driver.app.settings.min_layout = LAYOUT_SUBFOLDER
    tab.settings = driver.app.settings
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
    driver.app.settings.min_layout = LAYOUT_BESIDE
    tab.settings = driver.app.settings
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


def test_empty_run_cleanup(driver: Driver) -> None:
    section("A run that writes nothing leaves nothing behind")
    import minjpg.batchtab as batchtab
    from minjpg.config import LAYOUT_BESIDE

    tab = driver.app.thumbnail_tab
    tab.input_var.set(str(driver.input))
    tab.output_var.set(str(driver.output))
    driver.app.settings.min_layout = LAYOUT_BESIDE
    tab.settings = driver.app.settings
    tab.confirm_start = lambda: True

    def boom(job):
        raise OSError("simulated disk failure")

    tab.scan()
    driver.pump()
    plan = tab.run_plan
    saved_copy, saved_run = batchtab.run_copy, tab.run_one
    batchtab.run_copy, tab.run_one = boom, boom
    try:
        tab.start()
        driver.pump()
        while tab.busy():
            driver.pump(0.2)
        driver.pump(0.4)
    finally:
        batchtab.run_copy, tab.run_one = saved_copy, saved_run
    check(not plan.path.exists(), f"the empty run folder was removed ({plan.path.name})")

    kept = driver.run(tab, "successful")
    check(kept.path.is_dir() and any(kept.path.rglob("*")),
          "a run that did write is never cleaned up")


def test_nothing_stray(driver: Driver, plans: list) -> None:
    section("Nothing is written outside the run folders")
    expected = {plan.path.name for plan in plans}
    stray = sorted(p.name for p in driver.output.iterdir() if p.name not in expected)
    # Runs made by the later tests are legitimate too; only non-run entries matter.
    unexpected = [name for name in stray if "_min_" not in name and "_compressed_" not in name]
    check(not unexpected, f"the output folder holds only run folders (stray: {unexpected})")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DEFAULT_DATA)
    args = parser.parse_args()

    if not args.data.is_dir():
        print(f"skipping: no sample images at {args.data}")
        return 0
    try:
        import tkinter

        probe = tkinter.Tk()
        probe.destroy()
    except Exception as exc:
        print(f"skipping: no display available ({exc})")
        return 0

    tmp = Path(tempfile.mkdtemp(prefix="minjpg-gui-"))
    driver = Driver(tmp, args.data)
    try:
        test_folders_required(driver)
        plans = test_thumbnail_layouts(driver)
        plans += test_compress_tab(driver)
        test_dialog_text(driver)
        test_stale_scan(driver)
        test_empty_run_cleanup(driver)
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
