"""Microphone capture and speaker playback using PortAudio (sounddevice).

Both classes move fixed-size int16 PCM frames (matching the Opus codec's
frame_size) through a queue so the audio callback thread never blocks.
"""
from __future__ import annotations
import queue
import numpy as np
import sounddevice as sd


class MicCapture:
    """Captures mono int16 PCM frames of a fixed size from the default input device."""

    def __init__(self, sample_rate: int, frame_size: int):
        self.sample_rate = sample_rate
        self.frame_size = frame_size
        self._queue: "queue.Queue[bytes]" = queue.Queue()
        self._stream: sd.RawInputStream | None = None

    def _callback(self, indata, frames, time_info, status) -> None:
        # Overflows are acceptable to drop here - the receive side's PLC
        # handles the resulting gap in sequence numbers.
        self._queue.put(bytes(indata))

    def start(self) -> None:
        self._stream = sd.RawInputStream(
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
    """Plays mono int16 PCM frames through the default output device."""

    def __init__(self, sample_rate: int, frame_size: int):
        self.sample_rate = sample_rate
        self.frame_size = frame_size
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
        self._stream = sd.RawOutputStream(
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
