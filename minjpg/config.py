"""Settings for both pipelines, persisted as JSON between runs.

``Settings`` drives the Thumbnails tab (``_min.jpg``), ``ConvertSettings`` the
Compress images tab.  Both live in one file under separate keys.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path

from .common import write_atomic

#: Source extensions the scanner accepts (lowercase, with leading dot).
JPEG_EXTENSIONS = frozenset({".jpg", ".jpeg"})
OTHER_EXTENSIONS = frozenset({".png", ".webp", ".tif", ".tiff", ".bmp"})

#: Layouts for the thumbnail run folder.
LAYOUT_SUBFOLDER = "subfolder"  # a _min/ tree holding only the thumbnails
LAYOUT_BESIDE = "beside"        # a full copy of the input tree, thumbnails alongside
MIN_LAYOUTS = (LAYOUT_SUBFOLDER, LAYOUT_BESIDE)

#: The folder level inserted under the run folder by LAYOUT_SUBFOLDER.
MIN_SUBDIR = "_min"


@dataclass
class Settings:
    """Everything the pipeline and the scanner need to know.

    Defaults reproduce the user's Squoosh habits: fit inside a 1280 px long
    edge, aim for ~68 KB with 70 KiB as an absolute ceiling, and search quality
    up to 75 (Squoosh's own default) so images that are easy to compress stay
    small instead of spending the whole budget.
    """

    # Sizing
    max_long_edge: int = 1280
    max_short_edge: int = 0  # 0 disables the short-edge cap

    # Byte budget
    size_target: int = 68_000  # quality search aims at or below this
    size_hard_cap: int = 71_680  # 70 KiB, never exceeded

    # MozJPEG
    quality_floor: int = 30
    quality_ceiling: int = 75
    smoothing: int = 30
    linear_light_resize: bool = True

    # Shrink fallback, used only when quality_floor still busts the hard cap
    max_shrink_rounds: int = 6
    min_long_edge: int = 480

    # Scanning
    recursive: bool = True
    jpeg_only: bool = False
    force: bool = False

    # Where results go.  This is the folder the user picks; the run folder is
    # created *inside* it, so nothing that was already there is ever written to.
    # There is deliberately no default and no in-place mode: a blank value means
    # the app refuses to run rather than choosing a destination itself.
    output_parent: str = ""
    min_layout: str = LAYOUT_SUBFOLDER

    # Remembered between sessions for convenience
    last_folder: str = ""

    def source_extensions(self) -> frozenset[str]:
        if self.jpeg_only:
            return JPEG_EXTENSIONS
        return JPEG_EXTENSIONS | OTHER_EXTENSIONS

    def validate(self) -> None:
        """Clamp values into sane ranges, raising on ones that make no sense."""
        if self.max_long_edge < 16:
            raise ValueError("Max long edge must be at least 16 px.")
        if self.max_short_edge < 0:
            raise ValueError("Max short edge cannot be negative (use 0 to disable).")
        if not 1 <= self.quality_floor <= 100:
            raise ValueError("Quality floor must be between 1 and 100.")
        if not 1 <= self.quality_ceiling <= 100:
            raise ValueError("Quality ceiling must be between 1 and 100.")
        if self.quality_floor > self.quality_ceiling:
            raise ValueError("Quality floor cannot exceed the quality ceiling.")
        if not 0 <= self.smoothing <= 100:
            raise ValueError("Smoothing must be between 0 and 100.")
        if self.size_target < 1024:
            raise ValueError("Size target must be at least 1 KB.")
        if self.size_hard_cap < self.size_target:
            raise ValueError("Hard cap cannot be below the size target.")
        if self.min_long_edge < 16:
            raise ValueError("Minimum long edge must be at least 16 px.")
        if self.min_layout not in MIN_LAYOUTS:
            raise ValueError(
                f"Thumbnail layout must be one of {', '.join(MIN_LAYOUTS)}."
            )


@dataclass
class ConvertSettings:
    """Settings for the Compress images tab.

    Defaults come from the existing high-resolution originals: quality median
    65, long edge median 3840, p90 size 646 KB.  The 600 KB cap is a preference
    rather than a rule — unlike the 70 KB budget on the ``_min`` side, a file
    that cannot fit is still written, just flagged.
    """

    max_long_edge: int = 3840
    quality: int = 65
    max_size: int = 614_400  # 600 KiB; 0 disables the cap
    quality_floor: int = 40  # lowest the cap search will go
    smoothing: int = 0  # high-res output does not want the _min tab's 30

    strip_metadata: bool = False  # default keeps all EXIF, GPS included
    passthrough: bool = True  # copy JPEGs that already fit, no re-encode
    linear_light_resize: bool = True

    recursive: bool = True
    force: bool = False

    input_folder: str = ""
    # As on the _min side: the folder the run folder is created inside.
    output_parent: str = ""

    def validate(self) -> None:
        if self.max_long_edge < 16:
            raise ValueError("Max long edge must be at least 16 px.")
        if not 1 <= self.quality <= 100:
            raise ValueError("Quality must be between 1 and 100.")
        if not 1 <= self.quality_floor <= 100:
            raise ValueError("Quality floor must be between 1 and 100.")
        if self.quality_floor > self.quality:
            raise ValueError("Quality floor cannot exceed the quality.")
        if not 0 <= self.smoothing <= 100:
            raise ValueError("Smoothing must be between 0 and 100.")
        if self.max_size and self.max_size < 1024:
            raise ValueError("Max size must be at least 1 KB (or 0 to disable).")


def config_path() -> Path:
    """Per-user settings file, following the platform convention."""
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "minjpg" / "settings.json"


def _read_config() -> tuple[dict, str | None]:
    """The raw config, plus why it could not be read if it could not be."""
    path = config_path()
    if not path.is_file():
        return {}, None  # first run, nothing to explain
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        return {}, f"{path} could not be read ({exc})"
    except ValueError as exc:
        return {}, f"{path} is not valid JSON ({exc})"
    if not isinstance(raw, dict):
        return {}, f"{path} does not contain a settings object"
    return raw, None


def _build(cls, values: dict) -> tuple[object, str | None]:
    """Construct a settings dataclass from a dict, ignoring unknown keys.

    Anything that fails validation falls back to defaults wholesale — a
    half-applied config is harder to reason about than a fresh one.  The reason
    comes back with it so the app can say what happened instead of silently
    discarding values the user tuned.

    Unknown keys are dropped, which is also the migration path for configs
    written before this: the old ``output_folder``/``mirror_subfolders`` keys
    simply disappear.  That is deliberate.  ``output_folder`` used to be able to
    hold the *input* folder, meaning "write in place"; carrying it over as the
    new ``output_parent`` would silently aim a run's output folder at the user's
    own photos.  A blank value that makes the app ask is the safe answer.
    """
    known = {f.name for f in fields(cls)}
    try:
        instance = cls(**{k: v for k, v in values.items() if k in known})
        instance.validate()
    except (TypeError, ValueError) as exc:
        return cls(), str(exc)
    return instance, None


def load_settings() -> tuple[Settings, str | None]:
    """The ``_min`` settings, and why they are defaults if they had to be."""
    raw, problem = _read_config()
    # Files written before the converter existed are flat, not nested.
    settings, invalid = _build(Settings, raw.get("minjpg", raw))
    return settings, problem or invalid


def load_convert_settings() -> tuple[ConvertSettings, str | None]:
    raw, problem = _read_config()
    settings, invalid = _build(ConvertSettings, raw.get("convert", {}))
    return settings, problem or invalid


def save_settings(
    settings: Settings | None = None, convert: ConvertSettings | None = None
) -> None:
    """Best-effort persist; a read-only config dir must not break the app.

    Either section can be saved on its own — whatever is not passed is carried
    over from the file so one tab never clobbers the other's settings.
    """
    raw, _problem = _read_config()
    existing = raw.get("minjpg", raw if "convert" not in raw else {})
    merged = {
        "minjpg": asdict(settings) if settings is not None else existing,
        "convert": asdict(convert) if convert is not None else raw.get("convert", {}),
    }
    try:
        # Atomic: a crash or a full disk mid-write used to truncate the file,
        # which is exactly how it becomes the unreadable JSON above.
        write_atomic(config_path(), json.dumps(merged, indent=2).encode("utf-8"))
    except OSError:
        pass
