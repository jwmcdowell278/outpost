"""Channel patching (OutpostConfig.patches, outpost/patch.py): a node holding
two channels' keys re-sends one channel's traffic onto the other, under a
stand-in sender id per talker, with the original sequence numbers."""
from __future__ import annotations
import asyncio
import base64
import socket

import pytest

from outpost.client import OutpostClient
from outpost.config import (OutpostConfig, Channel, GatewayLink, Patch, Peer,
                            load_config, load_patches, validate_config_yaml)
from outpost.crypto import generate_channel_key
from outpost.protocol import TYPE_AUDIO, TYPE_CHANNEL_ANNOUNCE, TYPE_START, TYPE_STOP, pad_audio

OPS, TAC, MED, JOINT, CMD = 3, 4, 5, 201, 12
KEYS = {cid: generate_channel_key() for cid in (OPS, TAC, MED, JOINT, CMD)}
NAMES = {OPS: "ops", TAC: "tac", MED: "med", JOINT: "joint", CMD: "cmd"}


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _node(sender_id, port, channel_ids, active=None, patches=(), relay_only=False, gateways=()):
    config = OutpostConfig(
        sender_id=sender_id, listen_port=port, mesh_key=generate_channel_key(),
        channels={c: Channel(id=c, name=NAMES[c], key=KEYS[c]) for c in channel_ids},
        enable_mdns=False, enable_pex=False, enable_multicast=False,
        relay_only=relay_only, patches=list(patches), gateways=list(gateways),
    )
    return OutpostClient(config, None if relay_only else NAMES[active or channel_ids[0]])


class _Net:
    """Named nodes on localhost, started and stopped together."""

    def __init__(self):
        self.nodes: dict[str, OutpostClient] = {}
        self.ports: dict[str, int] = {}
        self.played: dict[str, list[tuple[int, int, int]]] = {}

    def port(self, name: str) -> int:
        return self.ports.setdefault(name, _free_port())

    def peer(self, name: str) -> Peer:
        return Peer("127.0.0.1", self.port(name))

    def add(self, name, *args, **kwargs) -> OutpostClient:
        node = _node(args[0], self.port(name), *args[1:], **kwargs)
        self.nodes[name] = node
        played = self.played[name] = []
        push = node.mixer.push

        def spy(channel_id, sender_id, seq, opus):
            played.append((channel_id, sender_id, seq))
            push(channel_id, sender_id, seq, opus)
        node.mixer.push = spy
        return node

    def link(self, *names) -> None:
        for a in names:
            for b in names:
                if a != b:
                    self.nodes[a].transport.add_peer(b, self.peer(b))

    async def __aenter__(self):
        for name, node in self.nodes.items():
            await node.transport.start(self.ports[name])
        return self

    async def __aexit__(self, *exc):
        for node in self.nodes.values():
            node.transport.stop()

    def announce(self, name: str, channel_id: int) -> None:
        self.nodes[name]._send_announce(channel_id)

    def talk(self, name: str, channel_id: int, frames: int = 3, alias: bytes = b"") -> None:
        node = self.nodes[name]
        cipher = node.ciphers[channel_id]
        node.transport.send_encrypted(cipher, channel_id, TYPE_START, alias)
        for i in range(frames):
            payload = pad_audio(b"%s-frame-%d" % (name.encode(), i), None)
            node.transport.send_encrypted(cipher, channel_id, TYPE_AUDIO, payload)
        node.transport.send_encrypted(cipher, channel_id, TYPE_STOP, b"")


async def _settle():
    await asyncio.sleep(0.25)


def test_two_way_patch_joins_two_channels():
    asyncio.run(_two_way())


async def _two_way():
    net = _Net()
    alice = net.add("alice", 1, [OPS])
    bob = net.add("bob", 2, [TAC])
    p = net.add("p", 9, [OPS, TAC], patches=[Patch(OPS, TAC)], relay_only=True)
    net.link("alice", "bob", "p")
    async with net:
        for name, ch in (("alice", OPS), ("bob", TAC), ("p", OPS), ("p", TAC)):
            net.announce(name, ch)
        await _settle()

        net.talk("alice", OPS, alias=b"Alice")
        await _settle()
        heard = [x for x in net.played["bob"] if x[0] == TAC]
        assert len(heard) == 3
        vids = {sender for _, sender, _ in heard}
        assert len(vids) == 1 and 1 not in vids          # a stand-in id, not alice's
        assert p.patches.is_virtual(vids.pop())
        seqs = [seq for _, _, seq in heard]
        assert seqs == sorted(seqs) and seqs[-1] - seqs[0] == 2   # original, contiguous seqs
        # The START carried who's talking; the STOP ended the stream.
        assert not bob._channel_speaking.get(TAC)
        assert p.patches.counts["patched"] == 3

        net.talk("bob", TAC, alias=b"Bob")
        await _settle()
        assert len([x for x in net.played["alice"] if x[0] == OPS]) == 3


def test_patched_talker_is_named_via_the_source_channel():
    asyncio.run(_alias())


async def _alias():
    net = _Net()
    net.add("alice", 1, [OPS])
    bob = net.add("bob", 2, [TAC])
    net.add("p", 9, [OPS, TAC], patches=[Patch(OPS, TAC)], relay_only=True)
    net.link("alice", "bob", "p")
    async with net:
        for name, ch in (("bob", TAC), ("p", OPS)):
            net.announce(name, ch)
        await _settle()
        node = net.nodes["alice"]
        cipher = node.ciphers[OPS]
        node.transport.send_encrypted(cipher, OPS, TYPE_START, b"Alice")
        node.transport.send_encrypted(cipher, OPS, TYPE_AUDIO, pad_audio(b"x", None))
        await _settle()
        assert [alias for alias, _ in bob._channel_speaking[TAC].values()] == ["Alice via ops"]


def test_one_way_patch_only_carries_one_direction():
    asyncio.run(_one_way())


async def _one_way():
    net = _Net()
    net.add("alice", 1, [OPS])
    net.add("bob", 2, [TAC])
    net.add("p", 9, [OPS, TAC], patches=[Patch(OPS, TAC, two_way=False)], relay_only=True)
    net.link("alice", "bob", "p")
    async with net:
        for name, ch in (("alice", OPS), ("bob", TAC), ("p", OPS), ("p", TAC)):
            net.announce(name, ch)
        await _settle()
        net.talk("bob", TAC)
        await _settle()
        assert not [x for x in net.played["alice"] if x[0] == OPS]
        net.talk("alice", OPS)
        await _settle()
        assert len([x for x in net.played["bob"] if x[0] == TAC]) == 3


def test_a_ring_of_patches_stops_after_one_lap():
    asyncio.run(_ring())


async def _ring():
    # ops -> tac -> med -> ops, each on a different node.
    net = _Net()
    net.add("alice", 1, [OPS])
    p1 = net.add("p1", 11, [OPS, TAC], patches=[Patch(OPS, TAC, False)], relay_only=True)
    p2 = net.add("p2", 12, [TAC, MED], patches=[Patch(TAC, MED, False)], relay_only=True)
    p3 = net.add("p3", 13, [MED, OPS], patches=[Patch(MED, OPS, False)], relay_only=True)
    net.link("alice", "p1", "p2", "p3")
    async with net:
        for name, ch in (("p1", OPS), ("p2", TAC), ("p3", MED), ("alice", OPS)):
            net.announce(name, ch)
        await _settle()
        net.talk("alice", OPS, frames=4)
        await asyncio.sleep(0.5)
        # Each node patched each frame once; the frames that came round
        # again to p1 were recognised and dropped.
        assert [p.patches.counts["patched"] for p in (p1, p2, p3)] == [4, 4, 4]
        assert p1.patches.counts["looped"] == 4


def test_redundant_patch_nodes_dont_echo_and_one_stands_by():
    asyncio.run(_redundant())


async def _redundant():
    net = _Net()
    net.add("alice", 1, [OPS])
    net.add("bob", 2, [TAC])
    p1 = net.add("p1", 11, [OPS, TAC], patches=[Patch(OPS, TAC)], relay_only=True)
    p2 = net.add("p2", 12, [OPS, TAC], patches=[Patch(OPS, TAC)], relay_only=True)
    net.link("alice", "bob", "p1", "p2")
    async with net:
        for name, ch in (("alice", OPS), ("bob", TAC), ("p1", OPS), ("p1", TAC), ("p2", OPS), ("p2", TAC)):
            net.announce(name, ch)
        await _settle()
        # Without hellos both run the patch: bob gets it twice, but nothing
        # echoes back onto ops.
        net.talk("alice", OPS)
        await _settle()
        assert len([x for x in net.played["bob"] if x[0] == TAC]) == 6
        assert not [x for x in net.played["alice"] if x[0] == OPS]

        # p2 hears p1's hello (p1 outranks it: same priority, lower sender_id).
        p2.patches.on_hello(11, p1.patches.hello_payload())
        assert not p2.patches.is_active((OPS, TAC)) and p1.patches.is_active((OPS, TAC))
        net.played["bob"].clear()
        net.talk("alice", OPS)
        await _settle()
        heard = [x for x in net.played["bob"] if x[0] == TAC]
        assert len(heard) == 3 and all(p1.patches.is_virtual(s) for _, s, _ in heard)


def test_patch_hello_round_trip_over_the_network():
    asyncio.run(_hello())


async def _hello():
    net = _Net()
    p1 = net.add("p1", 11, [OPS, TAC], patches=[Patch(OPS, TAC)], relay_only=True)
    p2 = net.add("p2", 12, [OPS, TAC], patches=[Patch(OPS, TAC)], relay_only=True)
    # Same mesh: same mesh key.
    p2._mesh_cipher = p1._mesh_cipher
    net.link("p1", "p2")
    async with net:
        task = asyncio.create_task(p1._patch_hello_loop())
        await _settle()
        task.cancel()
        assert not p2.patches.is_active((OPS, TAC))
        assert not p2.patches.is_active((TAC, OPS))


def test_patch_across_a_gateway_link_needs_no_shared_channel_on_the_members():
    asyncio.run(_across())


async def _across():
    # Mesh A: alice on ops. Mesh B: carol on cmd. Only the gateways hold
    # the shared channel (joint) - alice and carol keep their own channels.
    net = _Net()
    net.add("alice", 1, [OPS])
    net.add("carol", 3, [CMD])
    gA = net.add("gA", 10, [OPS, JOINT], patches=[Patch(OPS, JOINT)], relay_only=True,
                 gateways=[GatewayLink("b", frozenset({JOINT}), (net.peer("gB"),))])
    gB = net.add("gB", 20, [JOINT, CMD], patches=[Patch(JOINT, CMD)], relay_only=True,
                 gateways=[GatewayLink("a", frozenset({JOINT}), (net.peer("gA"),))])
    net.link("alice", "gA")
    net.link("carol", "gB")
    async with net:
        for name, ch in (("alice", OPS), ("carol", CMD), ("gA", OPS), ("gA", JOINT),
                         ("gB", JOINT), ("gB", CMD)):
            net.announce(name, ch)
        await _settle()
        net.talk("alice", OPS)
        await _settle()
        assert len([x for x in net.played["carol"] if x[0] == CMD]) == 3
        net.talk("carol", CMD)
        await _settle()
        assert len([x for x in net.played["alice"] if x[0] == OPS]) == 3
        # Neither mesh's own channels crossed the link.
        assert gA.gateway_status()[0]["refused"] == 0 and gB.gateway_status()[0]["refused"] == 0


def test_live_patch_changes():
    p = _node(9, 0, [OPS, TAC, MED], patches=[Patch(OPS, TAC)])
    assert p.patches.is_source(OPS) and not p.patches.is_source(MED)
    p.set_patches([Patch(TAC, MED, two_way=False)])
    assert p.patches.is_source(TAC) and not p.patches.is_source(OPS) and not p.patches.is_source(MED)
    with pytest.raises(ValueError, match="no key"):
        p.set_patches([Patch(OPS, JOINT)])
    p.set_patches([Patch(OPS, JOINT)], channels={JOINT: Channel(JOINT, "joint", KEYS[JOINT])})
    assert p.patches.is_source(JOINT)


# --- config -----------------------------------------------------------------

_CHANNELS = [{"id": 3, "name": "ops"}, {"id": 4, "name": "tac"}, {"id": 201, "name": "joint"}]


def test_load_patches_forms():
    assert load_patches([["ops", "tac"], {"from": 201, "to": "ops"}], _CHANNELS) == [
        Patch(3, 4, True), Patch(201, 3, False)]
    assert load_patches(None, _CHANNELS) == []


@pytest.mark.parametrize("patches, message", [
    ([["ops", "nope"]], "isn't a configured channel"),
    ([["ops", "ops"]], "itself"),
    ([["ops", "tac"], {"from": "tac", "to": "ops"}], "overlaps"),
    ([["ops"]], "two-way"),
    ([{"from": "ops"}], "one-way"),
    ("ops", "must be a list"),
])
def test_load_patches_rejects(patches, message):
    with pytest.raises(ValueError, match=message):
        load_patches(patches, _CHANNELS)


def test_relay_only_keeps_only_patched_channel_keys(tmp_path):
    key = lambda: base64.b64encode(generate_channel_key()).decode()
    path = tmp_path / "outpost.yaml"
    path.write_text(f"""
mesh_key: "{key()}"
relay_only: true
channels:
  - {{id: 3, name: ops, key: "{key()}"}}
  - {{id: 4, name: tac, key: "{key()}"}}
  - {{id: 201, name: joint, key: "{key()}"}}
patches:
  - [ops, joint]
patch_priority: 150
""")
    config = load_config(str(path))
    assert sorted(config.channels) == [3, 201]
    assert config.patches == [Patch(3, 201)]
    assert config.patch_priority == 150
    client = OutpostClient(config, None)
    assert sorted(client.ciphers) == [3, 201]


def test_validate_config_yaml_checks_patches():
    key = base64.b64encode(generate_channel_key()).decode()
    with pytest.raises(ValueError, match="configured channel"):
        validate_config_yaml(f"""
mesh_key: "{key}"
channels: [{{id: 1, name: ops, key: "{key}"}}]
patches: [[ops, tac]]
""")
