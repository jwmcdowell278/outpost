"""Verifies OutpostClient wires AGC into the tx path: on by default per
config, overridable via injection, applied before the noise suppressor and
Opus encoding."""
from __future__ import annotations
import asyncio

from outpost.agc import AutomaticGainControl
from outpost.client import OutpostClient
from outpost.config import OutpostConfig, Channel
from outpost.crypto import generate_channel_key


class _SpyProcessor:
    def __init__(self):
        self.calls: list[bytes] = []

    def process(self, pcm_bytes: bytes) -> bytes:
        self.calls.append(pcm_bytes)
        return pcm_bytes


def test_agc_enabled_by_default_from_config():
    client = OutpostClient(_make_config(), "ops")
    assert isinstance(client.agc, AutomaticGainControl)


def test_agc_disabled_when_config_turns_it_off():
    # The AGC object is always constructed (so the web UI can toggle it live
    # without a restart) - config.enable_agc=False just starts it gated off.
    client = OutpostClient(_make_config(enable_agc=False), "ops")
    assert isinstance(client.agc, AutomaticGainControl)
    assert client.agc_enabled is False


def test_web_ui_disabled_by_default_from_config():
    client = OutpostClient(_make_config(), "ops")
    assert client.web_ui is None


def test_web_ui_enabled_from_config():
    from outpost.webui import WebUI
    client = OutpostClient(_make_config(enable_web_ui=True), "ops")
    assert isinstance(client.web_ui, WebUI)


def test_subnet_scan_disabled_by_default_from_config():
    client = OutpostClient(_make_config(), "ops")
    assert client.subnet_scan is None


def test_subnet_scan_enabled_from_config():
    from outpost.subnet_scan import SubnetScanner
    client = OutpostClient(_make_config(enable_pex=True, enable_subnet_scan=True), "ops")
    assert isinstance(client.subnet_scan, SubnetScanner)


def test_subnet_scan_skipped_without_pex_since_it_has_nothing_to_probe_with(caplog):
    client = OutpostClient(_make_config(enable_pex=False, enable_subnet_scan=True), "ops")
    assert client.subnet_scan is None
    assert "subnet scan" in caplog.text


def test_injected_agc_overrides_config():
    spy = _SpyProcessor()
    client = OutpostClient(_make_config(), "ops", agc=spy)
    assert client.agc is spy


def test_tx_loop_runs_agc_before_noise_suppressor():
    asyncio.run(_tx_loop_runs_agc_before_noise_suppressor())


async def _tx_loop_runs_agc_before_noise_suppressor():
    config = _make_config()
    order: list[str] = []

    class _TaggedProcessor:
        def __init__(self, tag: str):
            self.tag = tag

        def process(self, pcm_bytes: bytes) -> bytes:
            order.append(self.tag)
            return pcm_bytes

    client = OutpostClient(
        config, "ops",
        noise_suppressor=_TaggedProcessor("noise_suppressor"),
        agc=_TaggedProcessor("agc"),
    )
    await client.transport.start(config.listen_port)
    client._ptt_active = True

    silence = b"\x00" * (client.codec.frame_size * 2)
    client.mic.read_frame = lambda timeout=None: silence

    try:
        tx_task = asyncio.create_task(client._tx_loop())
        await asyncio.sleep(0.1)
    finally:
        tx_task.cancel()
        client.transport.stop()

    assert len(order) > 0
    assert order[0] == "agc"
    assert order[1] == "noise_suppressor"


def test_highpass_enabled_by_default_and_squelch_disabled_by_default():
    client = OutpostClient(_make_config(), "ops")
    assert client.highpass_enabled is True
    assert client.squelch_enabled is False


def test_tx_loop_runs_highpass_and_squelch_before_agc():
    asyncio.run(_tx_loop_runs_highpass_and_squelch_before_agc())


async def _tx_loop_runs_highpass_and_squelch_before_agc():
    config = _make_config(enable_squelch=True)
    order: list[str] = []

    class _TaggedProcessor:
        def __init__(self, tag: str):
            self.tag = tag

        def process(self, pcm_bytes: bytes) -> bytes:
            order.append(self.tag)
            return pcm_bytes

    client = OutpostClient(
        config, "ops",
        noise_suppressor=_TaggedProcessor("noise_suppressor"),
        agc=_TaggedProcessor("agc"),
        highpass=_TaggedProcessor("highpass"),
        squelch=_TaggedProcessor("squelch"),
    )
    await client.transport.start(config.listen_port)
    client._ptt_active = True

    silence = b"\x00" * (client.codec.frame_size * 2)
    client.mic.read_frame = lambda timeout=None: silence

    try:
        tx_task = asyncio.create_task(client._tx_loop())
        await asyncio.sleep(0.1)
    finally:
        tx_task.cancel()
        client.transport.stop()

    assert len(order) > 0
    assert order[0] == "highpass"
    assert order[1] == "squelch"
    assert order[2] == "agc"
    assert order[3] == "noise_suppressor"


def _make_config(**overrides) -> OutpostConfig:
    ops_key = generate_channel_key()
    defaults = dict(
        sender_id=1,
        listen_port=0,
        mesh_key=generate_channel_key(),
        channels={1: Channel(id=1, name="ops", key=ops_key)},
        enable_mdns=False,
        enable_pex=False,
    )
    defaults.update(overrides)
    return OutpostConfig(**defaults)
