"""scripts/run_client.py's configure_logging: what goes to the console and
the log file - kept light on SD card writes."""
from __future__ import annotations
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_client  # noqa: E402

from outpost.config import OutpostConfig, validate_log_level
from outpost.crypto import generate_channel_key


@pytest.fixture
def clean_root():
    root = logging.getLogger()
    saved = (root.level, list(root.handlers), logging.getLogger("aiohttp.access").level)
    yield root
    for h in root.handlers:
        if h not in saved[1]:
            h.close()
    root.handlers[:] = saved[1]
    root.setLevel(saved[0])
    logging.getLogger("aiohttp.access").setLevel(saved[2])


def _config(**kw):
    return OutpostConfig(sender_id=1, listen_port=0, mesh_key=generate_channel_key(), **kw)


def _levels(root):
    """(console level, log file level or None) of the handlers
    configure_logging added - not pytest's own."""
    from logging.handlers import RotatingFileHandler
    console = [h for h in root.handlers if type(h) is logging.StreamHandler]
    files = [h for h in root.handlers if isinstance(h, RotatingFileHandler)]
    return console[-1].level, (files[-1].level if files else None)


def test_service_with_log_file_keeps_the_journal_quiet(clean_root, tmp_path, monkeypatch):
    monkeypatch.setenv("JOURNAL_STREAM", "8:1234")
    run_client.configure_logging(False, _config(log_file=str(tmp_path / "o.log")), None)
    assert _levels(clean_root) == (logging.WARNING, logging.INFO)
    assert clean_root.level == logging.INFO
    assert logging.getLogger("aiohttp.access").level == logging.WARNING


def test_debug_log_level_and_verbose(clean_root, tmp_path, monkeypatch):
    monkeypatch.delenv("JOURNAL_STREAM", raising=False)
    run_client.configure_logging(False, _config(log_file=str(tmp_path / "o.log"), log_level="debug"), None)
    assert _levels(clean_root) == (logging.INFO, logging.DEBUG)
    assert clean_root.level == logging.DEBUG


def test_no_log_file_from_a_terminal(clean_root, monkeypatch):
    monkeypatch.delenv("JOURNAL_STREAM", raising=False)
    run_client.configure_logging(True, _config(), None)
    assert _levels(clean_root) == (logging.DEBUG, None)


def test_log_level_validation():
    assert validate_log_level(None) == "info" and validate_log_level("DEBUG") == "debug"
    with pytest.raises(ValueError):
        validate_log_level("verbose")
