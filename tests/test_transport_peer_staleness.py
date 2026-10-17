"""Verifies MeshTransport.prune_stale(): second-hand-sourced peer entries
(e.g. PEX "gossip-*" keys, which carry no sender identity) must expire if
not refreshed, so a peer that moves/restarts doesn't leave a permanent ghost
entry inflating the peer count - but static config peers never expire."""
from __future__ import annotations
import time

from outpost.config import Peer
from outpost.transport import MeshTransport


def _make_transport(static_peers=None) -> MeshTransport:
    return MeshTransport(sender_id=1, peers=static_peers or [], on_packet=lambda *a: None)


def test_prune_stale_removes_unrefreshed_learned_peer():
    transport = _make_transport()
    transport.add_peer("gossip-10.0.0.5:9000", Peer(host="10.0.0.5", port=9000))

    transport._last_seen["gossip-10.0.0.5:9000"] = time.monotonic() - 1000
    transport.prune_stale(max_age_s=1.0)

    assert len(transport.peers) == 0


def test_prune_stale_keeps_recently_refreshed_peer():
    transport = _make_transport()
    transport.add_peer("peer-7", Peer(host="10.0.0.6", port=9001))

    transport.prune_stale(max_age_s=1000.0)

    assert len(transport.peers) == 1


def test_prune_stale_never_removes_static_peers():
    transport = _make_transport(static_peers=[Peer(host="10.0.0.1", port=9000)])

    transport._last_seen["static-0"] = time.monotonic() - 1000
    transport.prune_stale(max_age_s=1.0)

    assert len(transport.peers) == 1


def test_add_peer_refreshes_last_seen_on_repeat_sighting():
    transport = _make_transport()
    transport.add_peer("peer-7", Peer(host="10.0.0.6", port=9001))
    transport._last_seen["peer-7"] = time.monotonic() - 1000

    transport.add_peer("peer-7", Peer(host="10.0.0.6", port=9001))  # re-sighted
    transport.prune_stale(max_age_s=1.0)

    assert len(transport.peers) == 1
