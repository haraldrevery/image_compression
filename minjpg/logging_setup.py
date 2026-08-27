"""A log file, so a failure on someone else's machine leaves a trace.

The shipped binary is built windowed (``console=False`` in ``minjpg.spec``), so
stderr goes nowhere: a traceback printed there is lost, and a user reporting
"it just stopped" has nothing to send.  Everything the app would have printed
goes to a rotating file next to the settings instead.

Setting this up is best-effort — a read-only or missing config directory must
not stop the app from starting.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path

LOGGER_NAME = "minjpg"

_MAX_BYTES = 1 << 20  # 1 MB per file, 3 kept
_BACKUPS = 3

_log_path: Path | None = None
_configured = False


def log_path() -> Path:
    """Where the log lives: beside ``settings.json``, not a second location."""
    from .config import config_path

    return config_path().parent / "minjpg.log"


def setup_logging() -> Path | None:
    """Attach the file handler once. Returns the path, or ``None`` if unusable."""
    global _configured, _log_path
    if _configured:
        return _log_path

    _configured = True
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False

    path = log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=_MAX_BYTES, backupCount=_BACKUPS, encoding="utf-8"
        )
    except OSError:
        _log_path = None
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%Y-%m-%d %H:%M:%S")
        )
        logger.addHandler(handler)
        _log_path = path

    # When there *is* a console (running from source), keep the old behaviour of
    # seeing warnings as they happen.
    if sys.stderr is not None:
        stream = logging.StreamHandler(sys.stderr)
        stream.setLevel(logging.WARNING)
        stream.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
        logger.addHandler(stream)

    return _log_path


def get_logger() -> logging.Logger:
    return logging.getLogger(LOGGER_NAME)


def describe_log() -> str:
    """One line for a dialog: where to look, or why there is nowhere to look."""
    if _log_path is None:
        return "No log file could be written."
    return f"Details were written to:\n{_log_path}"
