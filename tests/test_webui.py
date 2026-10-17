"""Tests for the optional local web UI (outpost/webui.py): status reporting,
channel switching, PTT control, and bearer-token auth - using aiohttp's test
client directly against the WebUI's app (no real OutpostClient.start(), so no
real sockets/mic/speaker are touched).

Project convention: no pytest-asyncio plugin installed, so each async body
is driven via asyncio.run() from a plain sync test function (see
tests/test_channel_scoping.py for the same pattern)."""
from __future__ import annotations
import asyncio
import pytest
from aiohttp.test_utils import TestClient, TestServer

from outpost.client import OutpostClient
from outpost.config import OutpostConfig, Channel
from outpost.crypto import generate_channel_key
from outpost.webui import WebUI


def _make_client(**config_overrides) -> OutpostClient:
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
    return OutpostClient(OutpostConfig(**defaults), "ops")


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
