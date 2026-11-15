"""Gateway links (OutpostConfig.gateways): two meshes with different mesh
keys share chosen channels through a gateway on each side. Only the link's
channels cross; nothing else does, and neither mesh learns the other's
peer addresses."""
from __future__ import annotations
import asyncio
import os
import socket

import pytest

from outpost.client import OutpostClient
from outpost.config import OutpostConfig, Channel, GatewayLink, Peer, load_gateways
from outpost.crypto import generate_channel_key
from outpost.protocol import TYPE_AUDIO, TYPE_CHANNEL_ANNOUNCE, TYPE_PEX

SHARED = 201   # the channel both meshes share
PRIVATE = 7    # mesh B's own channel - must never cross


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _node(sender_id: int, port: int, channels: dict[int, bytes]) -> OutpostClient:
    config = OutpostConfig(
        sender_id=sender_id, listen_port=port, mesh_key=generate_channel_key(),
        channels={cid: Channel(id=cid, name=f"ch{cid}", key=key) for cid, key in channels.items()},
        enable_mdns=False, enable_pex=False, enable_multicast=False,
    )
    return OutpostClient(config, f"ch{next(iter(channels))}")


def _gateway(sender_id: int, port: int, link: GatewayLink) -> OutpostClient:
    config = OutpostConfig(
        sender_id=sender_id, listen_port=port, mesh_key=generate_channel_key(),
        relay_only=True, gateways=[link],
        enable_mdns=False, enable_pex=False, enable_multicast=False,
    )
    return OutpostClient(config, None)


def _spy(client: OutpostClient, ports) -> list[tuple[int, int]]:
    """(ptype, channel_id) of every packet the client receives from one of
    `ports` (this test's own nodes - a stray packet from an earlier test's
    socket can land on a reused port)."""
    received: list[tuple[int, int]] = []
    original = client._on_packet

    def wrapper(header, ciphertext, addr, via_multicast=False):
        if addr[1] in ports.values():
            received.append((header.ptype, header.channel_id))
        original(header, ciphertext, addr, via_multicast)

    client.transport._on_packet = wrapper
    return received


class _TwoMeshes:
    """a1 - gA ==link== gB - b1, where a1/gA and gB/b1 have different
    mesh keys. a1 and b1 both hold the shared channel; b1 also has a private
    one."""

    def __init__(self, shared_channels=(SHARED,)):
        self.shared_key, self.private_key = generate_channel_key(), generate_channel_key()
        ports = {name: _free_port() for name in ("a1", "gA", "gB", "b1")}
        self.ports = ports
        self.a1 = _node(1, ports["a1"], {SHARED: self.shared_key, PRIVATE: generate_channel_key()})
        self.b1 = _node(2, ports["b1"], {SHARED: self.shared_key, PRIVATE: self.private_key})
        self.gA = _gateway(10, ports["gA"], GatewayLink(
            "mesh-b", frozenset(shared_channels), (Peer("127.0.0.1", ports["gB"]),)))
        self.gB = _gateway(20, ports["gB"], GatewayLink(
            "mesh-a", frozenset(shared_channels), (Peer("127.0.0.1", ports["gA"]),)))
        self.to_a1 = _spy(self.a1, ports)
        self.to_b1 = _spy(self.b1, ports)

    def peer(self, name: str) -> Peer:
        return Peer("127.0.0.1", self.ports[name])

    async def __aenter__(self):
        for name in ("a1", "gA", "gB", "b1"):
            node = getattr(self, name)
            await node.transport.start(self.ports[name])
        # Each mesh's own peers: a1 <-> gA and b1 <-> gB.
        self.gA.transport.add_peer("a1", self.peer("a1"))
        self.gB.transport.add_peer("b1", self.peer("b1"))
        return self

    async def __aexit__(self, *exc):
        for name in ("a1", "gA", "gB", "b1"):
            getattr(self, name).transport.stop()

    def b1_announces(self, channel_id: int) -> None:
        self.b1.transport.send_encrypted(self.b1.ciphers[channel_id], channel_id, TYPE_CHANNEL_ANNOUNCE,
                                         os.urandom(8) + b"\x01", targets=[self.peer("gB")])

    def a1_announces(self, channel_id: int) -> None:
        self.a1.transport.send_encrypted(self.a1.ciphers[channel_id], channel_id, TYPE_CHANNEL_ANNOUNCE,
                                         os.urandom(8) + b"\x01", targets=[self.peer("gA")])

    def a1_talks(self, channel_id: int) -> None:
        self.a1.transport.send_encrypted(self.a1.ciphers[channel_id], channel_id, TYPE_AUDIO,
                                         b"opus-frame", targets=[self.peer("gA")])

    def b1_talks(self, channel_id: int) -> None:
        self.b1.transport.send_encrypted(self.b1.ciphers[channel_id], channel_id, TYPE_AUDIO,
                                         b"opus-frame", targets=[self.peer("gB")])


def test_shared_channel_audio_crosses_once_a_member_has_announced():
    asyncio.run(_shared_channel_audio_crosses())


async def _shared_channel_audio_crosses():
    async with _TwoMeshes() as m:
        m.b1_announces(SHARED)
        await asyncio.sleep(0.2)
        # b1's announce went gB -> gA -> a1, so a1 sees b1 as a member.
        assert (TYPE_CHANNEL_ANNOUNCE, SHARED) in m.to_a1
        assert 2 in m.a1._channel_members.get(SHARED, {})

        m.a1_talks(SHARED)
        await asyncio.sleep(0.2)
        assert (TYPE_AUDIO, SHARED) in m.to_b1

        # And back: a1 announces, b1 talks, a1 hears it.
        m.a1_announces(SHARED)
        await asyncio.sleep(0.2)
        m.b1_talks(SHARED)
        await asyncio.sleep(0.2)
        assert (TYPE_AUDIO, SHARED) in m.to_a1


def test_audio_does_not_cross_before_any_member_across_is_known():
    asyncio.run(_audio_needs_a_route())


async def _audio_needs_a_route():
    async with _TwoMeshes() as m:
        m.a1_talks(SHARED)
        await asyncio.sleep(0.2)
        assert (TYPE_AUDIO, SHARED) not in m.to_b1
        assert m.gA.gateway_status()[0]["sent"] == 0


def test_channels_not_on_the_link_never_cross():
    asyncio.run(_private_channel_stays_home())


async def _private_channel_stays_home():
    async with _TwoMeshes() as m:
        m.b1_announces(PRIVATE)
        m.a1_announces(PRIVATE)
        await asyncio.sleep(0.2)
        m.b1_talks(PRIVATE)
        m.a1_talks(PRIVATE)
        await asyncio.sleep(0.2)
        assert not any(cid == PRIVATE for _, cid in m.to_a1)
        assert not any(cid == PRIVATE for _, cid in m.to_b1)
        # Neither gateway sent anything across.
        assert m.gA.gateway_status()[0]["sent"] == 0
        assert m.gB.gateway_status()[0]["sent"] == 0


def test_gateway_refuses_disallowed_packets_from_the_other_side():
    asyncio.run(_gateway_refuses())


async def _gateway_refuses():
    async with _TwoMeshes() as m:
        # A misbehaving other side sends a channel that isn't on the link and
        # a PEX packet straight at our gateway, from its gateway's address.
        other = m.gB.transport
        other.send(PRIVATE, other.next_seq(), b"x" * 40, ptype=TYPE_CHANNEL_ANNOUNCE, targets=[m.peer("gA")])
        other.send(0, other.next_seq(), b"x" * 40, ptype=TYPE_PEX, targets=[m.peer("gA")])
        await asyncio.sleep(0.2)
        assert m.to_a1 == []
        assert m.gA.gateway_status()[0]["refused"] == 2


def test_gateway_peers_never_join_the_mesh_peer_list():
    asyncio.run(_gateway_peers_stay_out())


async def _gateway_peers_stay_out():
    async with _TwoMeshes() as m:
        m.b1_announces(SHARED)
        m.a1_announces(SHARED)
        await asyncio.sleep(0.2)
        m.a1_talks(SHARED)
        m.b1_talks(SHARED)
        await asyncio.sleep(0.2)
        assert (m.peer("gB").host, m.peer("gB").port) not in {(p.host, p.port) for p in m.gA.transport.peers}
        assert (m.peer("gA").host, m.peer("gA").port) not in {(p.host, p.port) for p in m.gB.transport.peers}
        # a1 reaches b1 through gA, never directly.
        member = m.a1._channel_members[SHARED][2][0]
        assert (member.host, member.port) == (m.peer("gA").host, m.peer("gA").port)


def test_status_reports_routes_and_counts():
    asyncio.run(_status())


async def _status():
    async with _TwoMeshes() as m:
        m.b1_announces(SHARED)
        await asyncio.sleep(0.2)
        status = m.gA.gateway_status()[0]
        assert status["name"] == "mesh-b"
        assert status["channels"] == [SHARED]
        assert status["routed_channels"] == [SHARED]
        assert status["received"] == 1
        assert status["peers"][0]["addresses"] == ["127.0.0.1"]


def test_relay_cannot_be_turned_off_on_a_gateway():
    gw = _gateway(10, 0, GatewayLink("x", frozenset({SHARED}), (Peer("127.0.0.1", 9),)))
    assert gw.enable_relay
    with pytest.raises(ValueError):
        gw.enable_relay = False


# --- config -----------------------------------------------------------------

def test_load_gateways_parses_links():
    links = load_gateways([{"name": "entity-b", "channels": [201, 202],
                            "peers": [{"host": "203.0.113.9", "port": 5555}]}])
    assert links == [GatewayLink("entity-b", frozenset({201, 202}), (Peer("203.0.113.9", 5555),))]
    assert load_gateways(None) == []


@pytest.mark.parametrize("entry, message", [
    ({"channels": [201], "peers": [{"host": "h", "port": 1}]}, "name"),
    ({"name": "b", "peers": [{"host": "h", "port": 1}]}, "channel ids"),
    ({"name": "b", "channels": [0], "peers": [{"host": "h", "port": 1}]}, "1-255"),
    ({"name": "b", "channels": [256], "peers": [{"host": "h", "port": 1}]}, "1-255"),
    ({"name": "b", "channels": ["201"], "peers": [{"host": "h", "port": 1}]}, "whole number"),
    ({"name": "b", "channels": [201]}, "peers"),
    ({"name": "b", "channels": [201], "peers": [{"host": "h"}]}, "host and port"),
    ({"name": "b", "channels": [201], "peers": [{"host": "h", "port": 70000}]}, "1-65535"),
])
def test_load_gateways_rejects_bad_entries(entry, message):
    with pytest.raises(ValueError, match=message):
        load_gateways([entry])


def test_load_gateways_rejects_an_address_used_twice():
    link = {"name": "b", "channels": [201], "peers": [{"host": "10.0.0.1", "port": 5555}]}
    with pytest.raises(ValueError, match="listed twice"):
        load_gateways([link, link])
    with pytest.raises(ValueError, match="already a peer"):
        load_gateways([dict(link, name="c"), link])
    with pytest.raises(ValueError, match="already a peer"):
        load_gateways([link], peers=[Peer("10.0.0.1", 5555)])


def test_load_config_reads_gateways_and_turns_relay_on(tmp_path):
    import base64
    from outpost.config import load_config
    key = base64.b64encode(generate_channel_key()).decode()
    path = tmp_path / "outpost.yaml"
    path.write_text(f"""
mesh_key: "{key}"
relay_only: true
gateways:
  - name: entity-b
    channels: [201]
    peers:
      - host: gw.example.net
        port: 5555
""")
    config = load_config(str(path))
    assert config.enable_relay
    assert config.gateways == [GatewayLink("entity-b", frozenset({201}), (Peer("gw.example.net", 5555),))]


def test_validate_config_yaml_checks_gateways():
    import base64
    from outpost.config import validate_config_yaml
    key = base64.b64encode(generate_channel_key()).decode()
    with pytest.raises(ValueError, match="1-255"):
        validate_config_yaml(f"""
mesh_key: "{key}"
relay_only: true
gateways: [{{name: b, channels: [300], peers: [{{host: h, port: 1}}]}}]
""")


def test_redundant_gateways_deliver_without_looping():
    asyncio.run(_redundant_gateways())


async def _redundant_gateways():
    # Two gateways per side, every gateway linked to both of the other side's.
    shared_key = generate_channel_key()
    names = ("a1", "gA1", "gA2", "gB1", "gB2", "b1")
    ports = {n: _free_port() for n in names}
    peer = lambda n: Peer("127.0.0.1", ports[n])
    a1 = _node(1, ports["a1"], {SHARED: shared_key})
    b1 = _node(2, ports["b1"], {SHARED: shared_key})
    gws = {
        "gA1": _gateway(11, ports["gA1"], GatewayLink("b", frozenset({SHARED}), (peer("gB1"), peer("gB2")))),
        "gA2": _gateway(12, ports["gA2"], GatewayLink("b", frozenset({SHARED}), (peer("gB1"), peer("gB2")))),
        "gB1": _gateway(21, ports["gB1"], GatewayLink("a", frozenset({SHARED}), (peer("gA1"), peer("gA2")))),
        "gB2": _gateway(22, ports["gB2"], GatewayLink("a", frozenset({SHARED}), (peer("gA1"), peer("gA2")))),
    }
    nodes = {"a1": a1, "b1": b1, **gws}
    to_b1 = _spy(b1, ports)
    for n in names:
        await nodes[n].transport.start(ports[n])
    try:
        # Mesh A: a1, gA1, gA2 all know each other; same for mesh B.
        for mesh in (("a1", "gA1", "gA2"), ("b1", "gB1", "gB2")):
            for x in mesh:
                for y in mesh:
                    if x != y:
                        nodes[x].transport.add_peer(y, peer(y))
        b1.transport.send_encrypted(b1.ciphers[SHARED], SHARED, TYPE_CHANNEL_ANNOUNCE, os.urandom(8) + b"\x01")
        await asyncio.sleep(0.3)
        assert 2 in a1._channel_members.get(SHARED, {})
        sent_before = sum(g.gateway_status()[0]["sent"] for g in gws.values())

        for _ in range(5):
            a1.transport.send_encrypted(a1.ciphers[SHARED], SHARED, TYPE_AUDIO, b"opus-frame",
                                        targets=[a1._channel_members[SHARED][2][0]])
        await asyncio.sleep(0.3)
        audio = [p for p in to_b1 if p == (TYPE_AUDIO, SHARED)]
        assert 5 <= len(audio) <= 10   # each frame arrives, at most once per side-B gateway
        # Each frame crosses between the meshes once.
        assert sum(g.gateway_status()[0]["sent"] for g in gws.values()) - sent_before == 5
    finally:
        for n in names:
            nodes[n].transport.stop()
