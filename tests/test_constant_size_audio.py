"""AUDIO packets are all the same size (constant_size_audio, on by default)
so packet lengths can't reveal what's being said - see protocol.pad_audio."""
from __future__ import annotations
import asyncio
import itertools
import numpy as np
import pytest

from outpost.client import OutpostClient
from outpost.config import OutpostConfig, Channel
from outpost.crypto import ChannelCipher, generate_channel_key
from outpost.protocol import (
    Header, VERSION, TYPE_AUDIO, audio_pad_size, pad_audio, unpad_audio,
)


def _make_client(**overrides):
    key = generate_channel_key()
    client = OutpostClient(OutpostConfig(
        sender_id=1, listen_port=0, mesh_key=generate_channel_key(),
        channels={1: Channel(1, "ops", key)}, enable_mdns=False, enable_pex=False, **overrides,
    ), "ops")
    return client, key


def _varied_frames(frame_size):
    rng = np.random.default_rng(0)
    silence = np.zeros(frame_size)
    noise = rng.normal(0, 6000, frame_size)
    tone = 8000 * np.sin(np.arange(frame_size) * 0.3)
    loud = rng.normal(0, 15000, frame_size)
    return [f.astype(np.int16).tobytes() for f in (silence, noise, tone, loud, silence, loud)]


async def _sent_audio(client):
    frames = itertools.cycle(_varied_frames(client.codec.frame_size))
    client.mic.read_frame = lambda timeout=None: next(frames)
    client._ptt_active = True
    # Disable DSP so the raw differences reach the encoder.
    client.agc_enabled = client.highpass_enabled = False
    sent = []
    client.transport.send = lambda channel_id, seq, ciphertext, ptype=None, targets=None: sent.append((seq, ciphertext))
    task = asyncio.create_task(client._tx_loop())
    while len(sent) < 30:
        await asyncio.sleep(0.01)
    task.cancel()
    return sent


def test_every_audio_packet_is_the_same_size():
    client, key = _make_client()
    sent = asyncio.run(_sent_audio(client))
    assert {len(ct) for _, ct in sent} == {client.audio_pad + 16}  # + Poly1305 tag


def test_sizes_vary_with_padding_off():
    client, key = _make_client(constant_size_audio=False)
    assert client.audio_pad is None
    sent = asyncio.run(_sent_audio(client))
    assert len({len(ct) for _, ct in sent}) > 1


def test_padded_audio_still_decodes_to_sound():
    client, key = _make_client()
    sent = asyncio.run(_sent_audio(client))
    cipher = ChannelCipher(key)
    seq, ct = sent[3]
    opus = unpad_audio(cipher.decrypt(Header(VERSION, TYPE_AUDIO, 1, 1, seq), ct))
    pcm = np.frombuffer(client.codec.decode(opus), dtype=np.int16)
    assert len(pcm) == client.codec.frame_size


def test_pad_size_matches_configured_bitrate():
    assert audio_pad_size(16000, 20) == 40     # default profile
    assert audio_pad_size(6000, 60) == 45      # constrained radio profile
    assert audio_pad_size(1000, 10) == 16      # floor


@pytest.mark.parametrize("opus", [b"", b"\x80", b"\x80\x00", b"data\x00\x00", bytes(range(39))])
def test_pad_roundtrip(opus):
    assert unpad_audio(pad_audio(opus, 40)) == opus
    assert unpad_audio(pad_audio(opus, None)) == opus
    assert len(pad_audio(opus, 40)) == max(40, len(opus) + 1)


def test_receiver_drops_audio_with_bad_padding():
    client, key = _make_client()
    played = []
    client.mixer.push = lambda ch, sid, seq, opus: played.append(opus)
    client.transport.confirm_peer = lambda *a: True
    header = Header(VERSION, TYPE_AUDIO, 1, 7, 100)
    opus = client.codec.encode(b"\x00\x00" * client.codec.frame_size)
    # Ends in 0x01 - no 0x80 marker after trailing zeros are stripped.
    client._on_packet(header, ChannelCipher(key).encrypt(header, opus + b"\x01\x00\x00"), ("10.0.0.2", 5555))
    assert played == []
