"""Tests for the automatic gain control (AGC) DSP stage."""
from __future__ import annotations
import numpy as np

from outpost.agc import AutomaticGainControl


def _frame(amplitude: float, n: int = 320) -> bytes:
    samples = np.full(n, amplitude, dtype=np.float64)
    return samples.astype(np.int16).tobytes()


def _rms_of(pcm_bytes: bytes) -> float:
    samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float64)
    return float(np.sqrt(np.mean(samples ** 2)))


def test_quiet_signal_is_boosted_toward_target_over_several_frames():
    agc = AutomaticGainControl(target_rms=4000.0, attack=0.5)
    # Desired gain (4000/600 ~= 6.7) stays under the default max_gain (10),
    # so this should converge to the target rather than saturate the clamp.
    quiet = _frame(600.0)

    rms_before = _rms_of(quiet)
    for _ in range(20):
        boosted = agc.process(quiet)
    rms_after = _rms_of(boosted)

    assert rms_after > rms_before
    # Should converge close to the target, not overshoot wildly.
    assert 3000.0 < rms_after < 5000.0


def test_loud_signal_is_attenuated_to_avoid_clipping():
    agc = AutomaticGainControl(target_rms=4000.0, attack=0.5)
    loud = _frame(20000.0)

    for _ in range(20):
        attenuated = agc.process(loud)
    rms_after = _rms_of(attenuated)

    assert rms_after < _rms_of(loud)
    assert 3000.0 < rms_after < 5000.0


def test_silence_is_not_amplified():
    agc = AutomaticGainControl(target_rms=4000.0, silence_rms=50.0, attack=0.5)
    silence = _frame(0.0)

    for _ in range(20):
        result = agc.process(silence)

    assert _rms_of(result) == 0.0


def test_gain_stays_within_configured_bounds():
    agc = AutomaticGainControl(target_rms=4000.0, max_gain=3.0, min_gain=0.5, attack=1.0)
    very_quiet = _frame(10.0)

    for _ in range(5):
        agc.process(very_quiet)

    assert agc._gain <= 3.0

    very_loud = _frame(30000.0)
    for _ in range(5):
        agc.process(very_loud)

    assert agc._gain >= 0.5
