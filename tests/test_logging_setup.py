"""Outpost's logging (outpost/logsetup.py, scripts/run_client.py): the
console and the log file - kept light on SD card writes - and switching the
log file off / normal / detailed live from the web UI's Settings page."""
from __future__ import annotations
import asyncio
import logging
import sys
from pathlib import Path

import pytest
import yaml
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import run_client  # noqa: E402

from outpost import logsetup
from outpost.client import OutpostClient
from outpost.config import OutpostConfig, Channel, validate_log_level
from outpost.crypto import generate_channel_key
from outpost.webui import WebUI


@pytest.fixture
def clean_root():
    root = logging.getLogger()
    saved = (root.level, logging.getLogger("aiohttp.access").level)
    logsetup.reset()
    yield root
    logsetup.reset()
    root.setLevel(saved[0])
    logging.getLogger("aiohttp.access").setLevel(saved[1])


def _config(**kw):
    return OutpostConfig(sender_id=1, listen_port=0, mesh_key=generate_channel_key(),
                         channels={1: Channel(id=1, name="ops", key=generate_channel_key())},
                         enable_mdns=False, enable_pex=False, **kw)


def _levels():
    """(console level, log file level or None)."""
    console, f = logsetup._state["console"], logsetup._state["file"]
    return console.level, (f.level if f is not None else None)


def test_service_with_log_file_keeps_the_journal_quiet(clean_root, tmp_path, monkeypatch):
    monkeypatch.setenv("JOURNAL_STREAM", "8:1234")
    run_client.configure_logging(False, _config(log_file=str(tmp_path / "o.log")), None)
    assert _levels() == (logging.WARNING, logging.INFO)
    assert clean_root.level == logging.INFO
    assert logging.getLogger("aiohttp.access").level == logging.WARNING


def test_debug_log_level(clean_root, tmp_path, monkeypatch):
    monkeypatch.delenv("JOURNAL_STREAM", raising=False)
    run_client.configure_logging(False, _config(log_file=str(tmp_path / "o.log"), log_level="debug"), None)
    assert _levels() == (logging.INFO, logging.DEBUG)
    assert clean_root.level == logging.DEBUG


def test_off_means_no_file_and_the_journal_gets_normal_messages(clean_root, tmp_path, monkeypatch):
    monkeypatch.setenv("JOURNAL_STREAM", "8:1234")
    run_client.configure_logging(False, _config(log_file=str(tmp_path / "o.log"), log_level="off"), None)
    assert _levels() == (logging.INFO, None)
    assert not (tmp_path / "o.log").exists()


def test_switching_live(clean_root, tmp_path, monkeypatch):
    monkeypatch.setenv("JOURNAL_STREAM", "8:1234")
    path = str(tmp_path / "o.log")
    run_client.configure_logging(False, _config(log_file=path), None)
    logsetup.set_file_logging(path, "debug")
    logging.getLogger("outpost.test").debug("detail line")
    assert "detail line" in Path(path).read_text()
    logsetup.set_file_logging(path, "off")
    logging.getLogger("outpost.test").info("not written")
    assert "not written" not in Path(path).read_text()
    assert _levels() == (logging.INFO, None) and not logsetup.file_logging_active()


def test_log_level_validation():
    assert validate_log_level(None) == "info" and validate_log_level("DEBUG") == "debug"
    assert validate_log_level("off") == "off"
    with pytest.raises(ValueError):
        validate_log_level("verbose")


def test_settings_page_turns_logging_on_off_and_detailed(clean_root, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("JOURNAL_STREAM", "8:1234")
    cfg_path = tmp_path / "outpost.yaml"
    cfg_path.write_text(yaml.safe_dump({"log_level": "off"}))
    config = _config(log_level="off")
    run_client.configure_logging(False, config, None)
    client = OutpostClient(config, "ops", config_path=str(cfg_path))

    async def run():
        async with TestClient(TestServer(WebUI(client).app)) as tc:
            body = await (await tc.get("/api/config")).json()
            field = next(f for g in body["groups"] for f in g["fields"] if f["name"] == "log_level")
            assert field["choices"] == [["off", "Off"], ["info", "Normal"], ["debug", "Detailed"]]
            assert body["fields"]["log_level"] == {"value": "off", "live": True}

            # On, with no log_file configured: outpost.log, saved.
            resp = await tc.post("/api/config", json={"log_level": "debug"})
            assert resp.status == 200 and (await resp.json())["restart_required"] == []
            assert logsetup.file_logging_active()
            saved = yaml.safe_load(cfg_path.read_text())
            assert saved["log_level"] == "debug" and saved["log_file"] == "outpost.log"
            logging.getLogger("outpost.test").debug("from the settings page")
            assert "from the settings page" in (tmp_path / "outpost.log").read_text()

            assert (await tc.post("/api/config", json={"log_level": "off"})).status == 200
            assert not logsetup.file_logging_active()
            assert (await tc.post("/api/config", json={"log_level": "loud"})).status == 400
    asyncio.run(run())
