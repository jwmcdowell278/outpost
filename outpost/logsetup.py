"""Outpost's logging: the console and the optional log file, which the web
UI's Settings page can switch off, to normal or to detailed while running.

Kept light on disk writes (SD cards wear out): run as a systemd service with
a log file, the console - which goes into the system journal - only gets
warnings, since the file already has the rest. With the file off, the
console gets normal messages again, so nothing goes unrecorded.
"""
from __future__ import annotations
import logging
import os
from logging.handlers import RotatingFileHandler

FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

# log_level values -> file handler level (None: no file).
FILE_LEVELS = {"off": None, "info": logging.INFO, "debug": logging.DEBUG}

_state: dict = {"console": None, "file": None, "verbose": False, "service": False}


def setup(verbose: bool, log_file: str | None, log_level: str,
          max_bytes: int = 10_000_000, backup_count: int = 5) -> None:
    """Console handler plus the log file, at startup."""
    root = logging.getLogger()
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter(FORMAT))
    root.addHandler(console)
    _state.update(console=console, verbose=verbose, service=bool(os.environ.get("JOURNAL_STREAM")))
    if not verbose:
        # A line per web UI page refresh - only wanted when debugging.
        logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    set_file_logging(log_file, log_level, max_bytes, backup_count)


def set_file_logging(log_file: str | None, log_level: str,
                     max_bytes: int = 10_000_000, backup_count: int = 5) -> None:
    """Switch the log file off, or (re)open it at log_level - live."""
    root = logging.getLogger()
    old = _state["file"]
    if old is not None:
        root.removeHandler(old)
        old.close()
        _state["file"] = None
    level = FILE_LEVELS.get(log_level, logging.INFO)
    if log_file and level is not None:
        log_dir = os.path.dirname(log_file)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        handler = RotatingFileHandler(log_file, maxBytes=max_bytes, backupCount=backup_count)
        handler.setLevel(level)
        handler.setFormatter(logging.Formatter(FORMAT))
        root.addHandler(handler)
        _state["file"] = handler
    _update_levels()


def file_logging_active() -> bool:
    return _state["file"] is not None


def _update_levels() -> None:
    console = _state["console"]
    if _state["verbose"]:
        console_level = logging.DEBUG
    elif _state["service"] and _state["file"] is not None:
        console_level = logging.WARNING   # the file has the rest
    else:
        console_level = logging.INFO
    if console is not None:
        console.setLevel(console_level)
    levels = [console_level] + ([_state["file"].level] if _state["file"] is not None else [])
    # The root logger has to pass everything any handler wants.
    logging.getLogger().setLevel(min(levels))


def reset() -> None:
    """Remove the handlers this module added (for tests)."""
    root = logging.getLogger()
    for key in ("console", "file"):
        h = _state[key]
        if h is not None:
            root.removeHandler(h)
            h.close()
            _state[key] = None
    _state.update(verbose=False, service=False)
