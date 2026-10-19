"""Regression test for a real-world bug: with relay mode on, a relay's own
address is the immediate UDP source for every packet it forwards, so
confirm_peer() was blindly attributing the RELAY's address to whatever
sender_id each relayed packet's header claimed - two entirely different
remote nodes, both reached only via the same relay, ended up showing the
exact same address in the web UI's Channel members table."""
from __future__ import annotations

from outpost.config import Peer
from outpost.transport import MeshTransport


def _make_transport(listen_port=5555) -> MeshTransport:
    transport = MeshTransport(sender_id=1, peers=[], on_packet=lambda *a: None)
    transport.listen_port = listen_port
    return transport


def test_second_sender_relayed_through_same_address_is_not_misattributed():
    transport = _make_transport()

    assert transport.confirm_peer(100, Peer(host="192.168.88.18", port=5555)) is True
    # sender 200's packet also arrived via 192.168.88.18 (relayed) - must
    # not be recorded as sender 200's own address too.
    assert transport.confirm_peer(200, Peer(host="192.168.88.18", port=5555)) is False

    peers = {p.host for p in transport.peers}
    assert peers == {"192.168.88.18"}  # only sender 100 claims that address


def test_repeated_confirmation_of_the_same_sender_still_succeeds():
    transport = _make_transport()

    assert transport.confirm_peer(100, Peer(host="192.168.88.18", port=5555)) is True
    # Every audio frame re-confirms the same sender at its own address -
    # must not be rejected as a "conflict" against itself.
    assert transport.confirm_peer(100, Peer(host="192.168.88.18", port=5555)) is True


def test_sender_can_move_to_a_fresh_unclaimed_address():
    transport = _make_transport()

    assert transport.confirm_peer(100, Peer(host="192.168.88.18", port=5555)) is True
    assert transport.confirm_peer(100, Peer(host="192.168.88.50", port=5555)) is True

    assert transport.peers == [Peer(host="192.168.88.50", port=5555)]


def test_address_becomes_claimable_again_after_owner_is_pruned():
    transport = _make_transport()
    transport.confirm_peer(100, Peer(host="192.168.88.18", port=5555))

    transport.remove_peer("peer-100")

    assert transport.confirm_peer(200, Peer(host="192.168.88.18", port=5555)) is True
    assert transport.peers == [Peer(host="192.168.88.18", port=5555)]
