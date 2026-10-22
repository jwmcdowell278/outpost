"""Multicast delivery within a site (enable_multicast): one copy per frame
to the channel's group for on-site listeners, unicast only for the rest."""
from __future__ import annotations
import asyncio
import random
import time

import pytest

from outpost.client import OutpostClient
from outpost.config import OutpostConfig, Channel, Peer
from outpost.crypto import ChannelCipher, generate_channel_key
from outpost.protocol import Header, VERSION, TYPE_AUDIO, TYPE_CHANNEL_ANNOUNCE, pad_audio

MAIN, TEAM = 1, 2


def _make(sender_id, keys, mesh_key, multicast=True, port=None, **overrides):
    cfg = dict(
        sender_id=sender_id, listen_port=0, mesh_key=mesh_key,
        channels={MAIN: Channel(MAIN, "main", keys[MAIN]), TEAM: Channel(TEAM, "team", keys[TEAM])},
        enable_mdns=False, enable_pex=False,
        enable_multicast=multicast, multicast_interface="127.0.0.1",
        multicast_port=port or 0, multicast_min_listeners=3,
        channel_announce_interval_s=0.2,
    )
    cfg.update(overrides)
    return OutpostClient(OutpostConfig(**cfg), "main")


async def _start(client):
    """Network side of OutpostClient.start() without opening the mic/speaker."""
    await client.transport.start(0)
    if client.config.enable_multicast:
        await client.transport.start_multicast(client.config.multicast_port, "127.0.0.1")
        client._update_groups()
    client._channel_announce_task = asyncio.create_task(client._channel_announce_loop())


def _port(client):
    return client.transport.listen_port


async def _site_scenario():
    keys = {MAIN: generate_channel_key(), TEAM: generate_channel_key()}
    mesh_key = generate_channel_key()
    mport = random.randint(40000, 60000)
    site = [_make(i, keys, mesh_key, port=mport) for i in range(1, 8)]
    remote = _make(99, keys, mesh_key, multicast=False)
    nodes = site + [remote]
    heard = {n.config.sender_id: [] for n in nodes}
    for n in nodes:
        n.mixer.push = (lambda sid: lambda ch, s, seq, opus: heard[sid].append(s))(n.config.sender_id)
        n.transport.confirm_peer  # real
    for n in nodes:
        await _start(n)
    # Static links so unicast paths exist: everyone knows the remote node
    # (as over a VPN) and the remote knows everyone.
    for n in site:
        n.transport.add_peer("static-remote", Peer("127.0.0.1", _port(remote)))
        remote.transport.add_peer(f"static-{n.config.sender_id}", Peer("127.0.0.1", _port(n)))

    talker = site[0]
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        targets, group = talker._channel_delivery(MAIN)
        if group and targets is not None and len(talker._channel_listeners(MAIN)) == 7:
            break
        await asyncio.sleep(0.1)
    return talker, site, remote, heard, nodes


def test_large_channel_uses_one_multicast_plus_unicast_only_off_site():
    async def run():
        talker, site, remote, heard, nodes = await _site_scenario()
        try:
            targets, group = talker._channel_delivery(MAIN)
            assert group is not None, "talker never switched to multicast"
            assert [(p.host, p.port) for p in targets] == [("127.0.0.1", _port(remote))]

            unicast_sent = []
            orig_send = talker.transport.send
            talker.transport.send = lambda *a, **k: (
                unicast_sent.append(k.get("targets")) if k.get("ptype") == TYPE_AUDIO else None, orig_send(*a, **k))
            opus = talker.codec.encode(b"\x00\x00" * talker.codec.frame_size)
            talker.transport.send_encrypted(talker.ciphers[MAIN], MAIN, TYPE_AUDIO, pad_audio(opus, None),
                                            targets=targets, multicast_group=group)
            await asyncio.sleep(0.3)

            assert len(unicast_sent) == 1 and len(unicast_sent[0]) == 1  # one unicast copy, to the remote
            for n in nodes[1:]:
                assert talker.config.sender_id in heard[n.config.sender_id], f"node {n.config.sender_id} missed it"
        finally:
            for n in nodes:
                n._channel_announce_task.cancel()
                n.transport.stop()
    asyncio.run(run())


def test_small_channel_stays_unicast():
    async def run():
        talker, site, remote, heard, nodes = await _site_scenario()
        try:
            talker.multicast_min_listeners = 100
            targets, group = talker._channel_delivery(MAIN)
            assert group is None and len(targets) == 7
        finally:
            for n in nodes:
                n._channel_announce_task.cancel()
                n.transport.stop()
    asyncio.run(run())


def test_nodes_only_join_groups_of_channels_they_listen_to():
    async def run():
        keys = {MAIN: generate_channel_key(), TEAM: generate_channel_key()}
        n = _make(1, keys, generate_channel_key(), port=random.randint(40000, 60000))
        await _start(n)
        try:
            assert n.transport._mcast_groups == {n._group(0), n._group(MAIN)}
            n.set_monitor("team", True)
            assert n._group(TEAM) in n.transport._mcast_groups
            n.set_monitor("team", False)
            assert n._group(TEAM) not in n.transport._mcast_groups
        finally:
            n._channel_announce_task.cancel()
            n.transport.stop()
    asyncio.run(run())


# --- unit-level ----------------------------------------------------------

def _client(**kw):
    keys = {MAIN: generate_channel_key(), TEAM: generate_channel_key()}
    c = _make(1, keys, generate_channel_key(), **kw)
    c.transport.confirm_peer = lambda *a: True
    return c, keys


def _announce(client, key, sender, seq, payload, addr, multicast=False):
    h = Header(VERSION, TYPE_CHANNEL_ANNOUNCE, MAIN, sender, seq)
    client._on_packet(h, ChannelCipher(key).encrypt(h, payload), addr, via_multicast=multicast)


def test_members_that_hold_the_key_but_dont_listen_get_no_audio():
    c, keys = _client(multicast=False)
    _announce(c, keys[MAIN], 7, 1, b"t" * 8 + b"\x01", ("10.0.0.7", 5555))  # listening
    _announce(c, keys[MAIN], 8, 1, b"t" * 8 + b"\x00", ("10.0.0.8", 5555))  # key only
    targets, group = c._channel_delivery(MAIN)
    assert [(p.host) for p in targets] == ["10.0.0.7"] and group is None


def test_older_nodes_without_flags_byte_are_assumed_listening():
    c, keys = _client(multicast=False)
    _announce(c, keys[MAIN], 7, 1, b"t" * 8, ("10.0.0.7", 5555))
    assert [p.host for p in c._channel_delivery(MAIN)[0]] == ["10.0.0.7"]


def test_listen_change_announces_immediately():
    c, keys = _client(multicast=False)
    sent = []
    c.transport.send_encrypted = lambda cipher, ch, ptype, payload, **k: sent.append((ch, ptype, payload[8]))
    c.set_monitor("team", True)
    c.set_monitor("team", False)
    assert sent == [(TEAM, TYPE_CHANNEL_ANNOUNCE, 1), (TEAM, TYPE_CHANNEL_ANNOUNCE, 0)]


def test_relay_does_not_unicast_a_multicast_packet_back_to_the_same_site():
    c, keys = _client(multicast=False, enable_relay=True)
    forwarded = []
    c.transport.forward = lambda packet, targets: forwarded.append({p.host for p in targets})
    for sid, host in ((7, "10.0.0.7"), (8, "10.0.0.8")):
        c.transport.add_peer(f"gossip-{host}", Peer(host, 5555))
        _announce(c, keys[MAIN], sid, 1, b"t" * 8 + b"\x01", (host, 5555), multicast=True)
    c.transport.add_peer("gossip-vpn", Peer("10.8.0.2", 5555))
    _announce(c, keys[MAIN], 50, 1, b"t" * 8 + b"\x01", ("10.8.0.2", 5555))  # off-site via VPN
    forwarded.clear()
    h = Header(VERSION, TYPE_AUDIO, MAIN, 7, 2)
    c._on_packet(h, b"\x00" * 40, ("10.0.0.7", 5555), via_multicast=True)
    assert forwarded == [{"10.8.0.2"}]
