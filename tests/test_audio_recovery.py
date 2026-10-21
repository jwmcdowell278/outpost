"""Tests for recovering from a dead mic/speaker (e.g. Bluetooth earbuds
disconnecting while the client runs): stall detection in outpost/audio.py,
restart_streams' fallback/switch-back, and OutpostClient's watchdog.
sounddevice is faked - no real audio hardware is touched."""
from __future__ import annotations
import asyncio
import pytest

from outpost import audio, client as client_mod
from outpost.client import OutpostClient
from outpost.config import OutpostConfig, Channel
from outpost.crypto import generate_channel_key

BUILTIN_MIC = {"index": 0, "name": "MacBook Air Microphone", "hostapi": 0, "max_input_channels": 1, "max_output_channels": 0}
BUILTIN_SPK = {"index": 1, "name": "MacBook Air Speakers", "hostapi": 0, "max_input_channels": 0, "max_output_channels": 2}
BUDS = {"index": 2, "name": "John's Buds FE", "hostapi": 0, "max_input_channels": 1, "max_output_channels": 1}


class FakeSD:
    """Stands in for the sounddevice module. `devices` is what PortAudio
    currently 'sees' - like the real thing, only refreshed by a rescan."""

    class default:
        device = [0, 1]

    def __init__(self, devices):
        self.connected = list(devices)  # what's physically plugged in
        self.devices = list(devices)    # PortAudio's (possibly stale) view
        self.opened = []                # (kind, device index) per stream opened
        self.rescans = 0
        self.fail_open = False

    def query_devices(self, device=None, kind=None):
        if kind is None:
            return self.devices
        if device is None:
            device = self.default.device[0 if kind == "input" else 1]
        return next(d for d in self.devices if d["index"] == device)

    def query_hostapis(self):
        return [{"name": "Core Audio"}]

    def _terminate(self):
        pass

    def _initialize(self):
        self.rescans += 1
        self.devices = list(self.connected)

    def _stream(self, kind):
        fake = self

        class Stream:
            def __init__(self, device=None, **kwargs):
                if fake.fail_open:
                    raise RuntimeError("no audio device")
                fake.opened.append((kind, device))

            def start(self): pass
            def stop(self): pass
            def close(self): pass
        return Stream

    @property
    def RawInputStream(self):
        return self._stream("input")

    @property
    def RawOutputStream(self):
        return self._stream("output")


@pytest.fixture
def fake_sd(monkeypatch):
    fake = FakeSD([BUILTIN_MIC, BUILTIN_SPK, BUDS])
    monkeypatch.setattr(audio, "sd", fake)
    return fake


def test_stream_is_not_stalled_until_callbacks_stop(fake_sd):
    mic = audio.MicCapture(48000, 960)
    assert not mic.stalled()  # never started
    mic.start()
    assert not mic.stalled()
    mic._callback(b"\x00\x00" * 960, 960, None, None)
    assert not mic.stalled(now=mic.last_callback + 1)
    assert mic.stalled(now=mic.last_callback + audio.STALL_TIMEOUT_S + 0.1)
    mic.stop()
    assert not mic.stalled(now=mic.last_callback + 100)  # stopped on purpose


def test_restart_falls_back_to_default_when_device_disappears(fake_sd):
    mic = audio.MicCapture(48000, 960, device="buds")
    mic.start()
    assert fake_sd.opened[-1] == ("input", 2) and not mic.on_fallback

    fake_sd.connected.remove(BUDS)  # earbuds disconnect
    audio.restart_streams(mic)
    assert fake_sd.rescans == 1
    assert fake_sd.opened[-1] == ("input", None)  # system default
    assert mic.on_fallback and mic.device_name == "MacBook Air Microphone"

    fake_sd.connected.append(BUDS)  # and come back
    audio.restart_streams(mic)
    assert fake_sd.opened[-1] == ("input", 2) and not mic.on_fallback


def test_failed_reopen_counts_as_stalled_so_it_is_retried(fake_sd):
    mic = audio.MicCapture(48000, 960)
    mic.start()
    fake_sd.fail_open = True
    with pytest.raises(RuntimeError):
        audio.restart_streams(mic)
    assert mic.stalled()
    fake_sd.fail_open = False
    audio.restart_streams(mic)
    assert not mic.stalled()


def test_mic_keeps_feeding_same_queue_after_restart(fake_sd):
    mic = audio.MicCapture(48000, 960)
    mic.start()
    audio.restart_streams(mic)
    mic._callback(b"\x01\x00" * 960, 960, None, None)
    assert mic.read_frame(timeout=0.1) == b"\x01\x00" * 960


def _make_client(**overrides) -> OutpostClient:
    config = OutpostConfig(
        sender_id=1, listen_port=0, mesh_key=generate_channel_key(),
        channels={1: Channel(id=1, name="ops", key=generate_channel_key())},
        enable_mdns=False, enable_pex=False, **overrides,
    )
    return OutpostClient(config, "ops")


async def _run_watchdog(client, ticks):
    task = asyncio.create_task(client._audio_watchdog_loop())
    try:
        for _ in range(ticks):
            await asyncio.sleep(0.02)
    finally:
        task.cancel()


def test_watchdog_reopens_stalled_mic(fake_sd, monkeypatch, caplog):
    monkeypatch.setattr(client_mod, "_AUDIO_WATCHDOG_INTERVAL_S", 0.01)
    client = _make_client(input_device="buds", output_device="buds")
    client.mic.start()
    client.speaker.start()
    fake_sd.connected.remove(BUDS)
    # Earbuds gone: no more callbacks, so both streams go stale.
    client.mic.last_callback -= 10
    client.speaker.last_callback -= 10

    asyncio.run(_run_watchdog(client, 5))

    assert client.mic.device_name == "MacBook Air Microphone"
    assert client.speaker.device_name == "MacBook Air Speakers"
    assert client.mic.on_fallback and client.speaker.on_fallback
    assert "stopped delivering audio" in caplog.text


def test_watchdog_switches_back_when_configured_device_returns(fake_sd, monkeypatch):
    monkeypatch.setattr(client_mod, "_AUDIO_WATCHDOG_INTERVAL_S", 0.01)
    monkeypatch.setattr(client_mod, "_AUDIO_DEVICE_RECHECK_S", 0.0)

    async def fresh():
        fake_sd.devices = list(fake_sd.connected)
        return {"input": audio.list_devices("input"), "output": audio.list_devices("output")}
    monkeypatch.setattr(client_mod, "list_devices_fresh", fresh)

    fake_sd.connected.remove(BUDS)
    fake_sd.devices = list(fake_sd.connected)
    client = _make_client(input_device="buds")
    client.mic.start()
    client.speaker.start()
    assert client.mic.on_fallback

    fake_sd.connected.append(BUDS)
    # Keep streams "alive" so only the device recheck can trigger a restart.
    async def run():
        task = asyncio.create_task(client._audio_watchdog_loop())
        for _ in range(10):
            client.mic.last_callback = client.speaker.last_callback = audio.time.monotonic()
            await asyncio.sleep(0.02)
        task.cancel()
    asyncio.run(run())

    assert client.mic.device_name == "John's Buds FE" and not client.mic.on_fallback
