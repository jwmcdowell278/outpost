"""Verifies relay/bridge mode (`enable_relay`): a node blind-floods
AUDIO/STOP/CHANNEL_ANNOUNCE packets to every other known peer, keyed only on
the cleartext header - bridging two otherwise-unreachable peer groups (e.g.
a local mesh segment and a WireGuard-connected uplink) without ever needing
that channel's key. PEX is deliberately NOT relayed (see client.py)."""
from __future__ import annotations
import asyncio

from outpost.client import OutpostClient
from outpost.config import OutpostConfig, Channel, Peer
from outpost.crypto import generate_channel_key
from outpost.protocol import Header, VERSION, TYPE_AUDIO, TYPE_PEX


def _make_client(sender_id: int, channel_key: bytes, active_channel_name: str = "ops",
                  enable_relay: bool = False) -> OutpostClient:
    config = OutpostConfig(
        sender_id=sender_id,
        listen_port=0,
        mesh_key=generate_channel_key(),
        channels={1: Channel(id=1, name=active_channel_name, key=channel_key)},
        enable_mdns=False,
        enable_pex=False,
        enable_relay=enable_relay,
    )
    return OutpostClient(config, active_channel_name)


def _spy_on_packet(client: OutpostClient, received: list[int]) -> None:
    original = client._on_packet

    def wrapper(header, ciphertext, addr):
        received.append(header.ptype)
        original(header, ciphertext, addr)

    client.transport._on_packet = wrapper


async def _start_all(*clients: OutpostClient):
    for c in clients:
        await c.transport.start(0)


def _port_of(client: OutpostClient) -> int:
    return client.transport._transport.get_extra_info("sockname")[1]


def test_relay_bridges_two_peers_that_do_not_know_each_other():
    asyncio.run(_relay_bridges_two_peers_that_do_not_know_each_other())


async def _relay_bridges_two_peers_that_do_not_know_each_other():
    # A different channel key for the relay than A/C use - proves the relay
    # forwards blind, without needing this channel's key.
    shared_key = generate_channel_key()
    client_a = _make_client(1, shared_key)
    relay = _make_client(99, generate_channel_key(), active_channel_name="relay-local", enable_relay=True)
    client_c = _make_client(3, shared_key)

    received_by_c: list[int] = []
    _spy_on_packet(client_c, received_by_c)

    await _start_all(client_a, relay, client_c)
    try:
        # A only knows the relay (simulating a field unit with no route to
        # HQ directly); the relay knows both A and C.
        relay.transport.add_peer("a", Peer(host="127.0.0.1", port=_port_of(client_a)))
        relay.transport.add_peer("c", Peer(host="127.0.0.1", port=_port_of(client_c)))

        cipher_a = client_a.ciphers[1]
        client_a.transport.send_encrypted(cipher_a, 1, TYPE_AUDIO, b"opus-frame",
                                           targets=[Peer(host="127.0.0.1", port=_port_of(relay))])

        await asyncio.sleep(0.3)
        assert TYPE_AUDIO in received_by_c
    finally:
        client_a.transport.stop()
        relay.transport.stop()
        client_c.transport.stop()


def test_relay_disabled_by_default_does_not_forward():
    asyncio.run(_relay_disabled_by_default_does_not_forward())


async def _relay_disabled_by_default_does_not_forward():
    shared_key = generate_channel_key()
    client_a = _make_client(1, shared_key)
    non_relay = _make_client(99, generate_channel_key(), active_channel_name="relay-local", enable_relay=False)
    client_c = _make_client(3, shared_key)

    received_by_c: list[int] = []
    _spy_on_packet(client_c, received_by_c)

    await _start_all(client_a, non_relay, client_c)
    try:
        non_relay.transport.add_peer("a", Peer(host="127.0.0.1", port=_port_of(client_a)))
        non_relay.transport.add_peer("c", Peer(host="127.0.0.1", port=_port_of(client_c)))

        cipher_a = client_a.ciphers[1]
        client_a.transport.send_encrypted(cipher_a, 1, TYPE_AUDIO, b"opus-frame",
                                           targets=[Peer(host="127.0.0.1", port=_port_of(non_relay))])

        await asyncio.sleep(0.3)
        assert TYPE_AUDIO not in received_by_c
    finally:
        client_a.transport.stop()
        non_relay.transport.stop()
        client_c.transport.stop()


def test_relay_does_not_forward_pex():
    asyncio.run(_relay_does_not_forward_pex())


async def _relay_does_not_forward_pex():
    shared_key = generate_channel_key()
    client_a = _make_client(1, shared_key)
    relay = _make_client(99, generate_channel_key(), active_channel_name="relay-local", enable_relay=True)
    client_c = _make_client(3, shared_key)

    received_by_c: list[int] = []
    _spy_on_packet(client_c, received_by_c)

    await _start_all(client_a, relay, client_c)
    try:
        relay.transport.add_peer("a", Peer(host="127.0.0.1", port=_port_of(client_a)))
        relay.transport.add_peer("c", Peer(host="127.0.0.1", port=_port_of(client_c)))

        seq = client_a.transport.next_seq()
        client_a.transport.send(0, seq, b"fake-pex-ciphertext", ptype=TYPE_PEX,
                                 targets=[Peer(host="127.0.0.1", port=_port_of(relay))])

        await asyncio.sleep(0.3)
        assert TYPE_PEX not in received_by_c
    finally:
        client_a.transport.stop()
        relay.transport.stop()
        client_c.transport.stop()


def test_relay_deduplicates_repeated_sequence_numbers():
    asyncio.run(_relay_deduplicates_repeated_sequence_numbers())


async def _relay_deduplicates_repeated_sequence_numbers():
    shared_key = generate_channel_key()
    client_a = _make_client(1, shared_key)
    relay = _make_client(99, generate_channel_key(), active_channel_name="relay-local", enable_relay=True)
    client_c = _make_client(3, shared_key)

    received_by_c: list[int] = []
    _spy_on_packet(client_c, received_by_c)

    await _start_all(client_a, relay, client_c)
    try:
        relay.transport.add_peer("a", Peer(host="127.0.0.1", port=_port_of(client_a)))
        relay.transport.add_peer("c", Peer(host="127.0.0.1", port=_port_of(client_c)))

        cipher_a = client_a.ciphers[1]
        seq = client_a.transport.next_seq()
        header = Header(VERSION, TYPE_AUDIO, 1, client_a.config.sender_id, seq)
        ciphertext = cipher_a.encrypt(header, b"opus-frame")
        for _ in range(3):  # same (sender_id, seq) resent repeatedly
            client_a.transport.send(1, seq, ciphertext, ptype=TYPE_AUDIO,
                                     targets=[Peer(host="127.0.0.1", port=_port_of(relay))])

        await asyncio.sleep(0.3)
        assert received_by_c.count(TYPE_AUDIO) == 1
    finally:
        client_a.transport.stop()
        relay.transport.stop()
        client_c.transport.stop()
