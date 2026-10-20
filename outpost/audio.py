"""Microphone capture and speaker playback using PortAudio (sounddevice).

Both classes move fixed-size int16 PCM frames (matching the Opus codec's
frame_size) through a queue so the audio callback thread never blocks.
"""
from __future__ import annotations
import logging
import queue
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
    if isinstance(spec, int):
        if any(d["index"] == spec for d in devices):
            return spec
        log.warning("%s_device %d not found or not an %s device - using system default", kind, spec, kind)
        return None

    needle = spec.lower()
    exact = [d for d in devices if d["name"].lower() == needle]
    partial = [d for d in devices if needle in d["name"].lower()]
    matches = exact or partial
    if not matches:
        log.warning("no %s device matches %r - using system default (available: %s)",
                    kind, spec, ", ".join(d["name"] for d in devices) or "none")
        return None
    if len(matches) > 1:
        log.warning("%s_device %r matches several devices (%s) - using the first; "
                    "use a longer name to pick a different one",
                    kind, spec, ", ".join(d["name"] for d in matches))
    return matches[0]["index"]


class MicCapture:
    """Captures mono int16 PCM frames of a fixed size from the configured
    input device (see resolve_device), or the system default."""

    def __init__(self, sample_rate: int, frame_size: int, device: str | int | None = None):
        self.sample_rate = sample_rate
        self.frame_size = frame_size
        self.device = device
        self._queue: "queue.Queue[bytes]" = queue.Queue()
        self._stream: sd.RawInputStream | None = None

    def _callback(self, indata, frames, time_info, status) -> None:
        # Overflows are acceptable to drop here - the receive side's PLC
        # handles the resulting gap in sequence numbers.
        self._queue.put(bytes(indata))

    def start(self) -> None:
        device = resolve_device(self.device, "input")
        log.info("%s device: %s", "input", sd.query_devices(device, "input")["name"])
        self._stream = sd.RawInputStream(
            device=device,
            samplerate=self.sample_rate,
            blocksize=self.frame_size,
            channels=1,
            dtype="int16",
            callback=self._callback,
        )
        self._stream.start()

    def stop(self) -> None:
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def read_frame(self, timeout: float | None = None) -> bytes:
        return self._queue.get(timeout=timeout)


class SpeakerPlayback:
    """Plays mono int16 PCM frames through the configured output device
    (see resolve_device), or the system default."""

    def __init__(self, sample_rate: int, frame_size: int, device: str | int | None = None):
        self.sample_rate = sample_rate
        self.frame_size = frame_size
        self.device = device
        self._queue: "queue.Queue[bytes]" = queue.Queue()
        self._stream: sd.RawOutputStream | None = None
        self._silence = np.zeros(frame_size, dtype=np.int16).tobytes()

    def _callback(self, outdata, frames, time_info, status) -> None:
        try:
            frame = self._queue.get_nowait()
        except queue.Empty:
            frame = self._silence
        outdata[:] = frame

    def start(self) -> None:
        device = resolve_device(self.device, "output")
        log.info("%s device: %s", "output", sd.query_devices(device, "output")["name"])
        self._stream = sd.RawOutputStream(
            device=device,
            samplerate=self.sample_rate,
            blocksize=self.frame_size,
            channels=1,
            dtype="int16",
            callback=self._callback,
        )
        self._stream.start()

    def stop(self) -> None:
        if self._stream:
            self._stream.stop()
            self._stream.close()
            self._stream = None

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


if __name__ == "__main__":
    # python -m outpost.audio - lists audio devices so you can pick values
    # for config's input_device/output_device.
    _print_devices()
