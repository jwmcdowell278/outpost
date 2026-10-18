"""Verifies the optional sender alias (`sender_alias`): a START packet is
sent once per PTT press (symmetric with the existing once-per-release STOP
packet) carrying the sender's alias, so peers can show a human-readable
name alongside/instead of the raw numeric sender_id - see protocol.py's
TYPE_START docs and client.py's `set_ptt`/`_on_packet`/`speaking_details`."""
from __future__ import annotations

from outpost.client import OutpostClient
from outpost.config import OutpostConfig, Channel
from outpost.crypto import ChannelCipher, generate_channel_key
from outpost.protocol import Header, VERSION, TYPE_AUDIO, TYPE_START, TYPE_STOP


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


def _deliver_start(client, key, sender_id=2, seq=0, alias: str | None = "Basecamp"):
    cipher = ChannelCipher(key)
    payload = alias.encode("utf-8") if alias else b""
    ciphertext = cipher.encrypt(sender_id, seq, payload)
    header = Header(VERSION, TYPE_START, channel_id=1, sender_id=sender_id, seq=seq)
    client._on_packet(header, ciphertext, ("127.0.0.1", 9999))


def _deliver_audio(client, key, sender_id=2, seq=0):
    silence = b"\x00" * (client.codec.frame_size * 2)
    opus_bytes = client.codec.encode(silence)
    cipher = ChannelCipher(key)
    ciphertext = cipher.encrypt(sender_id, seq, opus_bytes)
    header = Header(VERSION, TYPE_AUDIO, channel_id=1, sender_id=sender_id, seq=seq)
    client._on_packet(header, ciphertext, ("127.0.0.1", 9999))


def _deliver_stop(client, key, sender_id=2, seq=1):
    cipher = ChannelCipher(key)
    ciphertext = cipher.encrypt(sender_id, seq, b"")
    header = Header(VERSION, TYPE_STOP, channel_id=1, sender_id=sender_id, seq=seq)
    client._on_packet(header, ciphertext, ("127.0.0.1", 9999))


def test_sender_alias_is_none_by_default():
    client, _ = _make_client()
    assert client.sender_alias is None


def test_ptt_press_sends_start_packet_with_alias():
    client, ops_key = _make_client(sender_alias="Basecamp")
    sent = []
    client.transport.send = lambda *args, **kwargs: sent.append((args, kwargs))

    client.set_ptt(True)

    assert len(sent) == 1
    args, kwargs = sent[0]
    channel_id, seq, ciphertext = args[0], args[1], args[2]
    assert kwargs["ptype"] == TYPE_START
    cipher = ChannelCipher(ops_key)
    payload = cipher.decrypt(client.config.sender_id, seq, ciphertext)
    assert payload == b"Basecamp"


def test_ptt_press_sends_empty_start_payload_when_no_alias_set():
    client, ops_key = _make_client()
    sent = []
    client.transport.send = lambda *args, **kwargs: sent.append((args, kwargs))

    client.set_ptt(True)

    args, kwargs = sent[0]
    seq, ciphertext = args[1], args[2]
    assert kwargs["ptype"] == TYPE_START
    cipher = ChannelCipher(ops_key)
    payload = cipher.decrypt(client.config.sender_id, seq, ciphertext)
    assert payload == b""


def test_ptt_release_still_sends_stop_packet():
    client, ops_key = _make_client(sender_alias="Basecamp")
    sent = []
    client.transport.send = lambda *args, **kwargs: sent.append((args, kwargs))

    client.set_ptt(True)
    client.set_ptt(False)

    assert [kwargs["ptype"] for _, kwargs in sent] == [TYPE_START, TYPE_STOP]


def test_ptt_edge_with_unstarted_transport_does_not_raise():
    # transport.send() raises RuntimeError until transport.start() has run -
    # set_ptt must not let that escape into the PTTSource callback.
    client, _ = _make_client(sender_alias="Basecamp")
    client.set_ptt(True)
    assert client._ptt_active is True
    client.set_ptt(False)
    assert client._ptt_active is False


def test_receiving_start_packet_populates_speaking_details():
    client, ops_key = _make_client()
    _deliver_start(client, ops_key, sender_id=2, alias="Basecamp")

    details = client.speaking_details()

    assert details == [{"sender_id": 2, "alias": "Basecamp", "age_s": 0.0}]


def test_receiving_start_packet_with_empty_alias_reports_none():
    client, ops_key = _make_client()
    _deliver_start(client, ops_key, sender_id=2, alias=None)

    details = client.speaking_details()

    assert details == [{"sender_id": 2, "alias": None, "age_s": 0.0}]


def test_unauthenticated_start_packet_is_dropped():
    client, _ops_key = _make_client()
    other_key = generate_channel_key()
    _deliver_start(client, other_key, sender_id=2, alias="Intruder")

    assert client.speaking_details() == []


def test_stop_packet_clears_speaking_details():
    client, ops_key = _make_client()
    _deliver_start(client, ops_key, sender_id=2, alias="Basecamp")
    _deliver_stop(client, ops_key, sender_id=2)

    assert client.speaking_details() == []


def test_audio_without_start_still_reports_speaking_with_no_alias():
    client, ops_key = _make_client()
    _deliver_audio(client, ops_key, sender_id=2)

    details = client.speaking_details()

    assert details == [{"sender_id": 2, "alias": None, "age_s": 0.0}]


def test_audio_after_start_preserves_known_alias():
    client, ops_key = _make_client()
    _deliver_start(client, ops_key, sender_id=2, alias="Basecamp")
    _deliver_audio(client, ops_key, sender_id=2, seq=2)

    details = client.speaking_details()

    assert details == [{"sender_id": 2, "alias": "Basecamp", "age_s": 0.0}]


def test_speaking_details_excludes_stale_entries():
    client, ops_key = _make_client()
    _deliver_start(client, ops_key, sender_id=2, alias="Basecamp")

    # Fake an elapsed timeout instead of a real sleep - avoids a flaky/slow test.
    alias, _last_seen = client._channel_speaking[1][2]
    client._channel_speaking[1][2] = (alias, client._channel_speaking[1][2][1] - 10.0)

    assert client.speaking_details() == []
