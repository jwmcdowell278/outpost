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


def test_confirming_a_static_peer_keeps_its_prune_exemption():
    # Regression test: confirm_peer used to always rename a merged donor
    # entry to "peer-<sender_id>", even when the donor was a protected
    # "static-" entry - so the very first authenticated packet from a
    # static peer silently stripped its "never prune" protection, and it
    # could later be dropped entirely (with no static fallback left to
    # reseed discovery) just from a transient gap in traffic.
    transport = _make_transport(static_peers=[Peer(host="10.0.0.1", port=9000)])

    transport.confirm_peer(7, Peer(host="10.0.0.1", port=9000))

    assert len(transport.peers) == 1
    details = {d["key"]: d for d in transport.peer_details()}
    assert details["static-7"]["static"] is True

    transport._last_seen["static-7"] = time.monotonic() - 1000
    transport.prune_stale(max_age_s=1.0)
    assert len(transport.peers) == 1  # still protected, not pruned


def test_repeat_confirmation_of_a_tied_static_peer_does_not_churn_the_table():
    # confirm_peer runs on every authenticated packet (e.g. every audio
    # frame) - a repeat confirmation for an already-tied static peer must
    # not treat its own settled entry as a fresh "donor" each time.
    transport = _make_transport(static_peers=[Peer(host="10.0.0.1", port=9000)])
    transport.confirm_peer(7, Peer(host="10.0.0.1", port=9000))

    for _ in range(5):
        transport.confirm_peer(7, Peer(host="10.0.0.1", port=9000))

    assert len(transport.peers) == 1
    assert list(transport._peers.keys()) == ["static-7"]
