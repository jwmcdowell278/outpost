"""Tests for the high-pass filter and squelch DSP stages (outpost/dsp.py)."""
from __future__ import annotations
import numpy as np

from outpost.dsp import HighPassFilter, Squelch


def _frame(amplitude: float, n: int = 320) -> bytes:
    samples = np.full(n, amplitude, dtype=np.float64)
    return samples.astype(np.int16).tobytes()


def _constant_offset_frame(dc: float, n: int = 320) -> bytes:
    samples = np.full(n, dc, dtype=np.float64)
    return samples.astype(np.int16).tobytes()


def _rms_of(pcm_bytes: bytes) -> float:
    samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float64)
    return float(np.sqrt(np.mean(samples ** 2)))


def test_highpass_removes_dc_offset_over_several_frames():
    hpf = HighPassFilter(cutoff_hz=100.0, sample_rate=16000)
    dc = _constant_offset_frame(2000.0)

    for _ in range(50):
        out = hpf.process(dc)

    # A pure DC signal should decay toward zero once past the filter's
    # settling time.
    assert abs(_rms_of(out)) < 50.0


def test_highpass_passes_empty_frame_unchanged():
    hpf = HighPassFilter(cutoff_hz=100.0, sample_rate=16000)
    assert hpf.process(b"") == b""


def test_highpass_set_cutoff_updates_alpha():
    hpf = HighPassFilter(cutoff_hz=100.0, sample_rate=16000)
    original_alpha = hpf.alpha
    hpf.set_cutoff(300.0)
    assert hpf.alpha != original_alpha


def test_squelch_passes_signal_above_threshold():
    squelch = Squelch(threshold_rms=300.0, hold_frames=2)
    loud = _frame(1000.0)
    assert squelch.process(loud) == loud


def test_squelch_mutes_quiet_signal_with_no_prior_signal():
    squelch = Squelch(threshold_rms=300.0, hold_frames=2)
    quiet = _frame(10.0)

    muted = squelch.process(quiet)

    assert _rms_of(muted) == 0.0
    assert muted == b"\x00" * len(quiet)


def test_squelch_mutes_quiet_signal_after_hold_expires():
    squelch = Squelch(threshold_rms=300.0, hold_frames=2)
    loud = _frame(1000.0)
    quiet = _frame(10.0)

    squelch.process(loud)  # opens the gate, hold_remaining = 2
    squelch.process(quiet)  # within hold window, passed through
    squelch.process(quiet)  # hold window exhausted
    muted = squelch.process(quiet)  # gate now closed - muted

    assert _rms_of(muted) == 0.0
    assert muted == b"\x00" * len(quiet)


def test_squelch_hold_keeps_gate_open_briefly_after_signal_drops():
    squelch = Squelch(threshold_rms=300.0, hold_frames=3)
    loud = _frame(1000.0)
    quiet = _frame(10.0)

    squelch.process(loud)  # opens the gate
    held = squelch.process(quiet)  # still within hold window

    assert held == quiet  # passed through, not muted


def test_squelch_passes_empty_frame_unchanged():
    squelch = Squelch(threshold_rms=300.0)
    assert squelch.process(b"") == b""
