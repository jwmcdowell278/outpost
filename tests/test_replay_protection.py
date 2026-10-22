"""Replay protection: captured channel packets resent later (by anyone, no
key needed) must not be played or acted on. See outpost/replay.py."""
from __future__ import annotations
import logging
import numpy as np

from outpost.client import OutpostClient
from outpost.config import OutpostConfig, Channel
from outpost.crypto import ChannelCipher, generate_channel_key
from outpost.protocol import Header, VERSION, TYPE_AUDIO, TYPE_START, TYPE_STOP, TYPE_CHANNEL_ANNOUNCE, pad_audio
from outpost.replay import ReplayWindow

SENDER = 7
LIVE = ("192.168.1.20", 5555)
ATTACKER = ("192.168.1.66", 5555)


# --- ReplayWindow -------------------------------------------------------

def test_window_rejects_exact_repeats():
    w = ReplayWindow(size=8)
    assert w.is_fresh(1, 10)
    w.mark(1, 10)
    assert not w.is_fresh(1, 10)
    assert w.is_fresh(1, 11)


def test_window_accepts_reordered_packets_once():
    w = ReplayWindow(size=8)
    w.mark(1, 10)
    w.mark(1, 12)
    assert w.is_fresh(1, 11)  # arrived late, never seen
    w.mark(1, 11)
    assert not w.is_fresh(1, 11)


def test_window_rejects_anything_older_than_window():
    w = ReplayWindow(size=8)
    w.mark(1, 100)
    assert not w.is_fresh(1, 92) and w.is_too_old(1, 92)
    assert w.is_fresh(1, 93) and not w.is_too_old(1, 93)


def test_window_handles_large_jump_like_a_sender_restart():
    w = ReplayWindow(size=8)
    w.mark(1, 100)
    w.mark(1, 100 + 256_000)  # restart resumes well above persisted seq
    assert not w.is_fresh(1, 100)
    assert w.is_fresh(1, 100 + 256_001)


def test_window_is_per_sender():
    w = ReplayWindow(size=8)
    w.mark(1, 50)
    assert w.is_fresh(2, 50)


# --- client receive path ------------------------------------------------

def _make_client():
    key = generate_channel_key()
    client = OutpostClient(OutpostConfig(
        sender_id=1, listen_port=0, mesh_key=generate_channel_key(),
        channels={1: Channel(1, "ops", key)}, enable_mdns=False, enable_pex=False,
    ), "ops")
    played = []
    client.mixer.push = lambda ch, sid, seq, opus: played.append(opus)
    client.transport.confirm_peer = lambda *a: True
    return client, ChannelCipher(key), played


def _audio(client, cipher, seq):
    pcm = np.zeros(client.codec.frame_size, dtype=np.int16).tobytes()
    header = Header(VERSION, TYPE_AUDIO, 1, SENDER, seq)
    return header, cipher.encrypt(header, pad_audio(client.codec.encode(pcm), None))


def _packet(cipher, ptype, seq, payload=b""):
    header = Header(VERSION, ptype, 1, SENDER, seq)
    return header, cipher.encrypt(header, payload)


def test_recorded_transmission_cannot_be_replayed_after_stop():
    client, cipher, played = _make_client()
    captured = [_audio(client, cipher, seq) for seq in range(100, 110)]
    stop = _packet(cipher, TYPE_STOP, 110)

    for h, ct in captured:
        client._on_packet(h, ct, LIVE)
    client._on_packet(*stop, LIVE)
    live = len(played)
    assert live == 10

    # Attacker resends the old transmission, with and without the old STOP.
    for h, ct in captured:
        client._on_packet(h, ct, ATTACKER)
    client._on_packet(*stop, ATTACKER)
    for h, ct in captured:
        client._on_packet(h, ct, ATTACKER)
    assert len(played) == live


def test_replayed_start_does_not_mark_sender_as_speaking():
    client, cipher, _ = _make_client()
    start = _packet(cipher, TYPE_START, 50, b"Basecamp")
    client._on_packet(*start, LIVE)
    client._on_packet(*_packet(cipher, TYPE_STOP, 51), LIVE)
    assert client.speaking_details(1) == []

    client._on_packet(*start, ATTACKER)
    assert client.speaking_details(1) == []


def test_replayed_stop_cannot_cut_off_a_live_speaker():
    client, cipher, _ = _make_client()
    old_stop = _packet(cipher, TYPE_STOP, 10)
    client._on_packet(*old_stop, LIVE)
    client._on_packet(*_packet(cipher, TYPE_START, 20, b"Basecamp"), LIVE)
    client._on_packet(*_audio(client, cipher, 21), LIVE)

    client._on_packet(*old_stop, ATTACKER)
    assert [s["alias"] for s in client.speaking_details(1)] == ["Basecamp"]


def test_replayed_channel_announce_is_ignored():
    client, cipher, _ = _make_client()
    confirmed = []
    client.transport.confirm_peer = lambda sid, peer: confirmed.append(peer) or True
    announce = _packet(cipher, TYPE_CHANNEL_ANNOUNCE, 5)
    client._on_packet(*announce, LIVE)
    client._on_packet(*announce, ATTACKER)
    assert [(p.host, p.port) for p in confirmed] == [LIVE]


def test_start_overtaken_by_first_audio_frame_is_still_accepted():
    client, cipher, played = _make_client()
    client._on_packet(*_audio(client, cipher, 31), LIVE)
    client._on_packet(*_packet(cipher, TYPE_START, 30, b"Basecamp"), LIVE)
    assert [s["alias"] for s in client.speaking_details(1)] == ["Basecamp"]


def test_forged_packet_does_not_advance_the_window():
    client, cipher, played = _make_client()
    client._on_packet(*_audio(client, cipher, 100), LIVE)
    # Bogus high seq with garbage ciphertext - fails authentication...
    client._on_packet(Header(VERSION, TYPE_AUDIO, 1, SENDER, 10_000_000), b"\x00" * 40, ATTACKER)
    # ...so the sender's real next frame still plays.
    client._on_packet(*_audio(client, cipher, 101), LIVE)
    assert len(played) >= 2


def test_old_replay_logs_a_warning(caplog):
    client, cipher, _ = _make_client()
    old = _audio(client, cipher, 1)
    client._on_packet(*old, LIVE)
    client._on_packet(*_audio(client, cipher, 5000), LIVE)
    with caplog.at_level(logging.WARNING):
        client._on_packet(*old, ATTACKER)
    assert "likely replayed" in caplog.text
