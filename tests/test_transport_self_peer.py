"""Regression tests for a real-world bug found via the new log_file feature:
a PEX gossip message naturally includes our own address (we're one of our
gossip partner's own peers), so without filtering we'd add ourselves as a
peer. With relay mode on, that makes us bounce our own relayed packets back
to ourselves - mostly harmless for AUDIO (seq-deduped), but TYPE_STOP has no
such dedup, so a looped-back STOP blindly called confirm_peer() with our own
address, silently overwriting a real peer's trusted send address with our
own and black-holing all further outbound traffic to them (observed live as
"both nodes stopped receiving audio" after a flood of
"dropping stale/duplicate/out-of-order audio" log lines)."""
from __future__ import annotations

from outpost.config import Peer
from outpost.transport import MeshTransport


def _make_transport(listen_port=5555) -> MeshTransport:
    transport = MeshTransport(sender_id=1, peers=[], on_packet=lambda *a: None)
    transport.listen_port = listen_port  # normally set by start(), skipped here to avoid a real socket
    return transport


def test_add_peer_group_ignores_our_own_loopback_address():
    transport = _make_transport(listen_port=5555)

    transport.add_peer("gossip-127.0.0.1:5555", Peer(host="127.0.0.1", port=5555))

    assert transport.peers == []


def test_add_peer_group_still_adds_a_real_remote_address():
    transport = _make_transport(listen_port=5555)

    transport.add_peer("gossip-192.168.1.50:5555", Peer(host="192.168.1.50", port=5555))

    assert transport.peers == [Peer(host="192.168.1.50", port=5555)]


def test_same_host_different_port_is_not_treated_as_our_own_address():
    # Only our own listen_port counts - some other service on localhost
    # must not be mistaken for ourselves.
    transport = _make_transport(listen_port=5555)

    transport.add_peer("gossip-127.0.0.1:9999", Peer(host="127.0.0.1", port=9999))

    assert transport.peers == [Peer(host="127.0.0.1", port=9999)]


def test_confirm_peer_does_not_overwrite_a_real_peers_address_with_our_own():
    """The core regression: once a real peer's address is confirmed, a
    later confirm_peer() call claiming that same sender_id is reachable at
    OUR OWN address (e.g. from a relay loop-back of their STOP packet) must
    be ignored, not silently adopted as their new trusted address."""
    transport = _make_transport(listen_port=5555)
    transport.confirm_peer(42, Peer(host="192.168.1.50", port=5555))
    assert transport.peers == [Peer(host="192.168.1.50", port=5555)]

    transport.confirm_peer(42, Peer(host="127.0.0.1", port=5555))

    assert transport.peers == [Peer(host="192.168.1.50", port=5555)]


def test_confirm_peer_ignores_our_own_address_for_a_brand_new_sender():
    transport = _make_transport(listen_port=5555)

    transport.confirm_peer(99, Peer(host="127.0.0.1", port=5555))

    assert transport.peers == []
