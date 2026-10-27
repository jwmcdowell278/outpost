"""Bluetooth management (outpost/bluetooth.py) and the web UI's Bluetooth
page API. bluetoothctl is faked with canned output - nothing touches a real
adapter."""
from __future__ import annotations
import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer

from outpost import bluetooth as bt
from outpost.bluetooth import BluetoothManager, BluetoothError, parse_device_info, parse_fields
from outpost.client import OutpostClient
from outpost.config import OutpostConfig, Channel
from outpost.crypto import generate_channel_key
from outpost.webui import WebUI

INRICO = "12:34:56:78:9A:BC"
HEADSET = "AA:BB:CC:DD:EE:FF"
STRANGER = "11:22:33:44:55:66"

SHOW = """Controller DC:A6:32:01:02:03 (public)
\tName: raspberrypi
\tAlias: raspberrypi
\tPowered: yes
\tDiscoverable: no
\tDiscovering: no
"""

INFO = {
    INRICO: """Device 12:34:56:78:9A:BC (public)
\tName: INRICO B01
\tAlias: INRICO B01
\tIcon: input-keyboard
\tPaired: yes
\tTrusted: yes
\tConnected: yes
\tUUID: Human Interface Device    (00001812-0000-1000-8000-00805f9b34fb)
\tBattery Percentage: 0x0f (15)
""",
    HEADSET: """Device AA:BB:CC:DD:EE:FF (public)
\tName: Headset
\tAlias: Headset
\tPaired: yes
\tTrusted: yes
\tConnected: no
""",
    STRANGER: """Device 11:22:33:44:55:66 (random)
\tName: Somebody's Watch
\tAlias: Somebody's Watch
\tPaired: no
\tConnected: no
\tRSSI: -71
""",
}


class FakeBluetoothctl:
    def __init__(self):
        self.calls: list[list[str]] = []
        self.powered = True
        self.replies: dict[str, str] = {}

    async def __call__(self, args, timeout):
        self.calls.append(list(args))
        cmd = [a for a in args if not a.startswith("--") and a != "NoInputNoOutput"][0]
        if cmd in self.replies:
            return 0, self.replies[cmd]
        if cmd == "show":
            return 0, SHOW.replace("Powered: yes", f"Powered: {'yes' if self.powered else 'no'}")
        if cmd == "devices":
            # bluetoothctl colours its output - make sure that's stripped.
            return 0, "".join(f"\x1b[0;94mDevice {a} {n}\x1b[0m\n" for a, n in
                              [(STRANGER, "Somebody's Watch"), (HEADSET, "Headset"), (INRICO, "INRICO B01")])
        if cmd == "info":
            return 0, INFO[args[-1]]
        if cmd == "power":
            self.powered = args[-1] == "on"
            return 0, f"Changing power {args[-1]} succeeded\n"
        if cmd == "pair":
            return 0, "Attempting to pair with 12:34:56:78:9A:BC\nPairing successful\n"
        if cmd in ("trust", "connect", "disconnect", "remove", "pairable"):
            return 0, f"{cmd} succeeded\n"
        if cmd == "scan":
            await asyncio.sleep(0.05)
            return 0, "Discovery started\n"
        raise AssertionError(args)


def test_parses_battery_status_and_signal():
    d = parse_device_info(INRICO, "", INFO[INRICO])
    assert d == {"address": INRICO, "name": "INRICO B01", "icon": "input-keyboard", "paired": True,
                 "trusted": True, "connected": True, "battery": 15, "rssi": None}
    assert parse_device_info(STRANGER, "", INFO[STRANGER])["rssi"] == -71
    assert parse_device_info(HEADSET, "", INFO[HEADSET])["battery"] is None
    # Change notifications aren't mistaken for fields.
    assert "Connected" not in parse_fields("[CHG] Device 12:34:56:78:9A:BC Connected: yes")


def test_state_lists_paired_devices_first():
    async def run():
        fake = FakeBluetoothctl()
        state = await BluetoothManager(fake).state()
        assert state["adapter"] == "raspberrypi" and state["powered"]
        assert [d["name"] for d in state["devices"]] == ["INRICO B01", "Headset", "Somebody's Watch"]
    asyncio.run(run())


def test_no_adapter():
    async def run():
        fake = FakeBluetoothctl()
        fake.replies["show"] = "No default controller available\n"
        state = await BluetoothManager(fake).state()
        assert state["error"] == "no Bluetooth adapter found" and state["devices"] == []
    asyncio.run(run())


def test_pair_also_trusts_and_connects():
    async def run():
        fake = FakeBluetoothctl()
        msg = await BluetoothManager(fake).device_action(INRICO.lower(), "pair")
        assert msg == "paired and connected"
        assert fake.calls == [["pairable", "on"], ["--agent", "NoInputNoOutput", "pair", INRICO],
                              ["trust", INRICO], ["connect", INRICO]]
    asyncio.run(run())


def test_pair_failure_is_reported():
    async def run():
        fake = FakeBluetoothctl()
        fake.replies["pair"] = "Failed to pair: org.bluez.Error.AuthenticationFailed\n"
        with pytest.raises(BluetoothError, match="AuthenticationFailed"):
            await BluetoothManager(fake).device_action(INRICO, "pair")
        assert ["trust", INRICO] not in fake.calls
    asyncio.run(run())


def test_unpair_connect_disconnect_and_bad_input():
    async def run():
        fake = FakeBluetoothctl()
        m = BluetoothManager(fake)
        assert await m.device_action(INRICO, "unpair") == "unpaired"
        assert await m.device_action(INRICO, "disconnect") == "disconnected"
        assert fake.calls == [["remove", INRICO], ["disconnect", INRICO]]
        with pytest.raises(BluetoothError):
            await m.device_action("12:34; rm -rf /", "pair")
        with pytest.raises(BluetoothError):
            await m.device_action(INRICO, "format")
    asyncio.run(run())


def test_power_blocked_explains_rfkill():
    async def run():
        fake = FakeBluetoothctl()
        fake.replies["power"] = "Failed to set power on: org.bluez.Error.Blocked\n"
        with pytest.raises(BluetoothError, match="rfkill unblock"):
            await BluetoothManager(fake).set_power(True)
    asyncio.run(run())


def test_scan_runs_in_background():
    async def run():
        fake = FakeBluetoothctl()
        m = BluetoothManager(fake)
        m.start_scan(seconds=5)
        assert m.scanning
        await asyncio.sleep(0.1)
        assert ["--timeout", "5", "scan", "on"] in fake.calls
        assert not m.scanning  # the scan process ended
    asyncio.run(run())


def test_unavailable_off_linux(monkeypatch):
    monkeypatch.setattr(bt.sys, "platform", "darwin")
    assert "Linux" in BluetoothManager().unavailable_reason()


def _webui(fake) -> WebUI:
    config = OutpostConfig(sender_id=1, listen_port=0, mesh_key=generate_channel_key(),
                           channels={1: Channel(id=1, name="ops", key=generate_channel_key())},
                           enable_mdns=False, enable_pex=False)
    ui = WebUI(OutpostClient(config, "ops"))
    ui.bluetooth = BluetoothManager(fake)
    return ui


def test_web_ui_bluetooth_flow():
    async def run():
        fake = FakeBluetoothctl()
        async with TestClient(TestServer(_webui(fake).app)) as tc:
            state = await (await tc.get("/api/bluetooth")).json()
            assert state["available"] and state["powered"]
            assert state["devices"][0]["battery"] == 15

            resp = await tc.post("/api/bluetooth/power", json={"on": False})
            assert resp.status == 200
            assert not (await (await tc.get("/api/bluetooth")).json())["powered"]

            resp = await tc.post("/api/bluetooth/device", json={"address": INRICO, "action": "pair"})
            assert (await resp.json())["message"] == "paired and connected"
            resp = await tc.post("/api/bluetooth/device", json={"address": "nope", "action": "pair"})
            assert resp.status == 400
            assert (await tc.get("/bluetooth")).status == 200
    asyncio.run(run())


def test_web_ui_reports_unavailable(monkeypatch):
    monkeypatch.setattr(bt.sys, "platform", "darwin")
    async def run():
        ui = _webui(None)
        ui.bluetooth = BluetoothManager()
        async with TestClient(TestServer(ui.app)) as tc:
            state = await (await tc.get("/api/bluetooth")).json()
            assert state["available"] is False and "Linux" in state["reason"]
            assert (await tc.post("/api/bluetooth/scan")).status == 409
    asyncio.run(run())


def test_reconnect_connects_paired_trusted_devices_that_are_not_connected():
    async def run():
        fake = FakeBluetoothctl()
        m = BluetoothManager(fake)
        # Inrico: connected already. Headset: paired, trusted, disconnected.
        # Stranger: not paired. Only the headset gets connected.
        assert await m.reconnect_once() == ["Headset"]
        assert [c for c in fake.calls if c[0] == "connect"] == [["connect", HEADSET]]
    asyncio.run(run())


def test_reconnect_leaves_devices_disconnected_on_purpose():
    async def run():
        fake = FakeBluetoothctl()
        m = BluetoothManager(fake)
        await m.device_action(HEADSET, "disconnect")
        assert await m.reconnect_once() == []
        await m.device_action(HEADSET, "connect")       # user connects it again...
        fake.calls.clear()
        await m.reconnect_once()                         # ...so it's looked after again
        assert ["connect", HEADSET] in fake.calls
    asyncio.run(run())


def test_reconnect_does_nothing_while_powered_off_or_scanning():
    async def run():
        fake = FakeBluetoothctl()
        fake.powered = False
        m = BluetoothManager(fake)
        assert await m.reconnect_once() == []
        fake.powered = True
        m.start_scan(seconds=5)
        assert await m.reconnect_once() == []
        assert not [c for c in fake.calls if c[0] == "connect"]
    asyncio.run(run())


def test_reconnect_failure_is_quiet():
    async def run():
        fake = FakeBluetoothctl()
        fake.replies["connect"] = "Failed to connect: org.bluez.Error.Failed br-connection-page-timeout\n"
        assert await BluetoothManager(fake).reconnect_once() == []
    asyncio.run(run())
