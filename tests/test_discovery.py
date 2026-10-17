"""Verifies mDNS peer discovery works end to end: two nodes on localhost
find each other automatically, with no static peer config - and a late
joiner (simulating a device added after others are already deployed) is
discovered without restarting the existing node."""
from __future__ import annotations
import time

from outpost.discovery import PeerDiscovery


def _wait_until(predicate, timeout=10.0, interval=0.2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def test_two_nodes_discover_each_other_automatically():
    # on_peer_added now receives one callback per device (not per address) -
    # (name, candidates: list[Peer]), ordered shortest-path-first.
    found_by_a: dict[str, list] = {}
    found_by_b: dict[str, list] = {}

    node_a = PeerDiscovery(
        sender_id=101, listen_port=6101,
        on_peer_added=lambda name, candidates: found_by_a.__setitem__(name, candidates),
        on_peer_removed=lambda name: found_by_a.pop(name, None),
    )
    node_b = PeerDiscovery(
        sender_id=102, listen_port=6102,
        on_peer_added=lambda name, candidates: found_by_b.__setitem__(name, candidates),
        on_peer_removed=lambda name: found_by_b.pop(name, None),
    )

    node_a.start()
    try:
        node_b.start()
        try:
            assert _wait_until(lambda: len(found_by_a) == 1), "node_a never discovered node_b"
            assert _wait_until(lambda: len(found_by_b) == 1), "node_b never discovered node_a"

            candidates_of_b_seen_by_a = next(iter(found_by_a.values()))
            assert candidates_of_b_seen_by_a[0].port == 6102

            # Simulate a third device deployed later, after a/b are already running.
            found_by_c: dict[str, list] = {}
            node_c = PeerDiscovery(
                sender_id=103, listen_port=6103,
                on_peer_added=lambda name, candidates: found_by_c.__setitem__(name, candidates),
                on_peer_removed=lambda name: found_by_c.pop(name, None),
            )
            node_c.start()
            try:
                assert _wait_until(lambda: len(found_by_a) == 2), "node_a never found late-joining node_c"
                assert _wait_until(lambda: len(found_by_b) == 2), "node_b never found late-joining node_c"
                assert _wait_until(lambda: len(found_by_c) == 2), "node_c never found existing nodes a/b"
            finally:
                node_c.stop()
        finally:
            node_b.stop()
    finally:
        node_a.stop()
