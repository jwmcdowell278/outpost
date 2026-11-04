"""Bluetooth headset audio (outpost/bt_audio.py) and the Bluetooth page's
audio API. pactl/wpctl are faked with JSON shaped like PipeWire's - nothing
touches real audio devices."""
from __future__ import annotations
import asyncio
import json

from aiohttp.test_utils import TestClient, TestServer

from outpost import bt_audio
from outpost.bt_audio import HeadsetAudio, HeadsetAudioError, parse_audio, profile_label
from outpost.bluetooth import BluetoothManager
from outpost.client import OutpostClient
from outpost.config import OutpostConfig, Channel
from outpost.crypto import generate_channel_key
from outpost.webui import WebUI

from test_bluetooth import FakeBluetoothctl, INRICO

ADDR = INRICO  # the paired device in FakeBluetoothctl
CARD = "bluez_card." + ADDR.replace(":", "_")


class FakePipeWire:
    """Just enough pactl/wpctl: one headset card whose sink/source depend on
    the active profile, defaults, volume, and the autoswitch setting."""

    def __init__(self, profile="a2dp-sink", autoswitch=True, wpctl_settings=True, handsfree=True):
        self.handsfree = handsfree  # False: connected without its hands-free mode
        self.profile = profile
        self.autoswitch = autoswitch
        self.wpctl_settings = wpctl_settings
        self.default_sink = "alsa_output.platform-bcm2835_audio.stereo-fallback"
        self.default_source = "alsa_output.platform-bcm2835_audio.stereo-fallback.monitor"
        self.volume = 40
        self.calls: list[list[str]] = []

    def _bt_props(self):
        return {"device.api": "bluez5", "api.bluez5.address": ADDR}

    def cards(self):
        return [
            {"name": "alsa_card.platform-bcm2835_audio", "active_profile": "output:stereo-fallback",
             "properties": {"device.api": "alsa"}, "profiles": {"output:stereo-fallback": {"description": "Analog"}}},
            {"name": CARD, "active_profile": self.profile, "properties": self._bt_props(), "profiles": {
                "off": {"description": "Off", "available": True},
                "a2dp-sink": {"description": "High Fidelity Playback (A2DP Sink, codec SBC)", "available": True},
                **({"headset-head-unit-cvsd": {"description": "Headset Head Unit (HSP/HFP, codec CVSD)", "available": True},
                    "headset-head-unit": {"description": "Headset Head Unit (HSP/HFP, codec mSBC)", "available": True}}
                   if self.handsfree else {}),
            }},
        ]

    def sinks(self):
        out = [{"name": "alsa_output.platform-bcm2835_audio.stereo-fallback", "properties": {"device.api": "alsa"},
                "volume": {"front-left": {"value_percent": "40%"}}}]
        if self.profile != "off":
            out.append({"name": f"bluez_output.{ADDR.replace(':', '_')}.1", "properties": self._bt_props(),
                        "volume": {"mono": {"value": 0, "value_percent": f"{self.volume}%"}}})
        return out

    def sources(self):
        out = [{"name": f"bluez_output.{ADDR.replace(':', '_')}.1.monitor", "properties": self._bt_props()}]
        if self.profile.startswith("headset-head-unit"):
            out.append({"name": f"bluez_input.{ADDR.replace(':', '_')}.0", "properties": self._bt_props()})
        return out

    async def __call__(self, args, timeout):
        self.calls.append(list(args))
        if args[0] == "wpctl":
            if not self.wpctl_settings:
                return 1, "Unknown command 'settings'\n"
            if "--save" in args:
                self.autoswitch = args[-1] == "true"
                return 0, ""
            return 0, f"Value: {'true' if self.autoswitch else 'false'}\n"
        assert args[0] == "pactl", args
        if args[1] == "--format=json":
            return 0, json.dumps(getattr(self, args[-1])())
        if args[1] == "get-default-sink":
            return 0, self.default_sink + "\n"
        if args[1] == "get-default-source":
            return 0, self.default_source + "\n"
        if args[1] == "set-card-profile":
            assert args[2] == CARD
            self.profile = args[3]
            return 0, ""
        if args[1] == "set-default-sink":
            self.default_sink = args[2]
            return 0, ""
        if args[1] == "set-default-source":
            self.default_source = args[2]
            return 0, ""
        if args[1] == "set-sink-volume":
            self.volume = int(args[3].rstrip("%"))
            return 0, ""
        raise AssertionError(args)


def _audio(fake, routed=True) -> HeadsetAudio:
    return HeadsetAudio(fake, alsa_routed=lambda: routed)


def _parse(fake):
    return parse_audio(fake.cards(), fake.sinks(), fake.sources(), fake.default_sink, fake.default_source)


def test_profile_labels():
    assert "8 kHz" in profile_label("headset-head-unit-cvsd")
    assert "16 kHz" in profile_label("headset-head-unit", "Headset Head Unit (HSP/HFP, codec mSBC)")
    assert "no mic" in profile_label("a2dp-sink")
    assert profile_label("off") == "Off"


def test_parse_playback_only_headset():
    info = _parse(FakePipeWire())[ADDR]
    assert info["card"] == CARD and info["profile"] == "a2dp-sink"
    assert not info["handsfree"] and info["has_handsfree"]
    assert info["source"] is None  # the sink's monitor isn't a mic
    assert info["volume"] == 40
    assert not info["outpost_headset"]
    # Hands-free modes listed first, the most reliable one on top; off last.
    assert [p["name"] for p in info["profiles"]][0] == "headset-head-unit-cvsd"
    assert info["profiles"][-1]["name"] == "off"


def test_only_bluetooth_cards_are_listed():
    assert list(_parse(FakePipeWire())) == [ADDR]


def test_setup_does_everything_a_headset_needs():
    async def run():
        fake = FakePipeWire()
        message = await _audio(fake).setup(ADDR.lower())
        assert "8 kHz" in message
        assert fake.profile == "headset-head-unit-cvsd"
        assert fake.autoswitch is False
        assert fake.default_sink.startswith("bluez_output.") and fake.default_source.startswith("bluez_input.")
        assert fake.volume == 100
        info = (await _audio(fake).devices())[ADDR]
        assert info["outpost_headset"]
    asyncio.run(run())


def test_setup_leaves_autoswitch_alone_on_wireplumber_04():
    async def run():
        fake = FakePipeWire(wpctl_settings=False)
        await _audio(fake).setup(ADDR)
        assert not any("--save" in c for c in fake.calls)
        assert fake.profile == "headset-head-unit-cvsd"
    asyncio.run(run())


def test_profile_use_and_volume():
    async def run():
        fake = FakePipeWire(profile="headset-head-unit-cvsd", autoswitch=False)
        audio = _audio(fake)
        assert "16 kHz" in await audio.set_profile(ADDR, "headset-head-unit")
        assert fake.profile == "headset-head-unit"
        assert "mic and speaker" in await audio.use_for_outpost(ADDR)
        assert fake.default_source.startswith("bluez_input.")
        assert await audio.set_volume(ADDR, 70) == "volume 70%"
        assert fake.volume == 70
        for bad in (101, -1, "50", True, 5.5):
            try:
                await audio.set_volume(ADDR, bad)
            except HeadsetAudioError:
                continue
            raise AssertionError(bad)
    asyncio.run(run())


def test_bad_requests():
    async def run():
        audio = _audio(FakePipeWire())
        for call in (audio.setup("nope"), audio.set_profile(ADDR, "made-up"),
                     audio.setup("AA:AA:AA:AA:AA:AA")):
            try:
                await call
            except HeadsetAudioError:
                continue
            raise AssertionError("expected an error")
    asyncio.run(run())


def test_unavailable_off_linux(monkeypatch):
    monkeypatch.setattr(bt_audio.sys, "platform", "darwin")
    assert "Linux" in HeadsetAudio().unavailable_reason()


def _webui(bluez, pipewire, routed=True, input_device=None) -> WebUI:
    config = OutpostConfig(sender_id=1, listen_port=0, mesh_key=generate_channel_key(),
                           channels={1: Channel(id=1, name="ops", key=generate_channel_key())},
                           enable_mdns=False, enable_pex=False, input_device=input_device,
                           output_device=input_device)
    ui = WebUI(OutpostClient(config, "ops"))
    ui.bluetooth = BluetoothManager(bluez)
    ui.headset_audio = _audio(pipewire, routed)
    return ui


def test_web_ui_audio_flow():
    async def run():
        pw = FakePipeWire()
        async with TestClient(TestServer(_webui(FakeBluetoothctl(), pw).app)) as tc:
            state = await (await tc.get("/api/bluetooth")).json()
            assert state["audio"][ADDR]["profile"] == "a2dp-sink"
            assert any("automatically" in n for n in state["audio_notes"])

            resp = await tc.post("/api/bluetooth/audio", json={"address": ADDR, "action": "setup"})
            assert resp.status == 200, await resp.text()
            state = await (await tc.get("/api/bluetooth")).json()
            assert state["audio"][ADDR]["outpost_headset"]
            assert state["audio_notes"] == []

            resp = await tc.post("/api/bluetooth/audio", json={"address": ADDR, "action": "volume", "volume": 55})
            assert resp.status == 200 and pw.volume == 55
            resp = await tc.post("/api/bluetooth/audio", json={"address": ADDR, "action": "explode"})
            assert resp.status == 400
            resp = await tc.post("/api/bluetooth/audio", json={"address": ADDR, "action": "profile", "profile": "nope"})
            assert resp.status == 400
    asyncio.run(run())


def test_web_ui_warns_when_default_device_skips_pipewire():
    async def run():
        async with TestClient(TestServer(_webui(FakeBluetoothctl(), FakePipeWire(autoswitch=False), routed=False).app)) as tc:
            state = await (await tc.get("/api/bluetooth")).json()
            assert any("pipewire-alsa" in n for n in state["audio_notes"])
        # Not when Outpost is pointed at specific devices.
        ui = _webui(FakeBluetoothctl(), FakePipeWire(autoswitch=False), routed=False, input_device="pulse")
        async with TestClient(TestServer(ui.app)) as tc:
            state = await (await tc.get("/api/bluetooth")).json()
            assert state["audio_notes"] == []
    asyncio.run(run())


def test_pairing_a_headset_sets_it_up():
    async def run():
        pw = FakePipeWire()
        async with TestClient(TestServer(_webui(FakeBluetoothctl(), pw).app)) as tc:
            resp = await tc.post("/api/bluetooth/device", json={"address": ADDR, "action": "pair"})
            message = (await resp.json())["message"]
            assert message.startswith("paired and connected - set up as Outpost's headset")
            assert pw.profile == "headset-head-unit-cvsd" and pw.volume == 100
    asyncio.run(run())


def test_bluetooth_page_has_audio_controls():
    async def run():
        async with TestClient(TestServer(_webui(FakeBluetoothctl(), FakePipeWire()).app)) as tc:
            page = await (await tc.get("/bluetooth")).text()
            assert "Set up as headset" in page and 'data-audio="profile"' in page
    asyncio.run(run())


def test_setup_reconnects_a_headset_that_came_up_without_handsfree():
    async def run():
        pw = FakePipeWire(handsfree=False)
        bluez = FakeBluetoothctl()
        real = bluez.__call__

        async def bluetoothctl(args, timeout):
            if "connect" in args:  # the Pi connecting out brings hands-free back
                pw.handsfree = True
            return await real(args, timeout)
        ui = _webui(bluetoothctl, pw)
        async with TestClient(TestServer(ui.app)) as tc:
            resp = await tc.post("/api/bluetooth/audio", json={"address": ADDR, "action": "setup"})
            assert resp.status == 200, await resp.text()
            assert pw.profile == "headset-head-unit-cvsd"
            assert ["disconnect", ADDR] in bluez.calls and ["connect", ADDR] in bluez.calls

            pw.handsfree = False
            bluez.calls.clear()
            ui.HANDSFREE_RECONNECT_WAIT_S = 0.2
            async def never_handsfree(args, timeout):
                return await real(args, timeout)
            ui.bluetooth = BluetoothManager(never_handsfree)
            resp = await tc.post("/api/bluetooth/audio", json={"address": ADDR, "action": "setup"})
            assert resp.status == 400 and "switch it off and on" in await resp.text()
    asyncio.run(run())
