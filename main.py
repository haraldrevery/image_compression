#!/usr/bin/env python3
"""Entry point for minjpg.

Runs the GUI by default.  ``--selftest`` is there so a freshly built binary can
be checked without a display: it encodes a generated image with the bundled
MozJPEG and confirms the result really carries the expected settings.

The report never relies on stdout alone.  The shipped binary is built windowed,
so on Windows ``minjpg.exe --selftest`` has nowhere to print; every line also
goes to the log file, and the whole report is shown in a dialog when there is
no console to read it in.
"""

import sys


def _say(message: str) -> None:
    """Print if there is anywhere to print to, and always record it."""
    from minjpg.logging_setup import get_logger

    _REPORT.append(message)
    get_logger().info("selftest: %s", message)
    if sys.stdout is not None:
        print(message)


_REPORT: list[str] = []


def _finish(status: int) -> int:
    """Show the report when stdout went nowhere, so the run is not silent."""
    if sys.stdout is not None:
        return status
    from minjpg.logging_setup import describe_log

    try:
        from tkinter import Tk, messagebox

        root = Tk()
        root.withdraw()
        body = "\n".join(_REPORT) + f"\n\n{describe_log()}"
        if status:
            messagebox.showerror("minjpg selftest FAILED", body)
        else:
            messagebox.showinfo("minjpg selftest OK", body)
        root.destroy()
    except Exception:
        pass  # no display either; the log file is still there
    return status


def selftest() -> int:
    from PIL import Image, JpegImagePlugin
    from minjpg import __version__, encoder, formats

    _say(f"minjpg {__version__}")
    try:
        _say(f"encoder: {encoder.cjpeg_version()}")
        _say(f"binary:  {encoder.cjpeg_path()}")
    except Exception as exc:
        _say(f"FAIL: {exc}")
        return 1

    _say(f"formats: {formats.describe_support()}")
    if not formats.HEIF_AVAILABLE:
        _say("WARN: HEIC/HEIF input is unavailable in this build")

    image = Image.linear_gradient("L").resize((640, 480)).convert("RGB")
    try:
        data = encoder.encode(image, 60, 30)
    except Exception as exc:
        _say(f"FAIL: encoding failed: {exc}")
        return 1

    import io

    with Image.open(io.BytesIO(data)) as decoded:
        problems = []
        if decoded.size != (640, 480):
            problems.append(f"size is {decoded.size}, expected (640, 480)")
        if JpegImagePlugin.get_sampling(decoded) != 2:
            problems.append("not 4:2:0 chroma subsampling")
        if not decoded.info.get("progressive"):
            problems.append("not progressive")
        if decoded.info.get("exif") or decoded.info.get("icc_profile"):
            problems.append("carries metadata")

    if problems:
        _say("FAIL: " + "; ".join(problems))
        return 1

    _say(f"encoded {len(data)} bytes: 4:2:0, progressive, no metadata")

    if formats.HEIF_AVAILABLE:
        import tempfile
        from pathlib import Path

        from minjpg import convert
        from minjpg.config import ConvertSettings

        with tempfile.TemporaryDirectory() as tmp:
            heic = Path(tmp) / "probe.heic"
            try:
                image.save(heic, quality=80)
                result = convert.convert(
                    heic, Path(tmp) / "probe.jpg",
                    ConvertSettings(max_long_edge=320, quality=70, max_size=0),
                )
            except Exception as exc:
                _say(f"FAIL: HEIC round-trip failed: {exc}")
                return 1
            _say(f"heic round-trip: {result.output_size} {result.kilobytes:.1f} KB OK")

    _say("selftest OK")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        from minjpg.logging_setup import setup_logging

        setup_logging()
        sys.exit(_finish(selftest()))
    from minjpg.app import main

    sys.exit(main())
