"""Verifies the soft half-duplex lockout (`enable_tx_lockout`): a local PTT
press is ignored while another peer's audio on the active channel was
received recently, cleared immediately on that peer's STOP, and has no
effect at all unless explicitly enabled."""
from __future__ import annotations

from outpost.client import OutpostClient
from outpost.config import OutpostConfig, Channel
from outpost.crypto import ChannelCipher, generate_channel_key
from outpost.protocol import Header, VERSION, TYPE_AUDIO, TYPE_STOP


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


def _deliver_audio(client: OutpostClient, key: bytes, sender_id: int = 2, seq: int = 0) -> None:
    # Must be real Opus data the client's decoder can decode (not just any
    # bytes) - _on_packet decodes and plays it as part of handling AUDIO.
    silence = b"\x00" * (client.codec.frame_size * 2)
    opus_bytes = client.codec.encode(silence)
    cipher = ChannelCipher(key)
    ciphertext = cipher.encrypt(sender_id, seq, opus_bytes)
    header = Header(VERSION, TYPE_AUDIO, channel_id=1, sender_id=sender_id, seq=seq)
    client._on_packet(header, ciphertext, ("127.0.0.1", 9999))


def _deliver_stop(client: OutpostClient, key: bytes, sender_id: int = 2, seq: int = 1) -> None:
    cipher = ChannelCipher(key)
    ciphertext = cipher.encrypt(sender_id, seq, b"")
    header = Header(VERSION, TYPE_STOP, channel_id=1, sender_id=sender_id, seq=seq)
    client._on_packet(header, ciphertext, ("127.0.0.1", 9999))


def test_tx_lockout_disabled_by_default():
    client, _ = _make_client()
    assert client.enable_tx_lockout is False


def test_disabled_lockout_never_blocks_ptt():
    client, key = _make_client(enable_tx_lockout=False)
    _deliver_audio(client, key)
    client.set_ptt(True)
    assert client._ptt_active is True


def test_enabled_lockout_blocks_ptt_while_channel_busy():
    client, key = _make_client(enable_tx_lockout=True)
    _deliver_audio(client, key)
    client.set_ptt(True)
    assert client._ptt_active is False


def test_blocked_ptt_plays_audible_lockout_tone():
    client, key = _make_client(enable_tx_lockout=True)
    _deliver_audio(client, key)
    played: list[bytes] = []
    client.speaker.play_frame = played.append

    client.set_ptt(True)

    assert client._ptt_active is False
    assert played == client._lockout_tone_frames
    assert len(played) > 0


def test_allowed_ptt_does_not_play_lockout_tone():
    client, _key = _make_client(enable_tx_lockout=True)
    played: list[bytes] = []
    client.speaker.play_frame = played.append

    client.set_ptt(True)

    assert client._ptt_active is True
    assert played == []


def test_enabled_lockout_allows_ptt_when_channel_is_quiet():
    client, _key = _make_client(enable_tx_lockout=True)
    client.set_ptt(True)
    assert client._ptt_active is True


def test_stop_packet_clears_busy_state_immediately():
    client, key = _make_client(enable_tx_lockout=True, tx_lockout_timeout_ms=60_000)
    _deliver_audio(client, key)
    assert client.channel_busy() is True

    _deliver_stop(client, key)
    assert client.channel_busy() is False

    client.set_ptt(True)
    assert client._ptt_active is True


def test_channel_busy_expires_after_timeout():
    client, key = _make_client(enable_tx_lockout=True, tx_lockout_timeout_ms=1.0)
    _deliver_audio(client, key)
    assert client.channel_busy() is True

    # Fake an elapsed timeout instead of a real sleep - avoids a flaky/slow test.
    client._channel_last_audio[1] -= 1.0
    assert client.channel_busy() is False
