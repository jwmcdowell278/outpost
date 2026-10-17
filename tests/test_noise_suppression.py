"""Verifies the noise-suppression hook: PassthroughSuppressor is a true no-op,
and OutpostClient's tx path runs captured frames through whatever
NoiseSuppressor it's given before handing them to the Opus encoder."""
from __future__ import annotations
import asyncio

from outpost.client import OutpostClient
from outpost.config import OutpostConfig, Channel
from outpost.crypto import generate_channel_key
from outpost.noise_suppression import PassthroughSuppressor


def test_passthrough_suppressor_returns_input_unchanged():
    suppressor = PassthroughSuppressor()
    frame = b"\x01\x02\x03\x04"
    assert suppressor.process(frame) == frame


class _SpySuppressor:
    def __init__(self):
        self.calls: list[bytes] = []

    def process(self, pcm_bytes: bytes) -> bytes:
        self.calls.append(pcm_bytes)
        return pcm_bytes


def test_client_defaults_to_passthrough_suppressor():
    config = _make_config()
    client = OutpostClient(config, "ops")
    assert isinstance(client.noise_suppressor, PassthroughSuppressor)


def test_tx_loop_runs_captured_frames_through_the_configured_suppressor():
    asyncio.run(_tx_loop_runs_frames_through_suppressor())


async def _tx_loop_runs_frames_through_suppressor():
    config = _make_config()
    spy = _SpySuppressor()
    client = OutpostClient(config, "ops", noise_suppressor=spy)
    await client.transport.start(config.listen_port)
    client._ptt_active = True

    # Replace mic.read_frame with a non-blocking stub instead of calling
    # mic.start() (no real audio hardware) or feeding its queue (whose
    # blocking .get() would leave a background thread stuck once the queue
    # runs dry, hanging process teardown) - this just returns instantly.
    silence = b"\x00" * (client.codec.frame_size * 2)
    client.mic.read_frame = lambda timeout=None: silence

    try:
        tx_task = asyncio.create_task(client._tx_loop())
        await asyncio.sleep(0.1)
    finally:
        tx_task.cancel()
        client.transport.stop()

    assert len(spy.calls) > 0
    assert all(frame == silence for frame in spy.calls)


def _make_config() -> OutpostConfig:
    ops_key = generate_channel_key()
    return OutpostConfig(
        sender_id=1,
        listen_port=0,
        mesh_key=generate_channel_key(),
        channels={1: Channel(id=1, name="ops", key=ops_key)},
        enable_mdns=False,
        enable_pex=False,
    )
