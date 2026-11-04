"""Bluetooth headset audio for the web UI's Bluetooth page: which mode a
headset is in (hands-free with a mic, or playback only), whether Outpost
uses it for its mic and speaker, and its volume.

Drives PipeWire through `pactl` (pulseaudio-utils) and WirePlumber through
`wpctl`, one command per call. Linux only.

What a headset needs to work with Outpost (README "Raspberry Pi with a
Bluetooth headset"):
  - hands-free mode - playback-only mode (A2DP) has no mic;
  - to be the system's default speaker and mic, which Outpost uses unless
    input_device/output_device are set;
  - WirePlumber 0.5 (Raspberry Pi OS Trixie) not switching it between the
    two modes by itself - on a Pi 3 with a TP-Link UB500 that crashes the
    adapter (`RTL: hw err`), dropping the headset every few seconds;
  - the pipewire-alsa package, so the system default device goes through
    PipeWire at all (without it Outpost plays to the Pi's own audio jack).
"""
from __future__ import annotations
import asyncio
import json
import logging
import os
import re
import shutil
import sys
import time

log = logging.getLogger(__name__)

_COMMAND_TIMEOUT_S = 8.0
_STATE_CACHE_S = 2.0
# How long a headset's speaker/mic take to appear after a mode change.
_PROFILE_SETTLE_S = 6.0
AUTOSWITCH_SETTING = "bluetooth.autoswitch-to-headset-profile"
# Most reliable hands-free mode first: on a Pi 3, 16 kHz (mSBC) can fail one
# way (`corrupted SCO packet` - mic works, speaker silent); 8 kHz works.
_HANDSFREE_PREFERENCE = ["headset-head-unit-cvsd", "headset-head-unit", "headset-head-unit-msbc"]
_ALSA_ROUTING_FILES = ["/etc/alsa/conf.d/99-pipewire-default.conf", "/etc/alsa/conf.d/99-pulse.conf"]
_ADDRESS = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$")


def profile_label(name: str, description: str = "") -> str:
    """A plain-language name for a card profile."""
    if name.startswith("headset-head-unit"):
        desc = description.lower()
        if name.endswith("cvsd") or "cvsd" in desc:
            return "Hands-free, 8 kHz (most reliable)"
        if "msbc" in desc or "lc3" in desc or name.endswith("msbc"):
            return "Hands-free, 16 kHz"
        return "Hands-free"
    if name.startswith("a2dp"):
        return "Playback only, no mic" + (" (high quality)" if "xq" in name else "")
    if name == "off":
        return "Off"
    return description or name


def is_handsfree(profile: str | None) -> bool:
    return bool(profile) and profile.startswith("headset-head-unit")


def _address_of(obj: dict) -> str | None:
    props = obj.get("properties") or {}
    address = props.get("api.bluez5.address")
    if not address:
        # bluez_card.AA_BB_CC_DD_EE_FF
        m = re.search(r"([0-9A-Fa-f]{2}(?:[_:][0-9A-Fa-f]{2}){5})", obj.get("name", ""))
        address = m.group(1).replace("_", ":") if m else None
    return address.upper() if address else None


def _is_bluez(obj: dict) -> bool:
    props = obj.get("properties") or {}
    return props.get("device.api") == "bluez5" or obj.get("name", "").startswith(("bluez_card.", "bluez_output.", "bluez_input."))


def _volume_percent(sink: dict) -> int | None:
    values = []
    for channel in (sink.get("volume") or {}).values():
        if isinstance(channel, dict):
            pct = str(channel.get("value_percent", "")).rstrip("%")
            if pct.isdigit():
                values.append(int(pct))
    return round(sum(values) / len(values)) if values else None


def parse_audio(cards: list, sinks: list, sources: list, default_sink: str, default_source: str) -> dict[str, dict]:
    """Bluetooth address -> audio info, from `pactl --format=json` output."""
    result: dict[str, dict] = {}
    for card in cards:
        if not _is_bluez(card):
            continue
        address = _address_of(card)
        if not address:
            continue
        profiles = [
            {"name": name, "label": profile_label(name, (p or {}).get("description", "")),
             "available": bool((p or {}).get("available", True))}
            for name, p in (card.get("profiles") or {}).items()
        ]
        # Hands-free modes first (the ones Outpost needs), "off" last.
        profiles.sort(key=lambda p: (not is_handsfree(p["name"]), p["name"] == "off",
                                     _HANDSFREE_PREFERENCE.index(p["name"])
                                     if p["name"] in _HANDSFREE_PREFERENCE else 99))
        sink = next((s for s in sinks if _is_bluez(s) and _address_of(s) == address
                     and not s.get("name", "").endswith(".monitor")), None)
        source = next((s for s in sources if _is_bluez(s) and _address_of(s) == address
                       and not s.get("name", "").endswith(".monitor")), None)
        active = card.get("active_profile")
        result[address] = {
            "card": card.get("name"),
            "profile": active,
            "profile_label": profile_label(active or "", ((card.get("profiles") or {}).get(active) or {}).get("description", "")),
            "handsfree": is_handsfree(active),
            "has_handsfree": any(is_handsfree(p["name"]) for p in profiles),
            "profiles": profiles,
            "sink": sink.get("name") if sink else None,
            "source": source.get("name") if source else None,
            "volume": _volume_percent(sink) if sink else None,
            "default_speaker": bool(sink) and sink.get("name") == default_sink,
            "default_mic": bool(source) and source.get("name") == default_source,
        }
        info = result[address]
        info["outpost_headset"] = info["handsfree"] and info["default_speaker"] and info["default_mic"]
    return result


async def _run_command(args: list[str], timeout: float) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode, out.decode(errors="replace")


class HeadsetAudioError(Exception):
    pass


class NoHandsFreeError(HeadsetAudioError):
    """The headset is connected without its hands-free mode. Happens when
    an Inrico connects to the Pi by itself after being switched on;
    reconnecting from the Pi's side brings the mode back."""


class HeadsetAudio:
    def __init__(self, runner=None, alsa_routed=None):
        # runner(args, timeout) -> (returncode, output); swapped in tests.
        self._runner = runner or _run_command
        self._alsa_routed = alsa_routed
        self._lock = asyncio.Lock()
        self._cache: tuple[float, dict] | None = None

    def unavailable_reason(self) -> str | None:
        if self._runner is not _run_command:
            return None
        if not sys.platform.startswith("linux"):
            return "headset audio settings are only supported on Linux (e.g. a Raspberry Pi)"
        if shutil.which("pactl") is None:
            return "pactl isn't installed - run: sudo apt install pulseaudio-utils"
        return None

    async def _run(self, *args: str, check: bool = True) -> str:
        try:
            code, out = await self._runner(list(args), _COMMAND_TIMEOUT_S)
        except asyncio.TimeoutError:
            raise HeadsetAudioError(f"{args[0]} {args[1] if len(args) > 1 else ''} timed out".strip())
        except OSError as exc:
            raise HeadsetAudioError(f"couldn't run {args[0]}: {exc}")
        if check and code != 0:
            raise HeadsetAudioError(out.strip().splitlines()[-1] if out.strip() else f"{args[0]} failed")
        return out

    async def _json(self, what: str) -> list:
        out = await self._run("pactl", "--format=json", "list", what)
        try:
            data = json.loads(out)
        except ValueError:
            raise HeadsetAudioError("couldn't read the audio devices (pactl is too old for --format=json?)")
        return data if isinstance(data, list) else []

    async def devices(self) -> dict[str, dict]:
        now = time.monotonic()
        if self._cache is not None and now - self._cache[0] < _STATE_CACHE_S:
            return self._cache[1]
        cards = await self._json("cards")
        sinks = await self._json("sinks")
        sources = await self._json("sources")
        default_sink = (await self._run("pactl", "get-default-sink", check=False)).strip()
        default_source = (await self._run("pactl", "get-default-source", check=False)).strip()
        result = parse_audio(cards, sinks, sources, default_sink, default_source)
        self._cache = (time.monotonic(), result)
        return result

    async def autoswitch(self) -> bool | None:
        """Whether WirePlumber switches headsets between modes by itself -
        None where there's no such setting (WirePlumber 0.4)."""
        if shutil.which("wpctl") is None and self._runner is _run_command:
            return None
        try:
            out = await self._run("wpctl", "settings", AUTOSWITCH_SETTING, check=False)
        except HeadsetAudioError:
            return None
        m = re.search(r"Value:\s*(true|false)", out)
        return None if m is None else m.group(1) == "true"

    def alsa_routed(self) -> bool:
        """Whether the system default sound device goes through PipeWire."""
        if self._alsa_routed is not None:
            return self._alsa_routed()
        if any(os.path.exists(p) for p in _ALSA_ROUTING_FILES):
            return True
        for path in (os.path.expanduser("~/.asoundrc"), "/etc/asound.conf"):
            try:
                with open(path) as f:
                    text = f.read()
            except OSError:
                continue
            if "pulse" in text or "pipewire" in text:
                return True
        return False

    async def _device(self, address) -> dict:
        if not isinstance(address, str) or not _ADDRESS.match(address.upper()):
            raise HeadsetAudioError("invalid device address")
        self._cache = None
        info = (await self.devices()).get(address.upper())
        if info is None:
            raise HeadsetAudioError("that device isn't connected as an audio device - connect it first")
        return info

    async def _wait_for(self, address: str, ready) -> dict:
        deadline = time.monotonic() + _PROFILE_SETTLE_S
        while True:
            self._cache = None
            info = (await self.devices()).get(address)
            if info is not None and ready(info):
                return info
            if time.monotonic() >= deadline:
                return info or {}
            await asyncio.sleep(0.5)

    async def set_profile(self, address: str, profile: str) -> str:
        async with self._lock:
            info = await self._device(address)
            names = {p["name"]: p for p in info["profiles"]}
            if profile not in names:
                raise HeadsetAudioError(f"this device has no mode {profile!r}")
            await self._run("pactl", "set-card-profile", info["card"], profile)
            self._cache = None
        return f"switched to {names[profile]['label']}"

    async def use_for_outpost(self, address: str) -> str:
        async with self._lock:
            info = await self._device(address)
            if not info["sink"]:
                raise HeadsetAudioError("this device has no speaker in its current mode")
            await self._run("pactl", "set-default-sink", info["sink"])
            if info["source"]:
                await self._run("pactl", "set-default-source", info["source"])
            self._cache = None
        if not info["source"]:
            return "speaker in use - this mode has no mic; switch to a hands-free mode to talk"
        return "Outpost now uses this device's mic and speaker"

    async def set_volume(self, address: str, percent) -> str:
        if not isinstance(percent, int) or isinstance(percent, bool) or not 0 <= percent <= 100:
            raise HeadsetAudioError("volume must be a whole number from 0 to 100")
        async with self._lock:
            info = await self._device(address)
            if not info["sink"]:
                raise HeadsetAudioError("this device has no speaker in its current mode")
            await self._run("pactl", "set-sink-volume", info["sink"], f"{percent}%")
            self._cache = None
        return f"volume {percent}%"

    async def setup(self, address: str) -> str:
        """Everything a headset needs for Outpost: automatic mode switching
        off, hands-free mode, Outpost's mic and speaker, full volume."""
        async with self._lock:
            info = await self._device(address)
            address = address.upper()
            if not info["has_handsfree"]:
                raise NoHandsFreeError("this device isn't offering a hands-free mode, so it has no mic Outpost can use")
            if await self.autoswitch():
                await self._run("wpctl", "settings", "--save", AUTOSWITCH_SETTING, "false")
                log.info("bluetooth audio: turned off automatic headset mode switching")
            names = [p["name"] for p in info["profiles"] if p["available"]]
            profile = next((n for n in _HANDSFREE_PREFERENCE if n in names),
                           next((n for n in names if is_handsfree(n)), None))
            if profile is None:
                raise HeadsetAudioError("the hands-free mode isn't available right now - reconnect the headset and try again")
            if info["profile"] != profile:
                await self._run("pactl", "set-card-profile", info["card"], profile)
            info = await self._wait_for(address, lambda i: i["profile"] == profile and i["sink"] and i["source"])
            if not info.get("sink") or not info.get("source"):
                raise HeadsetAudioError("switched to hands-free mode, but its mic and speaker didn't appear - try again")
            await self._run("pactl", "set-default-sink", info["sink"])
            await self._run("pactl", "set-default-source", info["source"])
            await self._run("pactl", "set-sink-volume", info["sink"], "100%")
            self._cache = None
        log.info("bluetooth audio: %s set up as Outpost's headset (%s)", address, profile)
        return f"set up as Outpost's headset ({profile_label(profile)})"
