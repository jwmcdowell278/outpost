"""Small DSP stages applied to captured PCM before AGC/noise suppression/
encoding - a one-pole high-pass filter to strip DC offset/sub-audible rumble,
and a hold-time noise gate (squelch) that mutes background noise during
pauses within a held PTT transmission. Pure numpy, same pattern as
`outpost/agc.py` (no extra dependency, no model to vet).
"""
from __future__ import annotations
import math
import numpy as np


class HighPassFilter:
    """One-pole RC-style high-pass filter - removes DC offset and
    sub-audible rumble/handling noise below `cutoff_hz` before AGC/squelch
    see that energy and react to it."""

    def __init__(self, cutoff_hz: float, sample_rate: int):
        self.sample_rate = sample_rate
        self.set_cutoff(cutoff_hz)
        self._prev_in = 0.0
        self._prev_out = 0.0

    def set_cutoff(self, cutoff_hz: float) -> None:
        dt = 1.0 / self.sample_rate
        rc = 1.0 / (2 * math.pi * cutoff_hz)
        self.alpha = rc / (rc + dt)

    def process(self, pcm_bytes: bytes) -> bytes:
        samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float64)
        if samples.size == 0:
            return pcm_bytes

        alpha = self.alpha
        prev_in = self._prev_in
        prev_out = self._prev_out
        out = np.empty_like(samples)
        for i, x in enumerate(samples):
            y = alpha * (prev_out + x - prev_in)
            out[i] = y
            prev_in = x
            prev_out = y
        self._prev_in = prev_in
        self._prev_out = prev_out

        return np.clip(out, -32768, 32767).astype(np.int16).tobytes()


class Squelch:
    """Hard noise gate: mutes frames whose RMS stays below `threshold_rms`
    for more than `hold_frames` consecutive frames, so continuous
    background noise (wind, engine, chatter) between words isn't
    transmitted. `hold_frames` keeps the gate open briefly after the signal
    drops back below threshold so trailing syllables aren't clipped."""

    def __init__(self, threshold_rms: float, hold_frames: int = 10):
        self.threshold_rms = threshold_rms
        self.hold_frames = hold_frames
        self._hold_remaining = 0

    def process(self, pcm_bytes: bytes) -> bytes:
        samples = np.frombuffer(pcm_bytes, dtype=np.int16)
        if samples.size == 0:
            return pcm_bytes

        rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
        if rms >= self.threshold_rms:
            self._hold_remaining = self.hold_frames
            return pcm_bytes
        if self._hold_remaining > 0:
            self._hold_remaining -= 1
            return pcm_bytes
        return b"\x00" * len(pcm_bytes)
