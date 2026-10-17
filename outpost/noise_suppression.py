"""Pluggable noise-suppression stage applied to captured PCM before Opus
encoding (mirrors the `PTTSource` pattern: a narrow interface, injected
rather than hardcoded, so a real backend can be swapped in without touching
the client).

Ships with only a no-op default (`PassthroughSuppressor`) - no model-based
backend is bundled.
"""
from __future__ import annotations
from typing import Protocol


class NoiseSuppressor(Protocol):
    def process(self, pcm_bytes: bytes) -> bytes:
        """Takes one raw 16-bit PCM frame, returns a frame of the same size."""
        ...


class PassthroughSuppressor:
    """No-op default - used whenever no real suppressor is configured."""

    def process(self, pcm_bytes: bytes) -> bytes:
        return pcm_bytes
