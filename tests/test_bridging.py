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
from dataclasses import replace

import pytest

from outpost.client import OutpostClient
from outpost.config import OutpostConfig, Channel, Peer
from outpost.crypto import generate_channel_key
from outpost.protocol import TYPE_AUDIO, decode_packet, pad_audio

PORT = 5555
NETS = {"A": "10.1.0.0/24", "B": "10.2.0.0/24", "C": "10.3.0.0/24", "D": "10.4.0.0/24",
        "vpn": "10.99.0.0/24", "eth": "10.50.0.0/24"}
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
        self.blocked: set[frozenset] = set()  # node-name pairs that can't reach each other

    def add(self, node: Node, multicast: bool = True):
        self.nodes.append(node)
        t = node.client.transport
        t._transport = FakeSocket(self, node)
        t.listen_port = PORT
        t._is_own_address = lambda host, port, node=node: port == PORT and host in node.ips.values()
        if multicast:
            t._mcast_transport = object()
            t._mcast_port = 5556
        t.set_groups = lambda groups, node=node: setattr(node, "groups", set(groups))
        node.client.mixer.push = lambda ch, sid, seq, opus, node=node: node.heard.append(sid)
        node.client._own_addresses = lambda node=node: list(node.ips.values())
        node.client._update_groups()

    def send(self, sender: Node, packet, addr):
        host, port = addr
        if ipaddress.IPv4Address(host).is_multicast:
            net = sender.site
            self.links[(net, "multicast")] += 1
            src = (sender.ips[net], PORT)
            for n in self.nodes:
                if net in n.ips and host in n.groups and frozenset((sender.name, n.name)) not in self.blocked:
                    self.queue.append((n, packet, src, True))
            return
        for net, cidr in NETS.items():
            if net in sender.ips and ipaddress.IPv4Address(host) in ipaddress.IPv4Network(cidr):
                dest = next((n for n in self.nodes if n.ips.get(net) == host), None)
                self.links[(net, "unicast")] += 1
                if dest is not None and frozenset((sender.name, dest.name)) not in self.blocked:
                    self.queue.append((dest, packet, (sender.ips[net], PORT), False))
                return
        # no route - dropped

    def run(self):
        while self.queue:
            node, packet, src, mcast = self.queue.pop(0)
            header, ct = decode_packet(packet)
            node.client._on_packet(header, ct, src, via_multicast=mcast)

    def hellos(self):
        for n in self.nodes:
            n.client._check_bridges()
            n.client._send_bridge_hello()
        self.run()

    def round(self):
        self.hellos()
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
        peers=[Peer(h, PORT) for h in peers], enable_mdns=False, enable_pex=True, pex_fanout=64,
        enable_multicast=True, multicast_min_listeners=3, enable_relay=relay,
        site_subnets=[site_cidr] if site_cidr else [],
    ), active)


MESH_KEY = generate_channel_key()


@pytest.fixture
def mesh():
    return build_mesh()


def build_mesh(no_multicast=(), laptops=0, second_bridge_b=False, blocked=(), ethernet_site_d=False):
    """no_multicast: names of site nodes with multicast turned off.
    laptops: VPN clients (no site) using BA as their hub.
    second_bridge_b: a standby bridge BB2 on site B (VPN 10.99.0.4).
    blocked: node-name pairs that can't reach each other.
    ethernet_site_d: a fourth site D (d1..d3, bridge BD) that only connects
    to site A, over Ethernet through a second site-A bridge BA2 - not the VPN."""
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
            name = f"{site.lower()}{i}"
            net.add(Node(name, c, {site: f"{prefix}.0.{10 + i}"}, site), multicast=name not in no_multicast)
            sid += 1
    if second_bridge_b:
        own = {250: Channel(250, "bridge-B2", generate_channel_key())}
        bridge = _client(sid, own, "bridge-B2", relay=True, site_cidr=NETS["B"],
                         peers=["10.2.0.1", "10.99.0.2"])
        net.add(Node("BB2", bridge, {"B": "10.2.0.2", "vpn": "10.99.0.4"}, "B"))
        sid += 1
    if ethernet_site_d:
        own = {251: Channel(251, "bridge-A2", generate_channel_key())}
        bridge = _client(sid, own, "bridge-A2", relay=True, site_cidr=NETS["A"],
                         peers=["10.1.0.1", "10.50.0.2"])
        net.add(Node("BA2", bridge, {"A": "10.1.0.2", "eth": "10.50.0.1"}, "A"))
        sid += 1
        own = {252: Channel(252, "bridge-D", generate_channel_key())}
        bridge = _client(sid, own, "bridge-D", relay=True, site_cidr=NETS["D"], peers=["10.50.0.1"])
        net.add(Node("BD", bridge, {"D": "10.4.0.1", "eth": "10.50.0.2"}, "D"))
        sid += 1
        for i in range(1, 4):
            c = _client(sid, {MAIN: main}, "main", peers=["10.4.0.1"])
            net.add(Node(f"d{i}", c, {"D": f"10.4.0.{10 + i}"}, "D"))
            sid += 1
    for i in range(1, laptops + 1):
        c = _client(sid, {MAIN: main}, "main", peers=["10.99.0.1"])
        net.add(Node(f"L{i}", c, {"vpn": f"10.99.0.{20 + i}"}, "vpn"), multicast=False)
        sid += 1
    net.blocked = {frozenset(pair) for pair in blocked}
    for _ in range(6):
        net.round()
    return net


def _node(net, name):
    return next(n for n in net.nodes if n.name == name)


def _talk(net, talker, channel=MAIN):
    c = talker.client
    targets, group = c._channel_delivery(channel)
    opus = c.codec.encode(b"\x00\x00" * c.codec.frame_size)
    for n in net.nodes:
        n.heard.clear()
    net.links.clear()
    c.transport.send_encrypted(c.ciphers[channel], channel, TYPE_AUDIO, pad_audio(opus, None),
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


@pytest.mark.parametrize("second_bridge", [False, True])
def test_site_addresses_stay_on_their_site(second_bridge):
    """Peer exchange through a bridge must not leak one site's addresses to
    another (sites may even reuse the same subnets) - including with two
    bridges on one site, which share everything with each other."""
    mesh = build_mesh(second_bridge_b=second_bridge)
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


def test_bridge_unicasts_into_site_nodes_without_multicast():
    """A bridge multicasting into its site must still unicast to site
    listeners that never joined multicast (enable_multicast off)."""
    net = build_mesh(no_multicast=("b5", "b6"))
    talker = _node(net, "a1")
    _talk(net, talker)
    for name in ("b1", "b5", "b6"):
        assert talker.client.config.sender_id in _node(net, name).heard, f"{name} didn't hear a1"
    assert net.links[("B", "multicast")] == 1
    assert net.links[("B", "unicast")] == 2  # just b5 and b6


TWO = 2


def test_plain_relay_carries_multicast_to_remote_listeners():
    """A plain relay (no site) whose address is heard via multicast: talkers
    leave remote listeners behind it to the relay, so the relay has to be
    in that channel's group even for channels it holds no key for."""
    main = Channel(MAIN, "main", generate_channel_key())
    two = Channel(TWO, "two", generate_channel_key())
    net = FakeNet()
    relay = _client(1, {MAIN: main}, "main", relay=True)
    net.add(Node("R", relay, {"A": "10.1.0.1", "vpn": "10.99.0.1"}, "A"))
    for i in range(1, 5):
        c = _client(1 + i, {MAIN: main, TWO: two}, "two", peers=["10.1.0.1"])
        net.add(Node(f"a{i}", c, {"A": f"10.1.0.{10 + i}"}, "A"))
    remote = _client(9, {TWO: two}, "two", peers=["10.99.0.1"])
    net.add(Node("v1", remote, {"vpn": "10.99.0.5"}, "vpn"), multicast=False)
    for _ in range(6):
        net.round()
    talker = _node(net, "a1")
    targets, group = _talk(net, talker, TWO)
    assert group is not None  # big enough on site A to multicast
    assert talker.client.config.sender_id in _node(net, "v1").heard


def _listeners(net, talker):
    return [n for n in net.nodes if not n.name.startswith("B") and n is not talker]


def test_hub_bridge_relays_between_vpn_clients_that_cant_reach_each_other():
    """BA is also the VPN hub for two laptops that can't reach each other
    directly: BA has to relay between them, while bridges (which do reach
    each other) still never pass packets on to each other."""
    # Hub-and-spoke: the laptops only reach the hub.
    net = build_mesh(laptops=2, blocked=[("L1", "L2"), ("L1", "BB"), ("L1", "BC"), ("L2", "BB"), ("L2", "BC")])
    talker = _node(net, "L1")
    _talk(net, talker)
    for n in _listeners(net, talker):
        assert n.heard.count(talker.client.config.sender_id) == 1, n.name
    # L1 -> BA, then BA -> BB, BC, L2. Nothing between BB and BC.
    assert net.links[("vpn", "unicast")] == 4


def test_vpn_clients_that_reach_each_other_arent_sent_extra_copies():
    net = build_mesh(laptops=2)
    talker = _node(net, "L1")
    _talk(net, talker)
    for n in _listeners(net, talker):
        assert n.heard.count(talker.client.config.sender_id) == 1, n.name
    # L1 -> L2 directly and -> BA; BA -> BB, BC only.
    assert net.links[("vpn", "unicast")] == 4


def test_relay_carries_traffic_between_site_nodes_that_cant_reach_each_other():
    """E.g. Wi-Fi client isolation: c1 and c2 only reach each other via BC."""
    net = build_mesh(blocked=[("c1", "c2")])
    talker = _node(net, "c1")
    _talk(net, talker)
    assert talker.client.config.sender_id in _node(net, "c2").heard


def test_two_bridges_on_one_site_forward_one_copy():
    net = build_mesh(second_bridge_b=True)
    assert _node(net, "BA").client.bridge_role() == "active"
    assert _node(net, "BB").client.bridge_role() == "active"
    assert _node(net, "BB2").client.bridge_role() == "standby"
    talker = _node(net, "a1")
    _talk(net, talker)
    for n in _listeners(net, talker):
        assert n.heard.count(talker.client.config.sender_id) == 1, n.name
    assert net.links[("B", "multicast")] == 1
    assert net.links[("vpn", "unicast")] == 2

    b1 = _node(net, "b1")
    _talk(net, b1)
    for n in _listeners(net, b1):
        assert n.heard.count(b1.client.config.sender_id) == 1, n.name
    assert net.links[("vpn", "unicast")] == 2


def test_standby_bridge_takes_over_when_the_active_one_dies(monkeypatch):
    import time
    net = build_mesh(second_bridge_b=True)
    bb = _node(net, "BB")
    net.nodes.remove(bb)  # BB goes off the air

    real = time.monotonic
    monkeypatch.setattr(time, "monotonic", lambda: real() + 10.0)  # > 3 hello intervals
    net.hellos()  # just the hello timer - no new announces yet
    assert _node(net, "BB2").client.bridge_role() == "active"

    a1 = _node(net, "a1")
    _talk(net, a1)
    for n in _listeners(net, a1):
        assert a1.client.config.sender_id in n.heard, f"{n.name} didn't hear a1 after failover"
    b1 = _node(net, "b1")
    _talk(net, b1)
    for n in _listeners(net, b1):
        assert b1.client.config.sender_id in n.heard, f"{n.name} didn't hear b1 after failover"


def test_higher_priority_bridge_is_preferred():
    net = build_mesh(second_bridge_b=True)
    bb2 = _node(net, "BB2").client
    bb2.config = replace(bb2.config, bridge_priority=200)
    net.hellos()
    net.hellos()
    assert bb2.bridge_role() == "active"
    assert _node(net, "BB").client.bridge_role() == "standby"


def test_bridges_to_different_networks_on_one_site_both_forward():
    """Site A has two bridges leading to different places: BA to the VPN,
    BA2 over Ethernet to site D. Neither may stand by for the other."""
    net = build_mesh(ethernet_site_d=True)
    for talker_name in ("a1", "d1", "b1"):
        talker = _node(net, talker_name)
        _talk(net, talker)
        for n in _listeners(net, talker):
            assert n.heard.count(talker.client.config.sender_id) == 1, f"{n.name} from {talker_name}"
    assert _node(net, "BA").client.bridge_role() == "active"
    assert _node(net, "BA2").client.bridge_role() == "active"


def test_two_bridges_relay_inside_their_site_once():
    """One group on its own: b1 and b2 (multicast off) can't reach each other
    directly. Exactly one of the site's two bridges relays between them."""
    net = build_mesh(second_bridge_b=True, no_multicast=("b1", "b2"), blocked=[("b1", "b2")])
    b1 = _node(net, "b1")
    _talk(net, b1)
    assert _node(net, "b2").heard.count(b1.client.config.sender_id) == 1
