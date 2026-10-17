"""Verifies AUDIO/STOP traffic is targeted only at peers that have proven
(via an authenticated channel-announce beacon) they hold a given channel's
key, instead of flooding every peer the node happens to know about - the
mechanism that keeps per-transmission bandwidth from scaling with total mesh
size rather than just channel membership."""
from __future__ import annotations
import asyncio

from outpost.client import OutpostClient
from outpost.config import OutpostConfig, Channel, Peer
from outpost.crypto import generate_channel_key
from outpost.protocol import TYPE_AUDIO, TYPE_CHANNEL_ANNOUNCE


def _make_client(sender_id: int, channels: dict, active_channel_name: str) -> OutpostClient:
    config = OutpostConfig(
        sender_id=sender_id,
        listen_port=0,
        mesh_key=generate_channel_key(),
        channels=channels,
        enable_mdns=False,
        enable_pex=False,
    )
    return OutpostClient(config, active_channel_name)


def _spy_on_packet(client: OutpostClient, received: list[int]) -> None:
    """Must be installed before transport.start() - MeshTransport binds the
    callback into its asyncio protocol at start() time, so reassigning
    afterward has no effect (see tests/test_pex.py for the same gotcha)."""
    original = client._on_packet

    def wrapper(header, ciphertext, addr):
        received.append(header.ptype)
        original(header, ciphertext, addr)

    client.transport._on_packet = wrapper


def test_audio_is_targeted_only_at_announced_channel_members():
    asyncio.run(_audio_is_targeted_only_at_announced_channel_members())


async def _audio_is_targeted_only_at_announced_channel_members():
    ops_key = generate_channel_key()
    other_key = generate_channel_key()

    client_a = _make_client(1, {1: Channel(id=1, name="ops", key=ops_key)}, "ops")
    client_b = _make_client(2, {1: Channel(id=1, name="ops", key=ops_key)}, "ops")
    client_c = _make_client(3, {2: Channel(id=2, name="other", key=other_key)}, "other")

    received_by_b: list[int] = []
    received_by_c: list[int] = []
    _spy_on_packet(client_b, received_by_b)
    _spy_on_packet(client_c, received_by_c)

    # Exercise only the transport/membership/targeting logic - no mic/speaker
    # hardware needed.
    await client_a.transport.start(0)
    await client_b.transport.start(0)
    await client_c.transport.start(0)

    try:
        port_b = client_b.transport._transport.get_extra_info("sockname")[1]
        port_c = client_c.transport._transport.get_extra_info("sockname")[1]

        # A knows both B (an ops member) and C (not an ops member) as plain
        # peers - simulating peers learned via PEX/mDNS for unrelated reasons.
        client_a.transport.add_peer("static-b", Peer(host="127.0.0.1", port=port_b))
        client_a.transport.add_peer("static-c", Peer(host="127.0.0.1", port=port_c))

        # Before any announce has been seen, fall back to flooding everyone
        # known (safe bootstrap default) rather than silently dropping audio.
        assert client_a._channel_target_peers(1) is None
        seq0 = client_a.transport.next_seq()
        client_a.transport.send(1, seq0, b"placeholder-ciphertext", ptype=TYPE_AUDIO,
                                 targets=client_a._channel_target_peers(1))
        await asyncio.sleep(0.3)
        assert TYPE_AUDIO in received_by_b
        assert TYPE_AUDIO in received_by_c  # bootstrap flood reaches non-members too

        # B (a real ops member) authenticates itself via a channel announce.
        cipher_b = client_b.ciphers[1]
        seq = client_b.transport.next_seq()
        ciphertext = cipher_b.encrypt(2, seq, b"")
        client_a.transport.add_peer("peer-2", Peer(host="127.0.0.1", port=port_b))
        client_b.transport.send(1, seq, ciphertext, ptype=TYPE_CHANNEL_ANNOUNCE, targets=[
            Peer(host="127.0.0.1", port=client_a.transport._transport.get_extra_info("sockname")[1]),
        ])

        deadline = asyncio.get_running_loop().time() + 5
        while client_a._channel_target_peers(1) is None and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.1)

        targets = client_a._channel_target_peers(1)
        assert targets is not None, "A never recorded B as a confirmed ops member"
        target_ports = {p.port for p in targets}
        assert target_ports == {port_b}, "membership-scoped targets must exclude non-members like C"

        # Now a real AUDIO send only reaches the confirmed member B, not C.
        received_by_b.clear()
        received_by_c.clear()
        cipher_a = client_a.ciphers[1]
        seq2 = client_a.transport.next_seq()
        ciphertext2 = cipher_a.encrypt(1, seq2, b"fake-opus-bytes")
        client_a.transport.send(1, seq2, ciphertext2, ptype=TYPE_AUDIO, targets=targets)
        await asyncio.sleep(0.3)
        assert TYPE_AUDIO in received_by_b
        assert TYPE_AUDIO not in received_by_c
    finally:
        client_a.transport.stop()
        client_b.transport.stop()
        client_c.transport.stop()
