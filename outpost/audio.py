"""Microphone capture and speaker playback using PortAudio (sounddevice).

Both classes move fixed-size int16 PCM frames (matching the Opus codec's
frame_size) through a queue so the audio callback thread never blocks.
"""
from __future__ import annotations
import asyncio
import json
import logging
import os
import queue
import sys
import time
import numpy as np
import sounddevice as sd

log = logging.getLogger(__name__)

# "input"/"output" -> the PortAudio device-info key saying how many channels
# a device has in that direction (0 means it can't be used that way).
_CHANNEL_KEYS = {"input": "max_input_channels", "output": "max_output_channels"}


def list_devices(kind: str) -> list[dict]:
    """Every audio device usable as `kind` ("input" or "output"), as
    {"index", "name", "hostapi", "default"} dicts - for the web UI's device
    pickers and `python -m outpost.audio`."""
    key = _CHANNEL_KEYS[kind]
    default_index = sd.default.device[0 if kind == "input" else 1]
    hostapis = sd.query_hostapis()
    result = []
    for dev in sd.query_devices():
        if dev[key] <= 0:
            continue
        result.append({
            "index": dev["index"],
            "name": dev["name"],
            "hostapi": hostapis[dev["hostapi"]]["name"],
            "default": dev["index"] == default_index,
        })
    return result


def _match_device(spec: str | int, devices: list[dict]) -> list[dict]:
    """Devices (from list_devices) that a configured value refers to, best
    match first: an index matches exactly; a name matches as a
    case-insensitive substring, with exact name matches ranked first."""
    if isinstance(spec, int):
        return [d for d in devices if d["index"] == spec]
    needle = spec.lower()
    exact = [d for d in devices if d["name"].lower() == needle]
    partial = [d for d in devices if needle in d["name"].lower()]
    return exact or partial


def device_available(spec: str | int, devices: list[dict]) -> bool:
    return bool(_match_device(spec, devices))


def resolve_device(spec: str | int | None, kind: str) -> int | None:
    """Turns a configured input_device/output_device value into a PortAudio
    device index, or None for the system default.

    `spec` is either a device index (int) or a case-insensitive substring
    of the device name - names are preferred in config since indices can
    shift when USB devices are plugged/unplugged or the machine reboots. An
    exact name match wins over a partial one. If nothing matches, logs a
    warning and falls back to the system default rather than refusing to
    start (same policy as channel_announce_voice), since a headless node
    with no audio is harder to recover than one on the wrong device."""
    if spec is None or spec == "":
        return None
    devices = list_devices(kind)
    matches = _match_device(spec, devices)
    if not matches:
        log.warning("%s_device %r not found - using system default (available: %s)",
                    kind, spec, ", ".join(d["name"] for d in devices) or "none")
        return None
    if len(matches) > 1:
        log.warning("%s_device %r matches several devices (%s) - using the first; "
                    "use a longer name to pick a different one",
                    kind, spec, ", ".join(d["name"] for d in matches))
    return matches[0]["index"]


def rescan_devices() -> None:
    """Makes PortAudio pick up devices added/removed since it started (it
    only scans once, at initialization). Closes every open stream, so only
    call this between stopping and restarting all of them - see
    restart_streams."""
    sd._terminate()
    sd._initialize()


def restart_streams(*streams: "_AudioStream") -> None:
    """Closes `streams`, rescans devices, and reopens them - re-resolving
    each configured device, so a device that vanished falls back to the
    system default and one that came back is picked up again. Raises if a
    stream can't be reopened (e.g. no audio device at all); the caller is
    expected to retry."""
    for stream in streams:
        stream.close()
    rescan_devices()
    for stream in streams:
        stream.open()


# How long a running stream can go without its audio callback firing before
# it's considered dead. Both streams fire every frame (20ms by default) even
# when nobody's talking - input keeps capturing, output keeps playing
# silence - so seconds of quiet means the device is gone (e.g. Bluetooth
# earbuds disconnected) or the stream stalled (e.g. across laptop sleep).
STALL_TIMEOUT_S = 3.0


class _AudioStream:
    """Shared open/close/stall-tracking for MicCapture and SpeakerPlayback."""

    kind = ""  # "input" or "output"

    def __init__(self, sample_rate: int, frame_size: int, device: str | int | None = None):
        self.sample_rate = sample_rate
        self.frame_size = frame_size
        self.device = device
        self._queue: "queue.Queue[bytes]" = queue.Queue()
        self._stream = None
        self._wanted = False  # between start() and stop()
        self.last_callback = 0.0
        self.device_name: str | None = None
        # True when a specific device is configured but wasn't found, so
        # the system default is being used instead.
        self.on_fallback = False

    def _make_stream(self, device: int | None):
        raise NotImplementedError

    def open(self) -> None:
        index = resolve_device(self.device, self.kind)
        self.on_fallback = self.device not in (None, "") and index is None
        self.device_name = sd.query_devices(index, self.kind)["name"]
        log.info("%s device: %s", self.kind, self.device_name)
        self.last_callback = time.monotonic()
        stream = self._make_stream(index)
        stream.start()
        self._stream = stream

    def close(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        # A stream whose device has disappeared can error on the way down -
        # it's being discarded either way.
        try:
            stream.stop()
        except Exception as exc:
            log.debug("error stopping %s stream: %s", self.kind, exc)
        try:
            stream.close()
        except Exception as exc:
            log.debug("error closing %s stream: %s", self.kind, exc)

    def start(self) -> None:
        self._wanted = True
        self.open()

    def stop(self) -> None:
        self._wanted = False
        self.close()

    def stalled(self, now: float | None = None) -> bool:
        """True if this stream should be running but isn't delivering audio
        (dead device, stalled stream, or a failed reopen)."""
        if not self._wanted:
            return False
        if self._stream is None:
            return True
        now = time.monotonic() if now is None else now
        return now - self.last_callback > STALL_TIMEOUT_S


class MicCapture(_AudioStream):
    """Captures mono int16 PCM frames of a fixed size from the configured
    input device (see resolve_device), or the system default."""

    kind = "input"

    def _callback(self, indata, frames, time_info, status) -> None:
        self.last_callback = time.monotonic()
        # Overflows are acceptable to drop here - the receive side's PLC
        # handles the resulting gap in sequence numbers.
        self._queue.put(bytes(indata))

    def _make_stream(self, device):
        return sd.RawInputStream(
            device=device,
            samplerate=self.sample_rate,
            blocksize=self.frame_size,
            channels=1,
            dtype="int16",
            callback=self._callback,
        )

    def wake_reader(self) -> None:
        """Unblocks a read_frame() call waiting on a stopped stream (e.g. the
        client's transmit thread), so shutdown doesn't wait on it forever."""
        self._queue.put(b"")

    def read_frame(self, timeout: float | None = None) -> bytes:
        # Keeps blocking across a restart_streams() - the reopened stream
        # feeds the same queue.
        return self._queue.get(timeout=timeout)


class SpeakerPlayback(_AudioStream):
    """Plays mono int16 PCM frames through the configured output device
    (see resolve_device), or the system default.

    With `source` set (the client's Mixer.next_frame), the audio callback
    pulls each frame from it, so playout is clocked by the sound card.
    Otherwise frames queued via play_frame() are played in order."""

    kind = "output"

    def __init__(self, sample_rate: int, frame_size: int, device: str | int | None = None,
                 source=None):
        super().__init__(sample_rate, frame_size, device)
        self._silence = np.zeros(frame_size, dtype=np.int16).tobytes()
        self._source = source
        self._source_failed = False

    def _callback(self, outdata, frames, time_info, status) -> None:
        self.last_callback = time.monotonic()
        if self._source is not None:
            try:
                frame = self._source()
            except Exception:
                # Never let an exception escape the audio callback - that
                # stops the stream. Log once; keep playing silence.
                if not self._source_failed:
                    self._source_failed = True
                    log.exception("speaker source failed - playing silence")
                frame = self._silence
        else:
            try:
                frame = self._queue.get_nowait()
            except queue.Empty:
                frame = self._silence
        outdata[:] = frame

    def _make_stream(self, device):
        return sd.RawOutputStream(
            device=device,
            samplerate=self.sample_rate,
            blocksize=self.frame_size,
            channels=1,
            dtype="int16",
            callback=self._callback,
        )

    def play_frame(self, frame: bytes) -> None:
        self._queue.put(frame)


def _print_devices() -> None:
    for kind in ("input", "output"):
        print(f"{kind.capitalize()} devices ({kind}_device):")
        devices = list_devices(kind)
        if not devices:
            print("  (none)")
        for d in devices:
            marker = "  (default)" if d["default"] else ""
            print(f"  [{d['index']}] {d['name']}  - {d['hostapi']}{marker}")
        print()


async def list_devices_fresh(timeout: float = 10.0) -> dict:
    """{"input": [...], "output": [...]} as list_devices returns, but
    including devices connected after this process started (e.g. Bluetooth
    earbuds paired later).

    PortAudio only scans devices once, at initialization, and rescanning
    in-process (sd._terminate/_initialize) would close the client's open
    mic/speaker streams - so this asks a short-lived child process instead,
    which always gets a fresh scan."""
    pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ)
    env["PYTHONPATH"] = pkg_parent + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "outpost.audio", "--json",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError("timed out listing audio devices")
    if proc.returncode != 0:
        raise RuntimeError(f"listing audio devices failed: {stderr.decode(errors='replace').strip()}")
    return json.loads(stdout)


if __name__ == "__main__":
    # python -m outpost.audio - lists audio devices so you can pick values
    # for config's input_device/output_device. --json is for
    # list_devices_fresh.
    if "--json" in sys.argv[1:]:
        print(json.dumps({"input": list_devices("input"), "output": list_devices("output")}))
    else:
        _print_devices()
