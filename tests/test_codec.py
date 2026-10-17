"""Verifies OpusCodec falls back to library defaults instead of crashing when
the platform's ctypes/opuslib build can't marshal the variadic
opus_encoder_ctl() call used to set bitrate/inband_fec/packet_loss_perc -
observed to surface as either opuslib.OpusError (some arm64 builds) or a bare
TypeError from opuslib's own ctypes wrapper (some armv7l builds)."""
from __future__ import annotations
import opuslib
import pytest

from outpost.codec import OpusCodec


class _BrokenCtlEncoder:
    """Stands in for opuslib.Encoder: bitrate assignment succeeds, but
    inband_fec raises the given exception, mirroring a real broken build."""

    def __init__(self, exc: Exception):
        self._exc = exc
        self.bitrate = None

    @property
    def inband_fec(self):
        return False

    @inband_fec.setter
    def inband_fec(self, value):
        raise self._exc


@pytest.mark.parametrize("exc", [opuslib.OpusError("bad argument"), TypeError("missing 1 required positional argument: 'value'")])
def test_ctl_failure_falls_back_instead_of_crashing(monkeypatch, exc):
    monkeypatch.setattr(opuslib, "Encoder", lambda *a, **kw: _BrokenCtlEncoder(exc))
    codec = OpusCodec()  # must not raise
    assert codec._encoder is not None
