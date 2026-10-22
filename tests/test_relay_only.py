"""Relay-only mode (`relay_only`): a node with no audio, PTT or channels that
just forwards traffic - e.g. a Linux server that is also the WireGuard hub."""
from __future__ import annotations
import asyncio
import base64

import pytest
from aiohttp.test_utils import TestClient, TestServer

from outpost.client import OutpostClient
from outpost.config import OutpostConfig, Channel, Peer, load_config, validate_config_yaml
from outpost.crypto import generate_channel_key
from outpost.protocol import TYPE_AUDIO
from outpost.webui import WebUI


def _b64(key: bytes) -> str:
    return base64.b64encode(key).decode()


def _write(tmp_path, body: str):
    path = tmp_path / "outpost.yaml"
    path.write_text(f"mesh_key: {_b64(generate_channel_key())}\nsender_id: 7\n" + body)
    return str(path)


def test_relay_only_config_needs_no_channels_and_turns_relay_on(tmp_path):
    config = load_config(_write(tmp_path, "relay_only: true\n"))
    assert config.relay_only
    assert config.enable_relay
    assert config.channels == {}


def test_relay_only_ignores_configured_channels(tmp_path):
    config = load_config(_write(tmp_path, (
        "relay_only: true\n"
        f"channels:\n  - {{id: 1, name: ops, key: {_b64(generate_channel_key())}}}\n"
        "monitor_channels: [ops]\n")))
    assert config.channels == {}
    assert config.monitor_channels == []


def test_channels_still_required_without_relay_only(tmp_path):
    with pytest.raises(ValueError, match="relay_only"):
        load_config(_write(tmp_path, ""))


def test_relay_only_flag_overrides_config_file(tmp_path):
    config = load_config(_write(tmp_path, ""), relay_only=True)
    assert config.relay_only and config.enable_relay


def test_imported_relay_only_config_without_channels_is_valid():
    validate_config_yaml(f"mesh_key: {_b64(generate_channel_key())}\nrelay_only: true\n")
    with pytest.raises(ValueError):
        validate_config_yaml(f"mesh_key: {_b64(generate_channel_key())}\n")


def _relay_only_client(sender_id: int = 99) -> OutpostClient:
    config = OutpostConfig(sender_id=sender_id, listen_port=0, mesh_key=generate_channel_key(),
                           relay_only=True, enable_mdns=False, enable_pex=False, enable_multicast=False)
    return OutpostClient(config, None)


def _client(sender_id: int, key: bytes) -> OutpostClient:
    config = OutpostConfig(sender_id=sender_id, listen_port=0, mesh_key=generate_channel_key(),
                           channels={1: Channel(id=1, name="ops", key=key)},
                           enable_mdns=False, enable_pex=False, enable_multicast=False)
    return OutpostClient(config, "ops")


def test_relay_only_client_opens_no_audio_and_ignores_ptt():
    relay = _relay_only_client()
    assert relay.mic is None and relay.speaker is None
    assert relay.active_channel is None
    assert relay.enable_relay
    relay.set_ptt(True)  # no channel to talk on - must not raise
    assert relay.listening_channel_ids() == set()
    with pytest.raises(ValueError):
        relay.add_channel(1, "ops", generate_channel_key())
    with pytest.raises(ValueError):
        relay.enable_relay = False


def test_relay_only_client_starts_stops_and_forwards():
    asyncio.run(_starts_stops_and_forwards())


async def _starts_stops_and_forwards():
    key = generate_channel_key()
    a, c = _client(1, key), _client(3, key)
    relay = _relay_only_client()
    received: list[int] = []
    original = c._on_packet
    c.transport._on_packet = lambda h, ct, addr: (received.append(h.ptype), original(h, ct, addr))

    await a.transport.start(0)
    await c.transport.start(0)
    await relay.start()  # full start: no audio devices touched
    try:
        port = lambda cl: cl.transport._transport.get_extra_info("sockname")[1]
        relay.transport.add_peer("a", Peer(host="127.0.0.1", port=port(a)))
        relay.transport.add_peer("c", Peer(host="127.0.0.1", port=port(c)))
        a.transport.send_encrypted(a.ciphers[1], 1, TYPE_AUDIO, b"opus-frame",
                                   targets=[Peer(host="127.0.0.1", port=port(relay))])
        await asyncio.sleep(0.3)
        assert TYPE_AUDIO in received
    finally:
        await relay.stop()
        a.transport.stop()
        c.transport.stop()


def test_web_ui_status_works_for_relay_only():
    asyncio.run(_web_ui_status())


async def _web_ui_status():
    relay = _relay_only_client()
    async with TestClient(TestServer(WebUI(relay).app)) as tc:
        status = await (await tc.get("/api/status")).json()
        assert status["relay_only"] is True
        assert status["active_channel"] is None
        assert status["channel_list"] == []
