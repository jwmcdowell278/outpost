"""Dead peers expire instead of being kept alive by gossip, and relays
forward channel traffic only toward neighbours that lead to members."""
from __future__ import annotations
import time

from outpost import transport as transport_mod
from outpost.client import OutpostClient
from outpost.config import OutpostConfig, Channel, Peer
from outpost.crypto import generate_channel_key
from outpost.pex import PexGossip
from outpost.protocol import Header, VERSION, TYPE_AUDIO, TYPE_STOP, TYPE_CHANNEL_ANNOUNCE
from outpost.transport import MeshTransport

DEAD = Peer("10.20.31.81", 5555)


def _transport():
    return MeshTransport(sender_id=1, peers=[], on_packet=lambda *a: None)


def _expire(t, key):
    t._last_seen[key] = time.monotonic() - 1000
    t.prune_stale(max_age_s=1.0)


# --- #1 dead peers ------------------------------------------------------

def test_expired_unheard_peer_is_not_readded_by_gossip_right_away():
    t = _transport()
    t.add_peer("gossip-10.20.31.81:5555", DEAD)
    _expire(t, "gossip-10.20.31.81:5555")
    t.add_peer("gossip-10.20.31.81:5555", DEAD)  # someone gossips it again
    assert t.peers == []


def test_backoff_doubles_each_time_and_is_capped(monkeypatch):
    t = _transport()
    now = [1000.0]
    monkeypatch.setattr(transport_mod.time, "monotonic", lambda: now[0])
    delays = []
    for _ in range(6):
        t.add_peer("gossip-x", DEAD)
        t._last_seen["gossip-x"] = now[0] - 1000
        t.prune_stale(max_age_s=1.0)
        delays.append(t._backoff[("10.20.31.81", 5555)][1])
        now[0] += delays[-1] + 1  # wait out the backoff
    assert delays == [300, 600, 1200, 2400, 3600, 3600]


def test_hearing_from_a_backed_off_address_clears_backoff():
    t = _transport()
    t.add_peer("gossip-x", DEAD)
    _expire(t, "gossip-x")
    assert t.confirm_peer(9, DEAD)  # it came back and talked to us directly
    assert ("10.20.31.81", 5555) not in t._backoff
    assert DEAD in t.peers


def test_static_peers_ignore_backoff():
    t = _transport()
    t.add_peer("gossip-x", DEAD)
    _expire(t, "gossip-x")
    t.add_peer("static-0", DEAD)
    assert DEAD in t.peers


def test_only_peers_heard_from_are_gossiped():
    t = _transport()
    t.add_peer("gossip-10.20.31.81:5555", DEAD)        # second-hand only
    t.confirm_peer(7, Peer("192.168.88.10", 5555))     # heard directly
    assert t.live_peers == [Peer("192.168.88.10", 5555)]


class _RecordingTransport:
    def __init__(self):
        self.confirmed = set()
        self.sent = []
        self.peers = []
        self.live_peers = []

    def is_confirmed_sender(self, sid):
        return sid in self.confirmed

    def confirm_peer(self, sid, peer):
        self.confirmed.add(sid)
        return True

    def add_peer(self, key, peer):
        pass

    def send_encrypted(self, cipher, channel_id, ptype, plaintext, targets=None):
        self.sent.append(targets)


def test_pex_replies_immediately_on_first_contact_only():
    mesh_key = generate_channel_key()
    other = _RecordingTransport()
    sender = PexGossip(5, mesh_key, other)
    t = _RecordingTransport()
    pex = PexGossip(1, mesh_key, t)

    # Build a real PEX packet from node 5.
    captured = []
    other.send_encrypted = lambda cipher, ch, ptype, plaintext, targets=None: captured.append(
        (Header(VERSION, ptype, ch, 5, 1), cipher.encrypt(Header(VERSION, ptype, ch, 5, 1), plaintext)))
    sender._send_to([Peer("127.0.0.1", 1)])
    header, ct = captured[0]

    pex.handle_incoming(header, ct, ("192.168.88.10", 5555))
    pex.handle_incoming(header, ct, ("192.168.88.10", 5555))
    assert t.sent == [[Peer("192.168.88.10", 5555)]]


# --- #4 relay routes ----------------------------------------------------

A = ("192.168.88.10", 5555)   # sender, a member
B = ("192.168.88.18", 5555)   # another member
C = ("10.8.0.2", 5555)        # neighbour with no members of channel 1
R2 = ("10.8.0.9", 5555)       # another relay leading to members


def _relay_client():
    client = OutpostClient(OutpostConfig(
        sender_id=1, listen_port=0, mesh_key=generate_channel_key(),
        channels={2: Channel(2, "other", generate_channel_key())},  # no key for channel 1
        enable_mdns=False, enable_pex=False, enable_relay=True,
    ), "other")
    for host, port in (A, B, C, R2):
        client.transport.add_peer(f"gossip-{host}:{port}", Peer(host, port))
    forwarded = []
    client.transport.forward = lambda packet, targets: forwarded.append({(p.host, p.port) for p in targets})
    return client, forwarded


def _pkt(ptype, sender, seq, channel=1):
    return Header(VERSION, ptype, channel, sender, seq), b"\x00" * 32


def test_relay_forwards_audio_only_toward_neighbours_with_members():
    client, forwarded = _relay_client()
    client._on_packet(*_pkt(TYPE_CHANNEL_ANNOUNCE, 10, 1), A)
    client._on_packet(*_pkt(TYPE_CHANNEL_ANNOUNCE, 11, 1), B)
    forwarded.clear()

    client._on_packet(*_pkt(TYPE_AUDIO, 10, 2), A)
    assert forwarded == [{B}]  # not back to A, not to C or R2


def test_relay_learns_routes_through_other_relays():
    client, forwarded = _relay_client()
    client._on_packet(*_pkt(TYPE_CHANNEL_ANNOUNCE, 10, 1), A)
    client._on_packet(*_pkt(TYPE_CHANNEL_ANNOUNCE, 50, 1), R2)  # member behind R2
    forwarded.clear()
    client._on_packet(*_pkt(TYPE_STOP, 10, 2), A)
    assert forwarded == [{R2}]


def test_relay_does_not_forward_when_only_route_is_back_to_sender():
    client, forwarded = _relay_client()
    client._on_packet(*_pkt(TYPE_CHANNEL_ANNOUNCE, 10, 1), A)
    forwarded.clear()
    client._on_packet(*_pkt(TYPE_AUDIO, 10, 2), A)
    assert forwarded == []


def test_relay_floods_until_any_route_is_known():
    client, forwarded = _relay_client()
    client._on_packet(*_pkt(TYPE_AUDIO, 10, 2), A)
    assert forwarded == [{B, C, R2}]


def test_announces_are_still_flooded():
    client, forwarded = _relay_client()
    client._on_packet(*_pkt(TYPE_CHANNEL_ANNOUNCE, 10, 1), A)
    assert forwarded == [{B, C, R2}]


def test_announce_from_unknown_address_does_not_create_a_route():
    client, forwarded = _relay_client()
    client._on_packet(*_pkt(TYPE_CHANNEL_ANNOUNCE, 10, 1), A)
    client._on_packet(*_pkt(TYPE_CHANNEL_ANNOUNCE, 66, 1), ("203.0.113.5", 4444))  # spoofed
    assert ("203.0.113.5", 4444) not in client._relay_routes[1]


def test_relay_routes_expire(monkeypatch):
    client, forwarded = _relay_client()
    client._on_packet(*_pkt(TYPE_CHANNEL_ANNOUNCE, 11, 1), B)
    client._relay_routes[1][B] -= client._membership_ttl_s + 1
    forwarded.clear()
    client._on_packet(*_pkt(TYPE_AUDIO, 10, 2), A)
    assert forwarded == [{B, C, R2}]  # route gone -> back to flooding
