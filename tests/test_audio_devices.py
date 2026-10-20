"""Tests for picking a specific mic/speaker when a node has several
(outpost/audio.py's resolve_device, plus the input_device/output_device
config fields and web UI endpoint). sounddevice's device queries are faked,
so no real audio hardware is touched."""
from __future__ import annotations
import asyncio
import base64
import logging
import pytest
import yaml
from aiohttp.test_utils import TestClient, TestServer

from outpost import audio
from outpost.client import OutpostClient
from outpost.config import EDITABLE_FIELDS, OutpostConfig, Channel, load_config
from outpost.crypto import generate_channel_key
from outpost.webui import WebUI

_FAKE_DEVICES = [
    {"index": 0, "name": "MacBook Air Microphone", "hostapi": 0, "max_input_channels": 1, "max_output_channels": 0},
    {"index": 1, "name": "MacBook Air Speakers", "hostapi": 0, "max_input_channels": 0, "max_output_channels": 2},
    {"index": 2, "name": "USB Audio Device", "hostapi": 0, "max_input_channels": 1, "max_output_channels": 2},
    {"index": 3, "name": "USB Audio Device #2", "hostapi": 0, "max_input_channels": 1, "max_output_channels": 2},
]


class _FakeDefault:
    device = [0, 1]


@pytest.fixture(autouse=True)
def fake_devices(monkeypatch):
    monkeypatch.setattr(audio.sd, "query_devices", lambda *a, **k: _FAKE_DEVICES)
    monkeypatch.setattr(audio.sd, "query_hostapis", lambda *a, **k: [{"name": "Core Audio"}])
    monkeypatch.setattr(audio.sd, "default", _FakeDefault)


def test_list_devices_filters_by_direction_and_marks_default():
    inputs = audio.list_devices("input")
    outputs = audio.list_devices("output")
    assert [d["index"] for d in inputs] == [0, 2, 3]
    assert [d["index"] for d in outputs] == [1, 2, 3]
    assert inputs[0]["default"] is True and inputs[1]["default"] is False
    assert outputs[0]["hostapi"] == "Core Audio"


def test_resolve_none_or_blank_means_system_default():
    assert audio.resolve_device(None, "input") is None
    assert audio.resolve_device("", "output") is None


def test_resolve_by_case_insensitive_name_substring():
    assert audio.resolve_device("usb", "input") == 2
    assert audio.resolve_device("speakers", "output") == 1


def test_resolve_exact_name_beats_partial_match():
    assert audio.resolve_device("usb audio device #2", "input") == 3
    # Exact match on index 2's name wins even though "#2" also contains it.
    assert audio.resolve_device("USB Audio Device", "input") == 2


def test_resolve_by_index_checks_direction():
    assert audio.resolve_device(2, "output") == 2
    # Index 1 is speakers-only, so not usable as a mic.
    assert audio.resolve_device(1, "input") is None


def test_resolve_unknown_name_falls_back_to_default_with_warning(caplog):
    with caplog.at_level(logging.WARNING):
        assert audio.resolve_device("Blue Yeti", "input") is None
    assert "Blue Yeti" in caplog.text


def test_resolve_does_not_match_device_in_wrong_direction():
    # "Speakers" only exists as an output.
    assert audio.resolve_device("Speakers", "input") is None


def _write_config(path, extra: str = "") -> None:
    path.write_text(f"""
mesh_key: "{base64.b64encode(generate_channel_key()).decode()}"
channels:
  - id: 1
    name: ops
    key: "{base64.b64encode(generate_channel_key()).decode()}"
{extra}
""")


def test_config_loads_device_names_and_indices(tmp_path):
    path = tmp_path / "outpost.yaml"
    _write_config(path, 'input_device: "USB"\noutput_device: 1\n')
    config = load_config(str(path))
    assert config.input_device == "USB"
    assert config.output_device == 1


def test_config_devices_default_to_none(tmp_path):
    path = tmp_path / "outpost.yaml"
    _write_config(path)
    config = load_config(str(path))
    assert config.input_device is None and config.output_device is None


@pytest.mark.parametrize("raw,expected", [
    (None, None), ("", None), ("  ", None), ("USB ", "USB"), ("3", 3), (3, 3),
])
def test_device_validator_accepts(raw, expected):
    validator, live = EDITABLE_FIELDS["input_device"]
    assert live is False  # needs a restart to reopen the audio stream
    assert validator(raw) == expected


@pytest.mark.parametrize("raw", [-1, True, 1.5, ["USB"], "x" * 129])
def test_device_validator_rejects(raw):
    with pytest.raises(ValueError):
        EDITABLE_FIELDS["output_device"][0](raw)


def _make_client(config_path=None) -> OutpostClient:
    config = OutpostConfig(
        sender_id=1, listen_port=0, mesh_key=generate_channel_key(),
        channels={1: Channel(id=1, name="ops", key=generate_channel_key())},
        enable_mdns=False, enable_pex=False,
    )
    return OutpostClient(config, "ops", config_path=config_path)


def test_webui_lists_audio_devices():
    async def run():
        async with TestClient(TestServer(WebUI(_make_client()).app)) as tc:
            body = await (await tc.get("/api/audio_devices")).json()
            assert [d["name"] for d in body["input"]] == [
                "MacBook Air Microphone", "USB Audio Device", "USB Audio Device #2"]
            assert [d["index"] for d in body["output"]] == [1, 2, 3]
    asyncio.run(run())


def test_webui_saves_selected_device_as_restart_required(tmp_path):
    path = tmp_path / "outpost.yaml"
    _write_config(path)

    async def run():
        client = _make_client(config_path=str(path))
        async with TestClient(TestServer(WebUI(client).app)) as tc:
            resp = await tc.post("/api/config", json={"input_device": "USB Audio Device", "output_device": ""})
            assert resp.status == 200
            body = await resp.json()
            assert set(body["restart_required"]) == {"input_device", "output_device"}
        saved = yaml.safe_load(path.read_text())
        assert saved["input_device"] == "USB Audio Device"
        assert saved["output_device"] is None
    asyncio.run(run())
