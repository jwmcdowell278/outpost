"""The cleartext packet header is authenticated along with the payload
(protocol version 2 - see outpost/crypto.py), so nobody without the key can
alter it - e.g. relabel a captured AUDIO packet as a STOP to cut a speaker
off."""
from __future__ import annotations
import logging
import pytest
from nacl import secret

from outpost.client import OutpostClient
from outpost.config import OutpostConfig, Channel
from outpost.crypto import ChannelCipher, generate_channel_key, derive_nonce
from outpost.protocol import (
    Header, VERSION, TYPE_AUDIO, TYPE_START, TYPE_STOP, encode_packet, decode_packet, UnsupportedVersion,
)
from outpost.transport import _MeshProtocol

KEY = generate_channel_key()


def _sealed(ptype=TYPE_AUDIO, channel_id=1, sender_id=7, seq=100, payload=b"opus"):
    header = Header(VERSION, ptype, channel_id, sender_id, seq)
    return header, ChannelCipher(KEY).encrypt(header, payload)


@pytest.mark.parametrize("field,value", [
    ("ptype", TYPE_STOP), ("ptype", TYPE_START), ("channel_id", 2),
    ("sender_id", 8), ("seq", 101), ("version", 3),
])
def test_changing_any_header_field_fails_authentication(field, value):
    header, ciphertext = _sealed()
    setattr(header, field, value)
    with pytest.raises(ValueError):
        ChannelCipher(KEY).decrypt(header, ciphertext)


def test_untouched_header_decrypts():
    header, ciphertext = _sealed()
    assert ChannelCipher(KEY).decrypt(header, ciphertext) == b"opus"


def test_version_1_ciphertext_under_same_key_is_rejected():
    # Old nodes used the raw key with secretbox; the new format derives its
    # own key, so the two never share one and old packets don't validate.
    nonce = derive_nonce(7, 100)
    old = secret.SecretBox(KEY).encrypt(b"opus", nonce).ciphertext
    with pytest.raises(ValueError):
        ChannelCipher(KEY).decrypt(Header(VERSION, TYPE_AUDIO, 1, 7, 100), old)


def test_relabelled_audio_cannot_cut_off_a_live_speaker():
    client = OutpostClient(OutpostConfig(
        sender_id=1, listen_port=0, mesh_key=generate_channel_key(),
        channels={1: Channel(1, "ops", KEY)}, enable_mdns=False, enable_pex=False,
    ), "ops")
    client.transport.confirm_peer = lambda *a: True
    start_header, start_ct = _sealed(ptype=TYPE_START, seq=99, payload=b"Basecamp")
    client._on_packet(start_header, start_ct, ("10.0.0.2", 5555))

    # Attacker captures an in-flight AUDIO packet and resends it as STOP.
    audio_header, audio_ct = _sealed(seq=100)
    audio_header.ptype = TYPE_STOP
    client._on_packet(audio_header, audio_ct, ("10.0.0.66", 5555))

    assert [s["alias"] for s in client.speaking_details(1)] == ["Basecamp"]


def test_packet_from_other_protocol_version_logs_upgrade_warning_once(caplog):
    received = []
    proto = _MeshProtocol(lambda *a: received.append(a))
    header, ciphertext = _sealed()
    raw = bytearray(encode_packet(header, ciphertext))
    raw[0] = 1  # an un-upgraded node
    with caplog.at_level(logging.WARNING):
        proto.datagram_received(bytes(raw), ("10.0.0.5", 5555))
        proto.datagram_received(bytes(raw), ("10.0.0.5", 5555))
    assert received == []
    assert caplog.text.count("protocol version 1") == 1
    assert "upgrade every node" in caplog.text


def test_decode_raises_unsupported_version():
    raw = bytearray(encode_packet(*_sealed()))
    raw[0] = 1
    with pytest.raises(UnsupportedVersion):
        decode_packet(bytes(raw))
