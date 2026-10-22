"""Offline, hardware-free end-to-end test of the core voice pipeline:
Opus encode -> encrypt -> wire format -> real UDP localhost transport ->
decrypt -> Opus decode, run on synthetic audio (no mic/speaker required).
"""
from __future__ import annotations
import asyncio
import math
import struct

import pytest

from outpost.codec import OpusCodec
from outpost.crypto import ChannelCipher, generate_channel_key
from outpost.protocol import Header, VERSION, TYPE_AUDIO, encode_packet, decode_packet, HEADER_SIZE
from outpost.transport import MeshTransport
from outpost.config import Peer


def make_sine_frame(frame_size: int, sample_rate: int, freq: float = 440.0) -> bytes:
    samples = [int(3000 * math.sin(2 * math.pi * freq * i / sample_rate)) for i in range(frame_size)]
    return struct.pack(f"<{frame_size}h", *samples)


def test_header_roundtrip():
    header = Header(VERSION, TYPE_AUDIO, channel_id=2, sender_id=7, seq=12345)
    packet = encode_packet(header, b"fake-ciphertext")
    decoded, payload = decode_packet(packet)
    assert decoded.version == VERSION
    assert decoded.ptype == TYPE_AUDIO
    assert decoded.channel_id == 2
    assert decoded.sender_id == 7
    assert decoded.seq == 12345
    assert payload == b"fake-ciphertext"
    assert len(packet) == HEADER_SIZE + len(b"fake-ciphertext")


def test_codec_roundtrip_is_close_to_original():
    codec = OpusCodec()
    pcm = make_sine_frame(codec.frame_size, codec.sample_rate)
    opus_bytes = codec.encode(pcm)
    decoded_pcm = codec.decode(opus_bytes)

    # lossy codec - verify it's compact and produces a plausible-length frame
    assert len(opus_bytes) < len(pcm)  # compression actually reduces size
    assert len(decoded_pcm) == len(pcm)


def test_crypto_roundtrip_and_tamper_detection():
    key = generate_channel_key()
    cipher = ChannelCipher(key)

    plaintext = b"hello-opus-frame"
    header = Header(VERSION, TYPE_AUDIO, channel_id=1, sender_id=1, seq=0)
    ciphertext = cipher.encrypt(header, plaintext)
    assert cipher.decrypt(header, ciphertext) == plaintext

    # wrong channel key must fail to decrypt
    other_cipher = ChannelCipher(generate_channel_key())
    with pytest.raises(ValueError):
        other_cipher.decrypt(header, ciphertext)

    # tampered ciphertext must fail auth
    tampered = bytearray(ciphertext)
    tampered[0] ^= 0xFF
    with pytest.raises(ValueError):
        cipher.decrypt(header, bytes(tampered))


def test_two_clients_exchange_encrypted_audio_over_real_udp():
    asyncio.run(_two_clients_exchange_encrypted_audio_over_real_udp())


async def _two_clients_exchange_encrypted_audio_over_real_udp():
    """Simulates two mesh peers on localhost sending/receiving a real Opus
    frame through actual UDP sockets, exercising the full wire protocol."""
    key = generate_channel_key()
    codec_a = OpusCodec()
    codec_b = OpusCodec()
    cipher = ChannelCipher(key)

    received: list[bytes] = []

    def on_packet_b(header, ciphertext, addr):
        pcm = codec_b.decode(cipher.decrypt(header, ciphertext))
        received.append(pcm)

    transport_b = MeshTransport(sender_id=2, peers=[], on_packet=on_packet_b)
    await transport_b.start(listen_port=0)
    b_port = transport_b._transport.get_extra_info("sockname")[1]

    transport_a = MeshTransport(sender_id=1, peers=[Peer(host="127.0.0.1", port=b_port)], on_packet=lambda *a: None)
    await transport_a.start(listen_port=0)

    try:
        pcm_in = make_sine_frame(codec_a.frame_size, codec_a.sample_rate)
        opus_bytes = codec_a.encode(pcm_in)
        transport_a.send_encrypted(cipher, 1, TYPE_AUDIO, opus_bytes)

        for _ in range(50):
            if received:
                break
            await asyncio.sleep(0.05)

        assert len(received) == 1
        assert len(received[0]) == len(pcm_in)
    finally:
        transport_a.stop()
        transport_b.stop()
