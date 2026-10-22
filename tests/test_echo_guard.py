"""Verifies the echo guard (`enable_echo_guard`): captured mic audio is
muted for a short window after this device's own speaker last played
anything, so acoustic bleed from a nearby device's speaker can't be picked
up and re-transmitted into a feedback loop - and has no effect at all
unless explicitly enabled."""
from __future__ import annotations
import asyncio
import time

import numpy as np

from outpost.client import OutpostClient
from outpost.config import OutpostConfig, Channel
from outpost.crypto import ChannelCipher, generate_channel_key
from outpost.protocol import Header, VERSION, TYPE_AUDIO, pad_audio, unpad_audio


def _make_client(**overrides) -> tuple[OutpostClient, bytes]:
    ops_key = generate_channel_key()
    defaults = dict(
        sender_id=1,
        listen_port=0,
        mesh_key=generate_channel_key(),
        channels={1: Channel(id=1, name="ops", key=ops_key)},
        enable_mdns=False,
        enable_pex=False,
    )
    defaults.update(overrides)
    return OutpostClient(OutpostConfig(**defaults), "ops"), ops_key


def test_echo_guard_disabled_by_default():
    client, _ = _make_client()
    assert client.enable_echo_guard is False


def test_own_speaker_active_false_before_any_playback():
    client, _ = _make_client(enable_echo_guard=True)
    assert client._own_speaker_active() is False


def test_own_speaker_active_true_right_after_playback():
    client, _ = _make_client(enable_echo_guard=True)
    client._play_local([b"\x00" * (client.codec.frame_size * 2)])
    assert client._own_speaker_active() is True


def test_own_speaker_active_false_after_hold_expires():
    client, _ = _make_client(enable_echo_guard=True, echo_guard_hold_ms=10)
    client._play_local([b"\x00" * (client.codec.frame_size * 2)])
    time.sleep(0.02)
    assert client._own_speaker_active() is False


def test_tx_loop_sends_silence_while_echo_guard_active():
    asyncio.run(_tx_loop_sends_silence_while_echo_guard_active())


async def _tx_loop_sends_silence_while_echo_guard_active():
    client, ops_key = _make_client(enable_echo_guard=True, echo_guard_hold_ms=1000)
    await client.transport.start(0)
    client._ptt_active = True
    client._last_own_speaker_activity = time.monotonic()  # simulate recent own playback

    loud = (np.ones(client.codec.frame_size, dtype=np.int16) * 20000).tobytes()
    client.mic.read_frame = lambda timeout=None: loud

    sent = []
    client.transport.send = lambda *args, **kwargs: sent.append(args)

    try:
        tx_task = asyncio.create_task(client._tx_loop())
        await asyncio.sleep(0.1)
    finally:
        tx_task.cancel()
        client.transport.stop()

    assert sent, "expected at least one frame to have been sent"
    cipher = ChannelCipher(ops_key)
    channel_id, seq, ciphertext = sent[0][0], sent[0][1], sent[0][2]
    opus_bytes = unpad_audio(cipher.decrypt(Header(VERSION, TYPE_AUDIO, channel_id, client.config.sender_id, seq), ciphertext))
    pcm = client.codec.decode(opus_bytes)
    # Opus is lossy even for silence input (tiny dither noise survives the
    # encode/decode round trip), so assert "near silent" rather than
    # bit-exact zero - this is nowhere close to the loud input frame's
    # amplitude of 20000.
    assert np.abs(np.frombuffer(pcm, dtype=np.int16)).max() < 50


def test_tx_loop_sends_real_audio_once_echo_guard_hold_expires():
    asyncio.run(_tx_loop_sends_real_audio_once_echo_guard_hold_expires())


async def _tx_loop_sends_real_audio_once_echo_guard_hold_expires():
    client, ops_key = _make_client(
        enable_echo_guard=True, echo_guard_hold_ms=10,
        enable_highpass=False, enable_agc=False,
    )
    await client.transport.start(0)
    client._ptt_active = True
    client._last_own_speaker_activity = time.monotonic() - 1.0  # long expired

    loud = (np.ones(client.codec.frame_size, dtype=np.int16) * 20000).tobytes()
    client.mic.read_frame = lambda timeout=None: loud

    sent = []
    client.transport.send = lambda *args, **kwargs: sent.append(args)

    try:
        tx_task = asyncio.create_task(client._tx_loop())
        await asyncio.sleep(0.1)
    finally:
        tx_task.cancel()
        client.transport.stop()

    assert sent, "expected at least one frame to have been sent"
    cipher = ChannelCipher(ops_key)
    channel_id, seq, ciphertext = sent[0][0], sent[0][1], sent[0][2]
    opus_bytes = unpad_audio(cipher.decrypt(Header(VERSION, TYPE_AUDIO, channel_id, client.config.sender_id, seq), ciphertext))
    pcm = client.codec.decode(opus_bytes)
    assert np.frombuffer(pcm, dtype=np.int16).max() > 0


def test_speaker_activity_recorded_from_received_audio_playback():
    client, key = _make_client(enable_echo_guard=True)
    assert client._own_speaker_active() is False

    from outpost.protocol import Header, VERSION

    silence = b"\x00" * (client.codec.frame_size * 2)
    opus_bytes = client.codec.encode(silence)
    cipher = ChannelCipher(key)
    header = Header(VERSION, TYPE_AUDIO, channel_id=1, sender_id=2, seq=0)
    ciphertext = cipher.encrypt(header, pad_audio(opus_bytes, None))
    client._on_packet(header, ciphertext, ("127.0.0.1", 9999))
    client.mixer.stop(1, 2)
    # The speaker's callback pulls the received frame out of the mixer.
    client.mixer.next_frame()

    assert client._own_speaker_active() is True
