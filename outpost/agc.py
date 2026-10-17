"""Automatic gain control (AGC) for 16-bit PCM frames.

Classic DSP (no model, no dependency beyond numpy - already required by
`audio.py`). Smooths each frame's RMS level toward a target over several
frames (bounded, gradual gain changes to avoid audible "pumping"), and skips
adapting on near-silent frames so it doesn't amplify the noise floor during
gaps in speech. This is what lets a mesh of nodes with wildly different
mic/gain levels be used without manually tuning input volume per device.
"""
from __future__ import annotations
import numpy as np

DEFAULT_TARGET_RMS = 4000.0
DEFAULT_MAX_GAIN = 10.0
DEFAULT_MIN_GAIN = 0.05
DEFAULT_ATTACK = 0.3
DEFAULT_SILENCE_RMS = 50.0


class AutomaticGainControl:
    def __init__(
        self,
        target_rms: float = DEFAULT_TARGET_RMS,
        max_gain: float = DEFAULT_MAX_GAIN,
        min_gain: float = DEFAULT_MIN_GAIN,
        attack: float = DEFAULT_ATTACK,
        silence_rms: float = DEFAULT_SILENCE_RMS,
    ):
        self.target_rms = target_rms
        self.max_gain = max_gain
        self.min_gain = min_gain
        self.attack = attack
        self.silence_rms = silence_rms
        self._gain = 1.0

    def process(self, pcm_bytes: bytes) -> bytes:
        samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float64)
        if samples.size == 0:
            return pcm_bytes

        rms = float(np.sqrt(np.mean(samples ** 2)))
        if rms > self.silence_rms:
            desired_gain = self.target_rms / max(rms, 1.0)
            desired_gain = min(max(desired_gain, self.min_gain), self.max_gain)
            self._gain += (desired_gain - self._gain) * self.attack

        boosted = np.clip(samples * self._gain, -32768, 32767).astype(np.int16)
        return boosted.tobytes()
