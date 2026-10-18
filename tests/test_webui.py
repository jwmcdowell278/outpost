"""Tests for the optional local web UI (outpost/webui.py): status reporting,
channel switching, PTT control, and bearer-token auth - using aiohttp's test
client directly against the WebUI's app (no real OutpostClient.start(), so no
real sockets/mic/speaker are touched).

Project convention: no pytest-asyncio plugin installed, so each async body
is driven via asyncio.run() from a plain sync test function (see
tests/test_channel_scoping.py for the same pattern)."""
from __future__ import annotations
import asyncio
import base64
import pytest
import yaml
from aiohttp.test_utils import TestClient, TestServer

from outpost.client import OutpostClient
from outpost.config import OutpostConfig, Channel
from outpost.crypto import generate_channel_key
from outpost.webui import WebUI


def _make_client(config_path=None, **config_overrides) -> OutpostClient:
    ops_key = generate_channel_key()
    tac_key = generate_channel_key()
    defaults = dict(
        sender_id=1,
        listen_port=0,
        mesh_key=generate_channel_key(),
        channels={1: Channel(id=1, name="ops", key=ops_key), 2: Channel(id=2, name="tac", key=tac_key)},
        enable_mdns=False,
        enable_pex=False,
    )
    defaults.update(config_overrides)
    return OutpostClient(OutpostConfig(**defaults), "ops", config_path=config_path)


def test_status_reports_active_channel_and_channel_list():
    asyncio.run(_status_reports_active_channel_and_channel_list())


async def _status_reports_active_channel_and_channel_list():
    ui = WebUI(_make_client())
    async with TestClient(TestServer(ui.app)) as tc:
        resp = await tc.get("/api/status")
        assert resp.status == 200
        body = await resp.json()
        assert body["active_channel"] == "ops"
        assert set(body["channels"]) == {"ops", "tac"}
        assert body["ptt_active"] is False
        assert body["sender_alias"] is None
        assert body["speaking"] == []


def test_set_channel_switches_active_channel():
    asyncio.run(_set_channel_switches_active_channel())


async def _set_channel_switches_active_channel():
    ui = WebUI(_make_client())
    async with TestClient(TestServer(ui.app)) as tc:
        resp = await tc.post("/api/channel", json={"name": "tac"})
        assert resp.status == 200
        assert (await resp.json())["active_channel"] == "tac"

        status = await (await tc.get("/api/status")).json()
        assert status["active_channel"] == "tac"


def test_set_channel_rejects_unknown_name():
    asyncio.run(_set_channel_rejects_unknown_name())


async def _set_channel_rejects_unknown_name():
    ui = WebUI(_make_client())
    async with TestClient(TestServer(ui.app)) as tc:
        resp = await tc.post("/api/channel", json={"name": "nope"})
        assert resp.status == 404


def test_set_ptt_updates_status():
    asyncio.run(_set_ptt_updates_status())


async def _set_ptt_updates_status():
    ui = WebUI(_make_client())
    async with TestClient(TestServer(ui.app)) as tc:
        resp = await tc.post("/api/ptt", json={"pressed": True})
        assert resp.status == 200
        assert (await resp.json())["ptt_active"] is True

        status = await (await tc.get("/api/status")).json()
        assert status["ptt_active"] is True


def test_index_serves_html_without_auth_errors():
    asyncio.run(_index_serves_html_without_auth_errors())


async def _index_serves_html_without_auth_errors():
    ui = WebUI(_make_client())
    async with TestClient(TestServer(ui.app)) as tc:
        resp = await tc.get("/")
        assert resp.status == 200
        assert "text/html" in resp.content_type


def test_token_required_when_configured():
    asyncio.run(_token_required_when_configured())


async def _token_required_when_configured():
    ui = WebUI(_make_client(), token="secret")
    async with TestClient(TestServer(ui.app)) as tc:
        unauthenticated = await tc.get("/api/status")
        assert unauthenticated.status == 401

        authenticated = await tc.get("/api/status", headers={"Authorization": "Bearer secret"})
        assert authenticated.status == 200


def test_pages_load_without_auth_but_api_still_requires_token():
    asyncio.run(_pages_load_without_auth_but_api_still_requires_token())


async def _pages_load_without_auth_but_api_still_requires_token():
    # A plain browser navigation never sends a custom Authorization header,
    # so the static page shells (which carry no secrets) must load even
    # when a token is configured - only api/* needs it.
    ui = WebUI(_make_client(), token="secret")
    async with TestClient(TestServer(ui.app)) as tc:
        for path in ("/", "/settings", "/channels", "/peers"):
            resp = await tc.get(path)
            assert resp.status == 200, path
            assert "text/html" in resp.content_type

        assert (await tc.get("/api/status")).status == 401
        assert (await tc.post("/api/ptt", json={"pressed": True})).status == 401


def test_generate_key_returns_fresh_base64_key():
    asyncio.run(_generate_key_returns_fresh_base64_key())


async def _generate_key_returns_fresh_base64_key():
    import base64
    ui = WebUI(_make_client())
    async with TestClient(TestServer(ui.app)) as tc:
        resp = await tc.post("/api/generate_key")
        assert resp.status == 200
        body = await resp.json()
        key_bytes = base64.b64decode(body["key"])
        assert len(key_bytes) == 32

        other = await (await tc.post("/api/generate_key")).json()
        assert other["key"] != body["key"]


def test_generate_key_requires_auth_when_configured():
    asyncio.run(_generate_key_requires_auth_when_configured())


async def _generate_key_requires_auth_when_configured():
    ui = WebUI(_make_client(), token="secret")
    async with TestClient(TestServer(ui.app)) as tc:
        unauthenticated = await tc.post("/api/generate_key")
        assert unauthenticated.status == 401

        authenticated = await tc.post("/api/generate_key", headers={"Authorization": "Bearer secret"})
        assert authenticated.status == 200


def test_webui_raises_clear_error_without_aiohttp(monkeypatch):
    from outpost import webui as webui_module
    monkeypatch.setattr(webui_module, "web", None)
    with pytest.raises(RuntimeError, match="pip install -e '.\\[web\\]'|web.*extra"):
        webui_module.WebUI(_make_client())


def test_status_includes_peer_and_channel_member_detail():
    asyncio.run(_status_includes_peer_and_channel_member_detail())


async def _status_includes_peer_and_channel_member_detail():
    client = _make_client()
    from outpost.config import Peer
    client.transport.add_peer("gossip-10.0.0.5:9000", Peer(host="10.0.0.5", port=9000))
    ui = WebUI(client)
    async with TestClient(TestServer(ui.app)) as tc:
        body = await (await tc.get("/api/status")).json()
        assert body["peer_count"] == 1
        assert body["peers"][0]["host"] == "10.0.0.5"
        assert body["channel_members"] == []
        assert body["agc_enabled"] is True
        assert body["relay_enabled"] is False


def test_set_config_applies_sender_alias_live(tmp_path):
    asyncio.run(_set_config_applies_sender_alias_live(tmp_path))


async def _set_config_applies_sender_alias_live(tmp_path):
    config_path = tmp_path / "outpost.yaml"
    config_path.write_text("mesh_key: x\n")

    client = _make_client(config_path=str(config_path))
    ui = WebUI(client)
    async with TestClient(TestServer(ui.app)) as tc:
        resp = await tc.post("/api/config", json={"sender_alias": "Basecamp"})
        assert resp.status == 200
        assert client.sender_alias == "Basecamp"

        status = await (await tc.get("/api/status")).json()
        assert status["sender_alias"] == "Basecamp"


def test_get_config_lists_editable_fields_with_live_flag_and_save_capability():
    asyncio.run(_get_config_lists_editable_fields_with_live_flag_and_save_capability())


async def _get_config_lists_editable_fields_with_live_flag_and_save_capability():
    ui = WebUI(_make_client())
    async with TestClient(TestServer(ui.app)) as tc:
        body = await (await tc.get("/api/config")).json()
        assert body["can_save"] is False  # no config_path given
        assert body["fields"]["enable_agc"]["live"] is True
        assert body["fields"]["bitrate"]["live"] is False
        assert body["fields"]["bitrate"]["value"] == 16000


def test_set_config_requires_a_config_path():
    asyncio.run(_set_config_requires_a_config_path())


async def _set_config_requires_a_config_path():
    ui = WebUI(_make_client())
    async with TestClient(TestServer(ui.app)) as tc:
        resp = await tc.post("/api/config", json={"enable_agc": False})
        assert resp.status == 400


def test_set_config_rejects_unknown_field(tmp_path):
    asyncio.run(_set_config_rejects_unknown_field(tmp_path))


async def _set_config_rejects_unknown_field(tmp_path):
    config_path = tmp_path / "outpost.yaml"
    config_path.write_text("mesh_key: x\n")
    ui = WebUI(_make_client(config_path=str(config_path)))
    async with TestClient(TestServer(ui.app)) as tc:
        resp = await tc.post("/api/config", json={"mesh_key": "nope"})
        assert resp.status == 400


def test_set_config_rejects_invalid_value(tmp_path):
    asyncio.run(_set_config_rejects_invalid_value(tmp_path))


async def _set_config_rejects_invalid_value(tmp_path):
    config_path = tmp_path / "outpost.yaml"
    config_path.write_text("mesh_key: x\n")
    ui = WebUI(_make_client(config_path=str(config_path)))
    async with TestClient(TestServer(ui.app)) as tc:
        resp = await tc.post("/api/config", json={"bitrate": -5})
        assert resp.status == 400


def test_set_config_persists_to_yaml_and_applies_live_fields(tmp_path):
    asyncio.run(_set_config_persists_to_yaml_and_applies_live_fields(tmp_path))


async def _set_config_persists_to_yaml_and_applies_live_fields(tmp_path):
    config_path = tmp_path / "outpost.yaml"
    config_path.write_text("mesh_key: x\nbitrate: 16000\n")

    client = _make_client(config_path=str(config_path))
    ui = WebUI(client)
    async with TestClient(TestServer(ui.app)) as tc:
        resp = await tc.post("/api/config", json={"enable_agc": False, "agc_target_rms": 3000, "bitrate": 8000})
        assert resp.status == 200
        body = await resp.json()
        assert set(body["saved"]) == {"enable_agc", "agc_target_rms", "bitrate"}
        assert set(body["applied_live"]) == {"enable_agc", "agc_target_rms"}
        assert body["restart_required"] == ["bitrate"]

    assert client.agc_enabled is False
    assert client.agc.target_rms == 3000

    saved_raw = yaml.safe_load(config_path.read_text())
    assert saved_raw["bitrate"] == 8000
    assert saved_raw["mesh_key"] == "x"  # pre-existing, untouched key preserved


def test_get_config_reflects_values_saved_in_an_earlier_request(tmp_path):
    asyncio.run(_get_config_reflects_values_saved_in_an_earlier_request(tmp_path))


async def _get_config_reflects_values_saved_in_an_earlier_request(tmp_path):
    """Regression test: OutpostConfig is frozen, so a save used to update
    only client.agc_enabled (etc.) and the YAML file, never client.config
    itself - leaving the Settings page's next GET /api/config showing the
    pre-save value, which looked like the save hadn't taken effect."""
    config_path = tmp_path / "outpost.yaml"
    config_path.write_text("mesh_key: x\nbitrate: 16000\n")

    client = _make_client(config_path=str(config_path))
    ui = WebUI(client)
    async with TestClient(TestServer(ui.app)) as tc:
        await tc.post("/api/config", json={"enable_agc": False, "bitrate": 8000})
        body = await (await tc.get("/api/config")).json()
        assert body["fields"]["enable_agc"]["value"] is False
        assert body["fields"]["bitrate"]["value"] == 8000


def _write_two_channel_config(config_path, ops_key, tac_key) -> None:
    import base64
    config_path.write_text(yaml.safe_dump({
        "mesh_key": "x",
        "channels": [
            {"id": 1, "name": "ops", "key": base64.b64encode(ops_key).decode()},
            {"id": 2, "name": "tac", "key": base64.b64encode(tac_key).decode()},
        ],
    }))


def test_get_channels_lists_id_and_name_only():
    asyncio.run(_get_channels_lists_id_and_name_only())


async def _get_channels_lists_id_and_name_only():
    ui = WebUI(_make_client())
    async with TestClient(TestServer(ui.app)) as tc:
        body = await (await tc.get("/api/channels")).json()
        assert body["can_save"] is False
        ids = {(c["id"], c["name"]) for c in body["channels"]}
        assert ids == {(1, "ops"), (2, "tac")}
        assert all("key" not in c for c in body["channels"])


def test_create_channel_requires_a_config_path():
    asyncio.run(_create_channel_requires_a_config_path())


async def _create_channel_requires_a_config_path():
    ui = WebUI(_make_client())
    async with TestClient(TestServer(ui.app)) as tc:
        resp = await tc.post("/api/channels", json={"name": "new"})
        assert resp.status == 400


def test_create_channel_persists_and_applies_live(tmp_path):
    asyncio.run(_create_channel_persists_and_applies_live(tmp_path))


async def _create_channel_persists_and_applies_live(tmp_path):
    config_path = tmp_path / "outpost.yaml"
    ops_key, tac_key = generate_channel_key(), generate_channel_key()
    _write_two_channel_config(config_path, ops_key, tac_key)

    client = _make_client(config_path=str(config_path))
    ui = WebUI(client)
    async with TestClient(TestServer(ui.app)) as tc:
        resp = await tc.post("/api/channels", json={"name": "emergency"})
        assert resp.status == 200
        body = await resp.json()
        assert body["id"] == 3
        assert body["name"] == "emergency"
        assert "key" in body

    # Applied live - no restart needed.
    assert 3 in client.config.channels
    assert client.config.channels[3].name == "emergency"
    assert 3 in client.ciphers
    client.set_active_channel("emergency")  # doesn't raise

    raw = yaml.safe_load(config_path.read_text())
    assert [c["name"] for c in raw["channels"]] == ["ops", "tac", "emergency"]


def test_create_channel_rejects_duplicate_name(tmp_path):
    asyncio.run(_create_channel_rejects_duplicate_name(tmp_path))


async def _create_channel_rejects_duplicate_name(tmp_path):
    config_path = tmp_path / "outpost.yaml"
    ops_key, tac_key = generate_channel_key(), generate_channel_key()
    _write_two_channel_config(config_path, ops_key, tac_key)
    ui = WebUI(_make_client(config_path=str(config_path)))
    async with TestClient(TestServer(ui.app)) as tc:
        resp = await tc.post("/api/channels", json={"name": "ops"})
        assert resp.status == 400


def test_create_channel_rejects_malformed_key(tmp_path):
    asyncio.run(_create_channel_rejects_malformed_key(tmp_path))


async def _create_channel_rejects_malformed_key(tmp_path):
    config_path = tmp_path / "outpost.yaml"
    ops_key, tac_key = generate_channel_key(), generate_channel_key()
    _write_two_channel_config(config_path, ops_key, tac_key)
    ui = WebUI(_make_client(config_path=str(config_path)))
    async with TestClient(TestServer(ui.app)) as tc:
        resp = await tc.post("/api/channels", json={"name": "short", "key": "dG9vc2hvcnQ="})
        assert resp.status == 400


def test_rename_channel_persists_and_applies_live(tmp_path):
    asyncio.run(_rename_channel_persists_and_applies_live(tmp_path))


async def _rename_channel_persists_and_applies_live(tmp_path):
    config_path = tmp_path / "outpost.yaml"
    ops_key, tac_key = generate_channel_key(), generate_channel_key()
    _write_two_channel_config(config_path, ops_key, tac_key)

    client = _make_client(config_path=str(config_path))  # active channel is "ops" (id 1)
    ui = WebUI(client)
    async with TestClient(TestServer(ui.app)) as tc:
        resp = await tc.post("/api/channels/1/rename", json={"name": "primary"})
        assert resp.status == 200

    assert client.config.channels[1].name == "primary"
    assert client.active_channel.name == "primary"  # active channel reference updated too

    raw = yaml.safe_load(config_path.read_text())
    assert raw["channels"][0]["name"] == "primary"


def test_rename_channel_rejects_unknown_id(tmp_path):
    asyncio.run(_rename_channel_rejects_unknown_id(tmp_path))


async def _rename_channel_rejects_unknown_id(tmp_path):
    config_path = tmp_path / "outpost.yaml"
    ops_key, tac_key = generate_channel_key(), generate_channel_key()
    _write_two_channel_config(config_path, ops_key, tac_key)
    ui = WebUI(_make_client(config_path=str(config_path)))
    async with TestClient(TestServer(ui.app)) as tc:
        resp = await tc.post("/api/channels/99/rename", json={"name": "nope"})
        assert resp.status == 404


def test_rename_channel_rejects_duplicate_name(tmp_path):
    asyncio.run(_rename_channel_rejects_duplicate_name(tmp_path))


async def _rename_channel_rejects_duplicate_name(tmp_path):
    config_path = tmp_path / "outpost.yaml"
    ops_key, tac_key = generate_channel_key(), generate_channel_key()
    _write_two_channel_config(config_path, ops_key, tac_key)
    ui = WebUI(_make_client(config_path=str(config_path)))
    async with TestClient(TestServer(ui.app)) as tc:
        resp = await tc.post("/api/channels/1/rename", json={"name": "tac"})
        assert resp.status == 400


def test_rotate_channel_key_persists_and_applies_live(tmp_path):
    asyncio.run(_rotate_channel_key_persists_and_applies_live(tmp_path))


async def _rotate_channel_key_persists_and_applies_live(tmp_path):
    import base64
    config_path = tmp_path / "outpost.yaml"
    ops_key, tac_key = generate_channel_key(), generate_channel_key()
    _write_two_channel_config(config_path, ops_key, tac_key)

    client = _make_client(config_path=str(config_path))
    old_cipher = client.ciphers[1]
    ui = WebUI(client)
    async with TestClient(TestServer(ui.app)) as tc:
        resp = await tc.post("/api/channels/1/rotate_key")
        assert resp.status == 200
        body = await resp.json()
        new_key = base64.b64decode(body["key"])
        assert new_key != ops_key

    assert client.config.channels[1].key == new_key
    assert client.ciphers[1] is not old_cipher

    raw = yaml.safe_load(config_path.read_text())
    assert raw["channels"][0]["key"] == body["key"]


def test_channels_page_serves_html():
    asyncio.run(_channels_page_serves_html())


async def _channels_page_serves_html():
    ui = WebUI(_make_client())
    async with TestClient(TestServer(ui.app)) as tc:
        resp = await tc.get("/channels")
        assert resp.status == 200
        assert "text/html" in resp.content_type


def test_restart_sets_the_clients_restart_event():
    asyncio.run(_restart_sets_the_clients_restart_event())


async def _restart_sets_the_clients_restart_event():
    client = _make_client()
    ui = WebUI(client)
    async with TestClient(TestServer(ui.app)) as tc:
        assert not client.restart_requested.is_set()
        resp = await tc.post("/api/restart")
        assert resp.status == 200
        assert (await resp.json())["restarting"] is True
    assert client.restart_requested.is_set()


def test_restart_requires_auth_when_configured():
    asyncio.run(_restart_requires_auth_when_configured())


async def _restart_requires_auth_when_configured():
    client = _make_client()
    ui = WebUI(client, token="secret")
    async with TestClient(TestServer(ui.app)) as tc:
        unauthenticated = await tc.post("/api/restart")
        assert unauthenticated.status == 401
        assert not client.restart_requested.is_set()

        authenticated = await tc.post("/api/restart", headers={"Authorization": "Bearer secret"})
        assert authenticated.status == 200
    assert client.restart_requested.is_set()


def test_get_peers_lists_configured_static_peers():
    asyncio.run(_get_peers_lists_configured_static_peers())


async def _get_peers_lists_configured_static_peers():
    from outpost.config import Peer
    ui = WebUI(_make_client(peers=[Peer(host="10.0.0.5", port=5555)]))
    async with TestClient(TestServer(ui.app)) as tc:
        body = await (await tc.get("/api/peers")).json()
        assert body["can_save"] is False
        assert body["peers"] == [{"host": "10.0.0.5", "port": 5555}]


def test_add_peer_requires_a_config_path():
    asyncio.run(_add_peer_requires_a_config_path())


async def _add_peer_requires_a_config_path():
    ui = WebUI(_make_client())
    async with TestClient(TestServer(ui.app)) as tc:
        resp = await tc.post("/api/peers", json={"host": "10.0.0.5", "port": 5555})
        assert resp.status == 400


def test_add_peer_persists_and_applies_live(tmp_path):
    asyncio.run(_add_peer_persists_and_applies_live(tmp_path))


async def _add_peer_persists_and_applies_live(tmp_path):
    config_path = tmp_path / "outpost.yaml"
    ops_key, tac_key = generate_channel_key(), generate_channel_key()
    _write_two_channel_config(config_path, ops_key, tac_key)

    client = _make_client(config_path=str(config_path))
    ui = WebUI(client)
    async with TestClient(TestServer(ui.app)) as tc:
        resp = await tc.post("/api/peers", json={"host": "10.0.0.5", "port": 5555})
        assert resp.status == 200
        assert await resp.json() == {"host": "10.0.0.5", "port": 5555}

    # Applied live - no restart needed.
    assert any(p.host == "10.0.0.5" and p.port == 5555 for p in client.config.peers)
    assert any(d["host"] == "10.0.0.5" and d["port"] == 5555 and d["static"]
               for d in client.transport.peer_details())

    raw = yaml.safe_load(config_path.read_text())
    assert raw["peers"] == [{"host": "10.0.0.5", "port": 5555}]


def test_add_peer_rejects_duplicate(tmp_path):
    asyncio.run(_add_peer_rejects_duplicate(tmp_path))


async def _add_peer_rejects_duplicate(tmp_path):
    from outpost.config import Peer
    config_path = tmp_path / "outpost.yaml"
    ops_key, tac_key = generate_channel_key(), generate_channel_key()
    _write_two_channel_config(config_path, ops_key, tac_key)
    client = _make_client(config_path=str(config_path), peers=[Peer(host="10.0.0.5", port=5555)])
    ui = WebUI(client)
    async with TestClient(TestServer(ui.app)) as tc:
        resp = await tc.post("/api/peers", json={"host": "10.0.0.5", "port": 5555})
        assert resp.status == 400


def test_add_peer_rejects_invalid_port(tmp_path):
    asyncio.run(_add_peer_rejects_invalid_port(tmp_path))


async def _add_peer_rejects_invalid_port(tmp_path):
    config_path = tmp_path / "outpost.yaml"
    ops_key, tac_key = generate_channel_key(), generate_channel_key()
    _write_two_channel_config(config_path, ops_key, tac_key)
    ui = WebUI(_make_client(config_path=str(config_path)))
    async with TestClient(TestServer(ui.app)) as tc:
        resp = await tc.post("/api/peers", json={"host": "10.0.0.5", "port": 99999})
        assert resp.status == 400


def test_remove_peer_persists_and_applies_live(tmp_path):
    asyncio.run(_remove_peer_persists_and_applies_live(tmp_path))


async def _remove_peer_persists_and_applies_live(tmp_path):
    from outpost.config import Peer
    config_path = tmp_path / "outpost.yaml"
    ops_key, tac_key = generate_channel_key(), generate_channel_key()
    _write_two_channel_config(config_path, ops_key, tac_key)
    raw = yaml.safe_load(config_path.read_text())
    raw["peers"] = [{"host": "10.0.0.5", "port": 5555}]
    config_path.write_text(yaml.safe_dump(raw))

    client = _make_client(config_path=str(config_path), peers=[Peer(host="10.0.0.5", port=5555)])
    ui = WebUI(client)
    async with TestClient(TestServer(ui.app)) as tc:
        resp = await tc.post("/api/peers/remove", json={"host": "10.0.0.5", "port": 5555})
        assert resp.status == 200

    assert client.config.peers == []
    assert client.transport.peer_details() == []

    raw = yaml.safe_load(config_path.read_text())
    assert raw["peers"] == []


def test_remove_peer_rejects_unknown_address(tmp_path):
    asyncio.run(_remove_peer_rejects_unknown_address(tmp_path))


async def _remove_peer_rejects_unknown_address(tmp_path):
    config_path = tmp_path / "outpost.yaml"
    ops_key, tac_key = generate_channel_key(), generate_channel_key()
    _write_two_channel_config(config_path, ops_key, tac_key)
    ui = WebUI(_make_client(config_path=str(config_path)))
    async with TestClient(TestServer(ui.app)) as tc:
        resp = await tc.post("/api/peers/remove", json={"host": "10.0.0.5", "port": 5555})
        assert resp.status == 404


def test_peers_page_serves_html():
    asyncio.run(_peers_page_serves_html())


async def _peers_page_serves_html():
    ui = WebUI(_make_client())
    async with TestClient(TestServer(ui.app)) as tc:
        resp = await tc.get("/peers")
        assert resp.status == 200
        assert "text/html" in resp.content_type


def test_export_config_requires_a_config_path():
    asyncio.run(_export_config_requires_a_config_path())


async def _export_config_requires_a_config_path():
    ui = WebUI(_make_client())
    async with TestClient(TestServer(ui.app)) as tc:
        resp = await tc.get("/api/config/export")
        assert resp.status == 400


def test_export_config_returns_file_contents_verbatim(tmp_path):
    asyncio.run(_export_config_returns_file_contents_verbatim(tmp_path))


async def _export_config_returns_file_contents_verbatim(tmp_path):
    config_path = tmp_path / "outpost.yaml"
    config_path.write_text("mesh_key: x\n# a comment\nbitrate: 8000\n")
    ui = WebUI(_make_client(config_path=str(config_path)))
    async with TestClient(TestServer(ui.app)) as tc:
        resp = await tc.get("/api/config/export")
        assert resp.status == 200
        assert await resp.text() == config_path.read_text()
        assert "attachment" in resp.headers["Content-Disposition"]


def test_import_config_requires_a_config_path():
    asyncio.run(_import_config_requires_a_config_path())


async def _import_config_requires_a_config_path():
    ui = WebUI(_make_client())
    async with TestClient(TestServer(ui.app)) as tc:
        resp = await tc.post("/api/config/import", data="mesh_key: x\nchannels: []\n")
        assert resp.status == 400


def test_import_config_rejects_invalid_yaml(tmp_path):
    asyncio.run(_import_config_rejects_invalid_yaml(tmp_path))


async def _import_config_rejects_invalid_yaml(tmp_path):
    config_path = tmp_path / "outpost.yaml"
    config_path.write_text("mesh_key: x\nchannels: []\n")
    original = config_path.read_text()
    ui = WebUI(_make_client(config_path=str(config_path)))
    async with TestClient(TestServer(ui.app)) as tc:
        resp = await tc.post("/api/config/import", data="not a valid config at all\n")
        assert resp.status == 400
    assert config_path.read_text() == original  # rejected, file untouched


def test_import_config_overwrites_file_and_requires_restart(tmp_path):
    asyncio.run(_import_config_overwrites_file_and_requires_restart(tmp_path))


async def _import_config_overwrites_file_and_requires_restart(tmp_path):
    config_path = tmp_path / "outpost.yaml"
    config_path.write_text("mesh_key: x\nchannels: []\n")

    new_mesh_key = base64.b64encode(generate_channel_key()).decode()
    new_channel_key = base64.b64encode(generate_channel_key()).decode()
    new_text = (
        f'mesh_key: "{new_mesh_key}"\n'
        "channels:\n"
        f'  - id: 1\n    name: imported\n    key: "{new_channel_key}"\n'
    )

    ui = WebUI(_make_client(config_path=str(config_path)))
    async with TestClient(TestServer(ui.app)) as tc:
        resp = await tc.post("/api/config/import", data=new_text)
        assert resp.status == 200
        body = await resp.json()
        assert body == {"imported": True, "restart_required": True}

    assert config_path.read_text() == new_text

