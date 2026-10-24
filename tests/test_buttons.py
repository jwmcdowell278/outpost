"""Hardware button bindings (outpost/buttons.py): config, actions, the
"press a button" setup flow, the evdev reader thread, and the web UI's
Buttons page API. evdev is faked - no real input devices are touched."""
from __future__ import annotations
import asyncio
import base64
import os
import threading
import time
from types import SimpleNamespace

import pytest
import yaml
from aiohttp.test_utils import TestClient, TestServer

from outpost import buttons as buttons_mod
from outpost.buttons import ButtonController, EvdevInput, KEY_DOWN, KEY_UP, KEY_REPEAT
from outpost.client import OutpostClient
from outpost.config import (OutpostConfig, Channel, ButtonBinding, load_button_bindings,
                            load_config, validate_config_yaml)
from outpost.crypto import generate_channel_key
from outpost.webui import WebUI

INRICO = "INRICO B01"


def _b64() -> str:
    return base64.b64encode(generate_channel_key()).decode()


def _client(tmp_path=None, bindings=(), channels=("ops", "tac", "logistics")) -> OutpostClient:
    config = OutpostConfig(
        sender_id=1, listen_port=0, mesh_key=generate_channel_key(),
        channels={i + 1: Channel(id=i + 1, name=n, key=generate_channel_key()) for i, n in enumerate(channels)},
        enable_mdns=False, enable_pex=False, enable_web_ui=True, button_bindings=list(bindings),
    )
    path = None
    if tmp_path is not None:
        path = str(tmp_path / "outpost.yaml")
        with open(path, "w") as f:
            yaml.safe_dump({"mesh_key": _b64()}, f)
    client = OutpostClient(config, channels[0], config_path=path)
    client.buttons._input_factory = FakeInput
    return client


class FakeInput:
    def __init__(self, on_event):
        self.on_event = on_event
        self.unreadable = 0

    def start(self): pass
    def stop(self): pass
    def devices(self): return [INRICO]


async def _started(client):
    client.buttons.start()
    return client.buttons


# --- config -------------------------------------------------------------------

def test_bindings_load_from_config(tmp_path):
    path = tmp_path / "outpost.yaml"
    path.write_text(f"mesh_key: {_b64()}\nsender_id: 1\nchannels:\n  - {{id: 1, name: ops, key: {_b64()}}}\n"
                    "button_bindings:\n  - {key: KEY_F13, action: ptt, device: INRICO B01}\n"
                    "  - {key: KEY_VOLUMEUP, action: volume_up}\n")
    config = load_config(str(path))
    assert config.button_bindings == [ButtonBinding("KEY_F13", "ptt", INRICO), ButtonBinding("KEY_VOLUMEUP", "volume_up")]


@pytest.mark.parametrize("raw", [
    "nope", [{"key": "KEY_A", "action": "launch_missiles"}], [{"action": "ptt"}], ["KEY_A"],
])
def test_bad_bindings_are_rejected(raw):
    with pytest.raises(ValueError):
        load_button_bindings(raw)


def test_imported_config_with_bad_bindings_is_rejected():
    base = f"mesh_key: {_b64()}\nchannels:\n  - {{id: 1, name: ops, key: {_b64()}}}\n"
    validate_config_yaml(base + "button_bindings: [{key: KEY_A, action: ptt}]\n")
    with pytest.raises(ValueError):
        validate_config_yaml(base + "button_bindings: [{key: KEY_A, action: explode}]\n")


# --- actions ------------------------------------------------------------------

def test_ptt_hold_and_toggle():
    async def run():
        client = _client(bindings=[ButtonBinding("KEY_F13", "ptt"), ButtonBinding("KEY_F14", "ptt_toggle")])
        b = await _started(client)
        b.handle_event(INRICO, "KEY_F13", KEY_DOWN)
        assert client._ptt_active
        b.handle_event(INRICO, "KEY_F13", KEY_REPEAT)
        assert client._ptt_active
        b.handle_event(INRICO, "KEY_F13", KEY_UP)
        assert not client._ptt_active
        b.handle_event(INRICO, "KEY_F14", KEY_DOWN)
        b.handle_event(INRICO, "KEY_F14", KEY_UP)
        assert client._ptt_active
        b.handle_event(INRICO, "KEY_F14", KEY_DOWN)
        assert not client._ptt_active
    asyncio.run(run())


def test_device_specific_binding_ignores_other_devices():
    async def run():
        client = _client(bindings=[ButtonBinding("KEY_F13", "ptt", INRICO)])
        b = await _started(client)
        b.handle_event("USB Keyboard", "KEY_F13", KEY_DOWN)
        assert not client._ptt_active
        b.handle_event(INRICO, "KEY_F13", KEY_DOWN)
        assert client._ptt_active
    asyncio.run(run())


def test_channel_up_and_down_wrap_around():
    async def run():
        client = _client(bindings=[ButtonBinding("KEY_NEXTSONG", "channel_up"),
                                   ButtonBinding("KEY_PREVIOUSSONG", "channel_down")])
        b = await _started(client)
        names = []
        for key in ["KEY_NEXTSONG", "KEY_NEXTSONG", "KEY_NEXTSONG", "KEY_PREVIOUSSONG"]:
            b.handle_event(INRICO, key, KEY_DOWN)
            b.handle_event(INRICO, key, KEY_UP)
            names.append(client.active_channel.name)
        assert names == ["tac", "logistics", "ops", "logistics"]
    asyncio.run(run())


def test_volume_buttons_step_clamp_and_save(tmp_path):
    async def run():
        client = _client(tmp_path, bindings=[ButtonBinding("KEY_VOLUMEUP", "volume_up"),
                                             ButtonBinding("KEY_VOLUMEDOWN", "volume_down")])
        b = await _started(client)
        b.handle_event(INRICO, "KEY_VOLUMEDOWN", KEY_DOWN)
        b.handle_event(INRICO, "KEY_VOLUMEDOWN", KEY_REPEAT)  # held: keeps stepping
        assert client.channel_volume(1) == pytest.approx(0.8)
        for _ in range(5):
            b.handle_event(INRICO, "KEY_VOLUMEUP", KEY_DOWN)
        assert client.channel_volume(1) == 1.0
        with open(client.config_path) as f:
            assert yaml.safe_load(f)["channel_volumes"] == {"ops": 1.0}
    asyncio.run(run())


# --- "press a button" setup -------------------------------------------------------

def test_learn_captures_press_without_triggering_it():
    async def run():
        client = _client(bindings=[ButtonBinding("KEY_F13", "ptt")])
        b = await _started(client)
        learning = asyncio.ensure_future(b.learn(timeout=2))
        await asyncio.sleep(0)
        b.handle_event(INRICO, "KEY_F13", KEY_DOWN)
        assert await learning == {"device": INRICO, "key": "KEY_F13"}
        assert not client._ptt_active
        b.handle_event(INRICO, "KEY_F13", KEY_UP)  # release of the captured press: ignored too
        b.handle_event(INRICO, "KEY_F13", KEY_DOWN)  # next press works normally
        assert client._ptt_active
    asyncio.run(run())


def test_learn_times_out():
    async def run():
        b = await _started(_client())
        assert await b.learn(timeout=0.05) is None
    asyncio.run(run())


# --- evdev reader thread ----------------------------------------------------------

class FakeEvdev:
    """Stands in for the evdev module: one device whose events are fed
    through a real pipe, so the reader thread's select() works."""
    ecodes = SimpleNamespace(EV_KEY=1, EV_SYN=0, bytype={1: {183: "KEY_F13", 115: ["KEY_VOLUMEUP", "KEY_ALIAS"]}})

    def __init__(self):
        self.read_fd, self.write_fd = os.pipe()
        os.set_blocking(self.read_fd, False)
        self.pending = []
        self.gone = False
        fake = self

        class InputDevice:
            def __init__(self, path):
                if fake.gone:
                    raise OSError("no such device")
                self.path, self.name, self.fd = path, INRICO, fake.read_fd

            def capabilities(self):
                return {1: [183, 115]}

            def read(self):
                if fake.gone:
                    raise OSError(19, "No such device")
                os.read(fake.read_fd, 100)
                events, fake.pending = fake.pending, []
                return events

            def close(self): pass
        self.InputDevice = InputDevice

    def list_devices(self):
        return [] if self.gone else ["/dev/input/event7"]

    def press(self, code, value):
        self.pending.append(SimpleNamespace(type=1, code=code, value=value))
        os.write(self.write_fd, b"x")


def test_evdev_reader_delivers_key_events_and_survives_disconnect():
    fake = FakeEvdev()
    got = []
    done = threading.Event()

    def on_event(device, key, value):
        got.append((device, key, value))
        if len(got) == 2:
            done.set()

    reader = EvdevInput(on_event, fake)
    reader.start()
    try:
        deadline = time.monotonic() + 2
        while not reader.devices() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert reader.devices() == [INRICO]
        fake.press(183, KEY_DOWN)
        fake.press(115, KEY_UP)
        assert done.wait(2)
        assert got == [(INRICO, "KEY_F13", KEY_DOWN), (INRICO, "KEY_VOLUMEUP", KEY_UP)]

        fake.gone = True  # Bluetooth button out of range
        os.write(fake.write_fd, b"x")
        deadline = time.monotonic() + 2
        while reader.devices() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert reader.devices() == []
    finally:
        reader.stop()


def test_unavailable_off_linux(monkeypatch):
    monkeypatch.setattr(buttons_mod.sys, "platform", "darwin")
    async def run():
        b = ButtonController(SimpleNamespace(), [])
        b.start()
        assert not b.running and "Linux" in b.unavailable_reason
    asyncio.run(run())


# --- web UI -----------------------------------------------------------------------

def test_web_ui_learn_then_save_binding(tmp_path):
    async def run():
        client = _client(tmp_path)
        await _started(client)
        async with TestClient(TestServer(WebUI(client).app)) as tc:
            state = await (await tc.get("/api/buttons")).json()
            assert state["available"] and state["devices"] == [INRICO] and state["bindings"] == []

            learn = asyncio.ensure_future(tc.post("/api/buttons/learn"))
            await asyncio.sleep(0.1)
            client.buttons.handle_event(INRICO, "KEY_F13", KEY_DOWN)
            pressed = await (await learn).json()
            assert pressed == {"device": INRICO, "key": "KEY_F13"}

            resp = await tc.post("/api/buttons/bindings", json={"bindings": [{**pressed, "action": "ptt"}]})
            assert resp.status == 200
            assert (await resp.json())["bindings"] == [{"key": "KEY_F13", "action": "ptt", "device": INRICO}]
            bad = await tc.post("/api/buttons/bindings", json={"bindings": [{"key": "KEY_A", "action": "bad"}]})
            assert bad.status == 400

        client.buttons.handle_event(INRICO, "KEY_F13", KEY_UP)
        client.buttons.handle_event(INRICO, "KEY_F13", KEY_DOWN)
        assert client._ptt_active
        with open(client.config_path) as f:
            assert yaml.safe_load(f)["button_bindings"] == [{"key": "KEY_F13", "action": "ptt", "device": INRICO}]
    asyncio.run(run())


def test_buttons_page_loads():
    async def run():
        async with TestClient(TestServer(WebUI(_client()).app)) as tc:
            resp = await tc.get("/buttons")
            assert resp.status == 200 and "Press a button" in await resp.text()
    asyncio.run(run())


# --- Bluetooth headset (hands-free AT command) buttons ------------------------------

from outpost.buttons import HandsFreeInput


def _monitor_frame(opcode: int, rfcomm: bytes) -> bytes:
    """A Bluetooth monitor frame: opcode/index/len header, then ACL + L2CAP
    headers and the RFCOMM bytes."""
    acl = bytes([0x0c, 0x20]) + len(rfcomm).to_bytes(2, "little") + bytes([0x41, 0x00]) + rfcomm
    return opcode.to_bytes(2, "little") + b"\x00\x00" + len(acl).to_bytes(2, "little") + acl


# Captured from an Inrico B02PTT: PTT press, release, battery report.
TALK_DOWN = bytes.fromhex("51ef23") + b"AT+XEVENT=TALK,1\r" + b"\xeb"
TALK_UP = bytes.fromhex("51ef23") + b"AT+XEVENT=TALK,0\r" + b"\xeb"
BATTERY = bytes.fromhex("51ef2b") + b"AT+IPHONEACCEV=1,1,7\r" + b"\xeb"


def test_handsfree_ptt_commands_become_button_events():
    got = []
    hf = HandsFreeInput(lambda *e: got.append(e))
    hf.handle_frame(_monitor_frame(5, TALK_DOWN))
    hf.handle_frame(_monitor_frame(5, BATTERY))
    hf.handle_frame(_monitor_frame(4, TALK_DOWN))   # outgoing data - not a button
    hf.handle_frame(_monitor_frame(5, TALK_UP))
    hf.handle_frame(_monitor_frame(5, b"AT+CKPD=200\r"))
    hf.handle_frame(b"\x05\x00")                     # truncated
    d = HandsFreeInput.DEVICE
    assert got == [(d, "HFP_TALK", KEY_DOWN), (d, "HFP_TALK", KEY_UP),
                   (d, "HFP_BUTTON", KEY_DOWN), (d, "HFP_BUTTON", KEY_UP)]


def test_handsfree_ptt_drives_push_to_talk():
    class FakeHandsFree(HandsFreeInput):
        def start(self): pass
        def stop(self): pass

    async def run():
        client = _client(bindings=[ButtonBinding("HFP_TALK", "ptt", HandsFreeInput.DEVICE)])
        client.buttons._handsfree_factory = FakeHandsFree
        client.buttons.start()
        assert HandsFreeInput.DEVICE in client.buttons.devices()
        hf = client.buttons._handsfree
        hf.handle_frame(_monitor_frame(5, TALK_DOWN))
        await asyncio.sleep(0.01)  # events hop onto the event loop
        assert client._ptt_active
        hf.handle_frame(_monitor_frame(5, TALK_UP))
        await asyncio.sleep(0.01)
        assert not client._ptt_active
    asyncio.run(run())


def test_handsfree_without_permission_explains_why():
    def denied(on_event):
        class Denied:
            def start(self): raise PermissionError(1, "Operation not permitted")
        return Denied()

    async def run():
        client = _client()
        client.buttons._handsfree_factory = denied
        client.buttons.start()
        assert client.buttons.running  # ordinary buttons still work
        assert "permission" in client.buttons.handsfree_note
        async with TestClient(TestServer(WebUI(client).app)) as tc:
            state = await (await tc.get("/api/buttons")).json()
            assert state["notes"] == [client.buttons.handsfree_note]
    asyncio.run(run())
