"""Verifies MeshTransport.add_peer_group(): a single logical peer reachable
via several candidate addresses (e.g. a dual-homed device on both a VPN
tunnel and the local subnet) counts as one peer and fans out to one address,
falling back to the next candidate on a socket-level send failure."""
from __future__ import annotations

from outpost.config import Peer
from outpost.transport import MeshTransport


class _FakeDatagramTransport:
    """Records sendto calls; can be told to fail for specific addresses."""

    def __init__(self, unreachable: set[tuple[str, int]] = frozenset()):
        self.unreachable = set(unreachable)
        self.sent: list[tuple[str, int]] = []

    def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
        if addr in self.unreachable:
            raise OSError("network is unreachable")
        self.sent.append(addr)


def _make_transport() -> MeshTransport:
    return MeshTransport(sender_id=1, peers=[], on_packet=lambda *a: None)


def test_add_peer_group_counts_as_one_peer():
    transport = _make_transport()
    transport.add_peer_group("device-a", [
        Peer(host="192.168.1.50", port=9000),
        Peer(host="10.8.0.5", port=9000),
    ])

    assert len(transport.peers) == 1
    assert transport.peers[0] == Peer(host="192.168.1.50", port=9000)


def test_send_falls_back_to_secondary_candidate_on_failure():
    transport = _make_transport()
    transport.add_peer_group("device-a", [
        Peer(host="192.168.1.50", port=9000),
        Peer(host="10.8.0.5", port=9000),
    ])
    fake = _FakeDatagramTransport(unreachable={("192.168.1.50", 9000)})
    transport._transport = fake

    transport.send(channel_id=1, seq=0, ciphertext=b"x")

    assert fake.sent == [("10.8.0.5", 9000)]


def test_send_promotes_fallback_so_later_sends_skip_dead_primary():
    transport = _make_transport()
    transport.add_peer_group("device-a", [
        Peer(host="192.168.1.50", port=9000),
        Peer(host="10.8.0.5", port=9000),
    ])
    fake = _FakeDatagramTransport(unreachable={("192.168.1.50", 9000)})
    transport._transport = fake

    transport.send(channel_id=1, seq=0, ciphertext=b"x")
    transport.send(channel_id=1, seq=1, ciphertext=b"y")

    assert fake.sent == [("10.8.0.5", 9000), ("10.8.0.5", 9000)]
    assert transport.peers == [Peer(host="10.8.0.5", port=9000)]


def test_confirm_peer_ties_unidentified_group_to_sender_id():
    # mDNS (or any address-only source) saw a device's two interfaces as one
    # group before we ever knew its real identity.
    transport = _make_transport()
    transport.add_peer_group("outpost-42._outpost._udp.local.", [
        Peer(host="192.168.1.50", port=9000),
        Peer(host="10.8.0.5", port=9000),
    ])
    assert len(transport.peers) == 1

    # Authenticated traffic arrives from sender_id 42 at one of those addresses.
    transport.confirm_peer(42, Peer(host="192.168.1.50", port=9000))

    # Still just one logical peer, but now keyed/owned by the confirmed identity.
    assert len(transport.peers) == 1
    assert transport.peers[0] == Peer(host="192.168.1.50", port=9000)
    assert transport._candidates["peer-42"] == [
        Peer(host="192.168.1.50", port=9000),
        Peer(host="10.8.0.5", port=9000),
    ]
    assert "outpost-42._outpost._udp.local." not in transport._peers

    # The merged-in fallback candidate still works after the tie-in.
    fake = _FakeDatagramTransport(unreachable={("192.168.1.50", 9000)})
    transport._transport = fake
    transport.send(channel_id=1, seq=0, ciphertext=b"x")
    assert fake.sent == [("10.8.0.5", 9000)]


def test_confirm_peer_new_address_does_not_create_duplicate():
    transport = _make_transport()
    transport.confirm_peer(7, Peer(host="10.0.0.9", port=9000))
    transport.confirm_peer(7, Peer(host="10.0.0.9", port=9000))

    assert len(transport.peers) == 1


def test_peer_details_reports_address_candidate_count_and_static_flag():
    transport = MeshTransport(sender_id=1, peers=[Peer(host="10.0.0.1", port=9000)], on_packet=lambda *a: None)
    transport.add_peer_group("device-a", [
        Peer(host="192.168.1.50", port=9000),
        Peer(host="10.8.0.5", port=9000),
    ])

    details = {d["key"]: d for d in transport.peer_details()}
    assert details["static-0"]["static"] is True
    assert details["static-0"]["candidate_count"] == 1
    assert details["device-a"]["static"] is False
    assert details["device-a"]["host"] == "192.168.1.50"
    assert details["device-a"]["candidate_count"] == 2
    assert details["device-a"]["age_s"] >= 0
