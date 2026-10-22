"""Multi-site bridging, end to end on a simulated network: three sites
(each its own mesh/subnet with multicast), joined by bridge nodes over a
VPN. Real clients, real encryption and packet handling - only the network
underneath is simulated, so per-link copy counts can be checked.

Topology:
    site A (10.1.0.0/24): a1..a6 + bridge BA (10.1.0.1 / VPN 10.99.0.1)
    site B (10.2.0.0/24): b1..b6 + bridge BB (10.2.0.1 / VPN 10.99.0.2)
    site C (10.3.0.0/24): c1..c2 + bridge BC (10.3.0.1 / VPN 10.99.0.3)
Bridges hold no channel keys. Each site node is only configured with its
own bridge; each bridge only with one other bridge (BA->BB, BC->BB), so
everything else has to be discovered.
"""
from __future__ import annotations
import ipaddress
from collections import Counter

import pytest

from outpost.client import OutpostClient
from outpost.config import OutpostConfig, Channel, Peer
from outpost.crypto import generate_channel_key
from outpost.protocol import TYPE_AUDIO, decode_packet, pad_audio

PORT = 5555
NETS = {"A": "10.1.0.0/24", "B": "10.2.0.0/24", "C": "10.3.0.0/24", "vpn": "10.99.0.0/24"}
MAIN = 1


class FakeSocket:
    def __init__(self, net, node):
        self.net, self.node = net, node

    def sendto(self, packet, addr):
        self.net.send(self.node, packet, addr)

    def get_extra_info(self, name):
        return ("0.0.0.0", PORT)

    def close(self):
        pass


class Node:
    def __init__(self, name, client, ips, site):
        self.name, self.client, self.ips, self.site = name, client, ips, site
        self.groups: set[str] = set()
        self.heard: list[int] = []  # sender_ids of audio handed to the mixer


class FakeNet:
    def __init__(self):
        self.nodes: list[Node] = []
        self.queue = []
        self.links = Counter()  # (network, "multicast"/"unicast") -> transmissions

    def add(self, node: Node):
        self.nodes.append(node)
        t = node.client.transport
        t._transport = FakeSocket(self, node)
        t.listen_port = PORT
        t._is_own_address = lambda host, port, node=node: port == PORT and host in node.ips.values()
        t._mcast_transport = object()
        t._mcast_port = 5556
        t.set_groups = lambda groups, node=node: setattr(node, "groups", set(groups))
        node.client.mixer.push = lambda ch, sid, seq, opus, node=node: node.heard.append(sid)
        node.client._update_groups()

    def send(self, sender: Node, packet, addr):
        host, port = addr
        if ipaddress.IPv4Address(host).is_multicast:
            net = sender.site
            self.links[(net, "multicast")] += 1
            src = (sender.ips[net], PORT)
            for n in self.nodes:
                if net in n.ips and host in n.groups:
                    self.queue.append((n, packet, src, True))
            return
        for net, cidr in NETS.items():
            if net in sender.ips and ipaddress.IPv4Address(host) in ipaddress.IPv4Network(cidr):
                dest = next((n for n in self.nodes if n.ips.get(net) == host), None)
                self.links[(net, "unicast")] += 1
                if dest is not None:
                    self.queue.append((dest, packet, (sender.ips[net], PORT), False))
                return
        # no route - dropped

    def run(self):
        while self.queue:
            node, packet, src, mcast = self.queue.pop(0)
            header, ct = decode_packet(packet)
            node.client._on_packet(header, ct, src, via_multicast=mcast)

    def round(self):
        for n in self.nodes:
            if n.client.pex:
                n.client.pex._announce()
        self.run()
        for n in self.nodes:
            for cid in list(n.client.ciphers):
                n.client._send_announce(cid)
        self.run()


def _client(sender_id, channels, active, *, relay=False, site_cidr=None, peers=()):
    return OutpostClient(OutpostConfig(
        sender_id=sender_id, listen_port=PORT, mesh_key=MESH_KEY, channels=channels,
        peers=[Peer(h, PORT) for h in peers], enable_mdns=False, enable_pex=True,
        enable_multicast=True, multicast_min_listeners=3, enable_relay=relay,
        site_subnets=[site_cidr] if site_cidr else [],
    ), active)


MESH_KEY = generate_channel_key()


@pytest.fixture
def mesh():
    return build_mesh()


def build_mesh():
    main = Channel(MAIN, "main", generate_channel_key())
    net = FakeNet()
    sizes = {"A": 6, "B": 6, "C": 2}
    bridge_vpn = {"A": "10.99.0.1", "B": "10.99.0.2", "C": "10.99.0.3"}
    bridge_bootstrap = {"A": ["10.99.0.2"], "B": [], "C": ["10.99.0.2"]}
    sid = 1
    for site, n in sizes.items():
        prefix = NETS[site].rsplit(".", 2)[0]
        bridge_site_ip = f"{prefix}.0.1"
        # Bridge: no keys for "main", just a placeholder channel of its own.
        own = {200 + sid: Channel(200 + sid, f"bridge-{site}", generate_channel_key())}
        bridge = _client(sid, own, f"bridge-{site}", relay=True, site_cidr=NETS[site],
                         peers=bridge_bootstrap[site])
        net.add(Node(f"B{site}", bridge, {site: bridge_site_ip, "vpn": bridge_vpn[site]}, site))
        sid += 1
        for i in range(1, n + 1):
            c = _client(sid, {MAIN: main}, "main", peers=[bridge_site_ip])
            net.add(Node(f"{site.lower()}{i}", c, {site: f"{prefix}.0.{10 + i}"}, site))
            sid += 1
    for _ in range(6):
        net.round()
    return net


def _node(net, name):
    return next(n for n in net.nodes if n.name == name)


def _talk(net, talker):
    c = talker.client
    targets, group = c._channel_delivery(MAIN)
    opus = c.codec.encode(b"\x00\x00" * c.codec.frame_size)
    for n in net.nodes:
        n.heard.clear()
    net.links.clear()
    c.transport.send_encrypted(c.ciphers[MAIN], MAIN, TYPE_AUDIO, pad_audio(opus, None),
                               targets=targets, multicast_group=group)
    net.run()
    return targets, group


def test_every_listener_on_every_site_hears_a_talker(mesh):
    talker = _node(mesh, "a1")
    _talk(mesh, talker)
    for n in mesh.nodes:
        if n.name.startswith("B") or n is talker:
            continue
        assert talker.client.config.sender_id in n.heard, f"{n.name} didn't hear a1"


def test_one_copy_per_site_not_per_listener(mesh):
    _talk(mesh, _node(mesh, "a1"))
    links = mesh.links
    assert links[("A", "multicast")] == 1        # whole of site A, once
    assert links[("A", "unicast")] <= 1          # at most one copy to its bridge
    assert links[("vpn", "unicast")] == 2        # BA -> BB, BA -> BC: one per remote site
    assert links[("B", "multicast")] == 1        # BB re-multicasts into site B once
    assert links[("C", "unicast")] == 2          # site C is small (2): unicast inward
    assert links[("B", "unicast")] == 0


def test_bridges_never_bounce_traffic_between_each_other(mesh):
    _talk(mesh, _node(mesh, "b3"))
    # From site B: BB sends to BA and BC; neither sends it back or on.
    assert mesh.links[("vpn", "unicast")] == 2


def test_each_listener_hears_a_frame_at_most_twice(mesh):
    talker = _node(mesh, "c1")
    _talk(mesh, talker)
    for n in mesh.nodes:
        if n.name.startswith("B") or n is talker:
            continue
        assert 1 <= n.heard.count(talker.client.config.sender_id) <= 1, n.name


def test_site_addresses_stay_on_their_site(mesh):
    """Peer exchange through a bridge must not leak one site's addresses to
    another (sites may even reuse the same subnets)."""
    for n in mesh.nodes:
        if n.name.startswith("B"):
            continue
        own = ipaddress.IPv4Network(NETS[n.site])
        for p in n.client.transport.peers:
            assert ipaddress.IPv4Address(p.host) in own, f"{n.name} learned off-site address {p.host}"


def test_bridges_discover_every_other_bridge(mesh):
    # BA was only configured with BB; it must have found BC too.
    ba = _node(mesh, "BA").client
    vpn_peers = {p.host for p in ba.transport.peers if p.host.startswith("10.99.")}
    assert vpn_peers == {"10.99.0.2", "10.99.0.3"}


def test_talker_learns_remote_listeners_via_its_bridge(mesh):
    a1 = _node(mesh, "a1").client
    listeners = a1._channel_listeners(MAIN)
    via_bridge = {sid for sid, peer in listeners if peer.host == "10.1.0.1"}
    # All 8 off-site listeners (b1..b6, c1..c2) are reached through BA.
    assert len(via_bridge) == 8
