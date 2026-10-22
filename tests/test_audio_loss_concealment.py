"""Verifies the AUDIO receive path's loss-concealment logic doesn't
misinterpret a seq gap caused by idle time as real packet loss.

seq is a single counter shared across every packet type from a sender
(AUDIO, START, STOP, CHANNEL_ANNOUNCE, PEX - see transport.py), which is
required for nonce safety (a (channel key, nonce) pair must never repeat),
but means a gap between two AUDIO packets from the same sender isn't purely
lost AUDIO frames - the sender's periodic channel-announce/PEX traffic
during a pause between transmissions also advances it. Treating the whole
gap as lost frames would flood playback with synthetic loss-concealment
proportional to elapsed idle time rather than real loss - see client.py's
`_MAX_CONCEALED_FRAMES`."""
from __future__ import annotations

from outpost.client import OutpostClient
from outpost.config import OutpostConfig, Channel
from outpost.crypto import ChannelCipher, generate_channel_key
from outpost.protocol import Header, VERSION, TYPE_AUDIO, pad_audio


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


def _deliver_audio(client, key, sender_id=2, seq=0):
    silence = b"\x00" * (client.codec.frame_size * 2)
    opus_bytes = client.codec.encode(silence)
    cipher = ChannelCipher(key)
    header = Header(VERSION, TYPE_AUDIO, channel_id=1, sender_id=sender_id, seq=seq)
    ciphertext = cipher.encrypt(header, pad_audio(opus_bytes, None))
    client._on_packet(header, ciphertext, ("127.0.0.1", 9999))


def test_small_seq_gap_plays_concealment_for_each_missing_frame():
    client, key = _make_client()
    played = []
    client._play_on_speaker = lambda pcm: played.append(pcm)

    _deliver_audio(client, key, seq=0)
    _deliver_audio(client, key, seq=4)  # 3 frames "missing"

    # seq=0's own real frame, + 3 concealment frames (seq 1,2,3), + the real seq-4 frame.
    assert len(played) == 5


def test_large_seq_gap_from_idle_control_traffic_skips_concealment_flood():
    client, key = _make_client()
    played = []
    client._play_on_speaker = lambda pcm: played.append(pcm)

    _deliver_audio(client, key, seq=0)
    # A large gap (e.g. from channel-announce/PEX seq increments accrued
    # during a pause between transmissions) must not flood playback with
    # hundreds of fake concealment frames.
    _deliver_audio(client, key, seq=500)

    assert len(played) == 2  # seq=0's real frame + the real resumed frame, no concealment flood


def test_gap_exactly_at_cap_still_plays_concealment():
    client, key = _make_client()
    played = []
    client._play_on_speaker = lambda pcm: played.append(pcm)

    _deliver_audio(client, key, seq=0)
    missing = client._MAX_CONCEALED_FRAMES
    _deliver_audio(client, key, seq=missing + 1)

    assert len(played) == missing + 2  # seq=0's real frame + `missing` concealment frames + the real resumed frame


def test_stale_duplicate_audio_is_dropped_and_logged(caplog):
    client, key = _make_client()
    played = []
    client._play_on_speaker = lambda pcm: played.append(pcm)

    _deliver_audio(client, key, seq=5)
    with caplog.at_level("DEBUG"):
        _deliver_audio(client, key, seq=3)  # older seq - stale/duplicate

    assert len(played) == 1  # only the first (seq=5) frame played
    assert "stale/duplicate/out-of-order" in caplog.text
