"""Tests for gossip-based peer exchange (PEX) - the discovery mechanism that
works over VPN mesh and multi-hop mesh radio where mDNS/multicast can't
reach, as opposed to tests/test_discovery.py which covers mDNS."""
from __future__ import annotations
import asyncio
import time

from outpost.config import Peer
from outpost.crypto import generate_channel_key
from outpost.pex import pack_peers, unpack_peers, PexGossip
from outpost.transport import MeshTransport


def test_pack_unpack_roundtrip_ipv4_and_ipv6():
    peers = [
        Peer(host="192.168.1.50", port=5555),
        Peer(host="10.0.0.2", port=6000),
        Peer(host="fd00::1", port=5555),
    ]
    data = pack_peers(peers)
    assert unpack_peers(data) == peers


def test_pack_skips_unresolvable_host():
    peers = [Peer(host="not-an-ip-literal.example", port=5555), Peer(host="192.168.1.1", port=1)]
    data = pack_peers(peers)
    assert unpack_peers(data) == [Peer(host="192.168.1.1", port=1)]


def test_unpack_ignores_truncated_garbage():
    assert unpack_peers(b"\x04\x01\x02") == []  # claims IPv4 but body is too short


class _FakeTransport:
    """Records send() calls without opening any real sockets - used to check
    PEX's fanout logic in isolation from the network."""

    def __init__(self, peers):
        self._peers = peers
        self._seq = 0
        self.sent_targets: list = []

    @property
    def peers(self):
        return self._peers

    @property
    def live_peers(self):
        return self._peers

    def next_seq(self):
        seq = self._seq
        self._seq += 1
        return seq

    def send(self, channel_id, seq, ciphertext, ptype=None, targets=None):
        self.sent_targets.append(targets)

    def send_encrypted(self, cipher, channel_id, ptype, plaintext, targets=None):
        self.send(channel_id, self.next_seq(), b"", ptype=ptype, targets=targets)


def test_announce_only_targets_a_bounded_fanout_subset_of_peers():
    """With hundreds of known peers, each gossip round must still only send
    to a small bounded subset - this is what keeps PEX control-plane traffic
    from growing quadratically with total mesh size."""
    many_peers = [Peer(host=f"10.0.0.{i}", port=5555) for i in range(1, 201)]
    fake_transport = _FakeTransport(many_peers)
    pex = PexGossip(1, generate_channel_key(), fake_transport, fanout=8)

    pex._announce()

    assert len(fake_transport.sent_targets) == 1
    targets = fake_transport.sent_targets[0]
    assert targets is not None
    assert len(targets) == 8
    assert all(peer in many_peers for peer in targets)


def test_announce_targets_everyone_when_fewer_peers_than_fanout():
    few_peers = [Peer(host="10.0.0.1", port=5555), Peer(host="10.0.0.2", port=5555)]
    fake_transport = _FakeTransport(few_peers)
    pex = PexGossip(1, generate_channel_key(), fake_transport, fanout=8)

    pex._announce()

    assert fake_transport.sent_targets == [few_peers]


def test_probe_sends_directly_to_an_address_not_in_the_peer_table():
    """probe() is how SubnetScanner reaches an address with no prior
    relationship at all - unlike _announce, it must not depend on the
    target already being a known peer."""
    fake_transport = _FakeTransport([])
    pex = PexGossip(1, generate_channel_key(), fake_transport)

    pex.probe("10.0.0.99", 5555)

    assert fake_transport.sent_targets == [[Peer(host="10.0.0.99", port=5555)]]


def test_three_nodes_learn_full_mesh_via_gossip_without_mdns():
    """A bootstrap-only peer link (A<->B) plus gossip should let a
    late-arriving node C - introduced only to B - be learned by A too,
    purely through PEX, with no multicast/mDNS involved at all."""
    asyncio.run(_three_nodes_learn_full_mesh_via_gossip_without_mdns())


async def _three_nodes_learn_full_mesh_via_gossip_without_mdns():
    mesh_key = generate_channel_key()

    # MeshTransport binds its callback at construction time, but PexGossip
    # needs a constructed transport - route through a mutable holder so the
    # callback dispatches to whichever PexGossip instance is set, regardless
    # of construction order.
    handlers: dict[int, object] = {}

    def make_router(sender_id):
        def on_packet(header, ciphertext, addr):
            handlers[sender_id].handle_incoming(header, ciphertext, addr)
        return on_packet

    transport_a = MeshTransport(sender_id=1, peers=[], on_packet=make_router(1))
    transport_b = MeshTransport(sender_id=2, peers=[], on_packet=make_router(2))
    transport_c = MeshTransport(sender_id=3, peers=[], on_packet=make_router(3))

    await transport_a.start(listen_port=0)
    await transport_b.start(listen_port=0)
    await transport_c.start(listen_port=0)

    pex_a = PexGossip(1, mesh_key, transport_a, interval_s=0.2)
    pex_b = PexGossip(2, mesh_key, transport_b, interval_s=0.2)
    pex_c = PexGossip(3, mesh_key, transport_c, interval_s=0.2)
    handlers[1], handlers[2], handlers[3] = pex_a, pex_b, pex_c

    port_a = transport_a._transport.get_extra_info("sockname")[1]
    port_b = transport_b._transport.get_extra_info("sockname")[1]
    port_c = transport_c._transport.get_extra_info("sockname")[1]

    try:
        # Only a single bootstrap link in each direction: A<->B directly
        # configured, C only told about B (simulating "add C near B, after
        # A and B are already deployed").
        transport_a.add_peer("static-0", Peer(host="127.0.0.1", port=port_b))
        transport_b.add_peer("static-0", Peer(host="127.0.0.1", port=port_a))
        transport_c.add_peer("static-0", Peer(host="127.0.0.1", port=port_b))

        pex_a.start()
        pex_b.start()
        pex_c.start()

        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if len(transport_a.peers) >= 2 and len(transport_c.peers) >= 2:
                break
            await asyncio.sleep(0.2)

        a_ports = {p.port for p in transport_a.peers}
        c_ports = {p.port for p in transport_c.peers}
        assert port_b in a_ports and port_c in a_ports, "A never learned about C via gossip through B"
        assert port_b in c_ports and port_a in c_ports, "C never learned about A via gossip through B"
    finally:
        pex_a.stop()
        pex_b.stop()
        pex_c.stop()
        transport_a.stop()
        transport_b.stop()
        transport_c.stop()


def test_pex_rejects_messages_with_wrong_mesh_key():
    asyncio.run(_pex_rejects_messages_with_wrong_mesh_key())


async def _pex_rejects_messages_with_wrong_mesh_key():
    handler_b: dict[str, object] = {}
    transport_a = MeshTransport(sender_id=1, peers=[], on_packet=lambda *a: None)
    transport_b = MeshTransport(
        sender_id=2, peers=[],
        on_packet=lambda header, ct, addr: handler_b["pex"].handle_incoming(header, ct, addr),
    )
    await transport_a.start(listen_port=0)
    await transport_b.start(listen_port=0)

    pex_a = PexGossip(1, generate_channel_key(), transport_a, interval_s=0.1)
    pex_b = PexGossip(2, generate_channel_key(), transport_b, interval_s=0.1)  # different key
    handler_b["pex"] = pex_b

    port_a = transport_a._transport.get_extra_info("sockname")[1]
    port_b = transport_b._transport.get_extra_info("sockname")[1]

    try:
        transport_a.add_peer("static-0", Peer(host="127.0.0.1", port=port_b))
        pex_a._announce()
        await asyncio.sleep(0.3)
        assert transport_b.peers == [], "peer with wrong mesh key should not be learned"
    finally:
        transport_a.stop()
        transport_b.stop()
