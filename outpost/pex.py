"""Gossip-based peer exchange (PEX).

mDNS only works where IP multicast is reachable - typically a plain LAN.
Most VPNs (point-to-point tunnels) and multi-hop mesh radio networks don't
forward multicast even though unicast routing between nodes works fine. PEX
fixes discovery for those cases: each node periodically sends its current
peer list to every peer it already knows, so knowledge of the full mesh
propagates hop-by-hop from just one reachable bootstrap peer (static config
or an mDNS-found peer) - works identically over LAN, VPN mesh, or radio mesh,
since it only requires ordinary unicast UDP delivery.

Wire payload (before encryption): a sequence of entries, each
  1 byte   address family (4 = IPv4, 6 = IPv6)
  4|16     address bytes
  2        port (uint16, big-endian)
Encrypted with the shared mesh key (not a channel key - PEX is channel-
agnostic control-plane traffic) using the same per-(sender_id, seq) nonce
scheme as audio packets.
"""
from __future__ import annotations
import asyncio
import logging
import random
import socket
import struct

from .config import Peer
from .crypto import ChannelCipher
from .protocol import TYPE_PEX

log = logging.getLogger(__name__)

MAX_PEERS_PER_MESSAGE = 40


def pack_peers(peers: list[Peer]) -> bytes:
    out = bytearray()
    for peer in peers[:MAX_PEERS_PER_MESSAGE]:
        try:
            family, packed_addr = _pack_address(peer.host)
        except OSError:
            continue  # not a plain IPv4/IPv6 literal (e.g. unresolved hostname) - skip
        out += struct.pack("!B", family)
        out += packed_addr
        out += struct.pack("!H", peer.port)
    return bytes(out)


def unpack_peers(data: bytes) -> list[Peer]:
    peers: list[Peer] = []
    offset = 0
    while offset < len(data):
        if offset + 1 > len(data):
            break
        family = data[offset]
        offset += 1
        addr_len = 4 if family == 4 else 16 if family == 6 else None
        if addr_len is None or offset + addr_len + 2 > len(data):
            break  # malformed/truncated - stop parsing rather than guess
        addr_bytes = data[offset:offset + addr_len]
        offset += addr_len
        (port,) = struct.unpack("!H", data[offset:offset + 2])
        offset += 2
        host = socket.inet_ntop(socket.AF_INET if family == 4 else socket.AF_INET6, addr_bytes)
        peers.append(Peer(host=host, port=port))
    return peers


def _pack_address(host: str) -> tuple[int, bytes]:
    try:
        return 4, socket.inet_pton(socket.AF_INET, host)
    except OSError:
        return 6, socket.inet_pton(socket.AF_INET6, host)


class PexGossip:
    """Periodically gossips the known peer list and ingests others' gossip."""

    def __init__(self, sender_id: int, mesh_key: bytes, transport, interval_s: float = 30.0, fanout: int = 8,
                 share_filter=None):
        self._sender_id = sender_id
        # Optional (target, peer) -> bool: whether to tell `target` about
        # `peer`. Bridges use it to keep site-local addresses on their site
        # and VPN addresses among bridges (see OutpostClient._same_side).
        self._share_filter = share_filter
        self._cipher = ChannelCipher(mesh_key)
        self._transport = transport
        self._interval_s = interval_s
        self._fanout = fanout
        self._seq = 0
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._gossip_loop())

    def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    async def _gossip_loop(self) -> None:
        # Jitter the first announce so many nodes starting at once (e.g. a
        # fresh deployment) don't all transmit in lockstep on a shared/
        # bandwidth-constrained radio channel.
        await asyncio.sleep(random.uniform(0, self._interval_s))
        while True:
            self._announce()
            await asyncio.sleep(self._interval_s * random.uniform(0.8, 1.2))

    def _announce(self) -> None:
        peers = self._transport.peers
        if not peers:
            return
        # Gossip to a bounded random subset each round (classic epidemic/
        # fanout gossip) rather than every known peer - this keeps each
        # node's control-plane send cost constant instead of growing with
        # total mesh size, while still reaching everyone within a few
        # rounds via hop-by-hop propagation. The gossiped peer LIST itself
        # (the payload) is unaffected - only the number of recipients.
        targets = peers if len(peers) <= self._fanout else random.sample(peers, self._fanout)
        self._send_to(targets)

    def probe(self, host: str, port: int) -> None:
        """Sends our peer list directly to one address regardless of
        whether it's already a known peer - used by SubnetScanner to find
        peers on a subnet mDNS can't reach (e.g. a VPN tunnel/batman-adv
        segment) without a static peers: entry. A real Outpost node
        sharing mesh_key at that address will authenticate this and confirm
        us as a peer, after which its own next periodic gossip round (see
        _announce) reaches us back - the same confirmation path as any
        other PEX-discovered peer, just seeded by a direct probe instead of
        a chain of existing peers."""
        self._send_to([Peer(host=host, port=port)])

    def _send_to(self, targets: list[Peer]) -> None:
        # Only gossip peers we've heard from ourselves - passing on
        # second-hand addresses is how dead peers used to circulate forever.
        live = self._transport.live_peers
        if self._share_filter is None:
            self._transport.send_encrypted(self._cipher, 0, TYPE_PEX, pack_peers(live), targets=targets)
            return
        for target in targets:
            shared = [p for p in live if self._share_filter(target, p)]
            self._transport.send_encrypted(self._cipher, 0, TYPE_PEX, pack_peers(shared), targets=[target])

    def handle_incoming(self, header, ciphertext: bytes, addr) -> None:
        try:
            plaintext = self._cipher.decrypt(header, ciphertext)
        except ValueError:
            log.debug("dropping PEX packet with bad auth tag from %s", addr)
            return

        # The sender itself is a confirmed, authenticated peer.
        sender = Peer(host=addr[0], port=addr[1])
        was_known = self._transport.is_confirmed_sender(header.sender_id)
        confirmed = self._transport.confirm_peer(header.sender_id, sender)
        if confirmed and not was_known:
            # First contact (e.g. a subnet-scan probe, or a node that just
            # learned of us): answer straight away so it confirms us too,
            # instead of waiting up to a full gossip interval - which could
            # outlast the grace period a new, not-yet-heard peer gets before
            # expiring. Only on first contact, so two nodes can't ping-pong.
            self._send_to([sender])

        for gossiped in unpack_peers(plaintext):
            self._transport.add_peer(f"gossip-{gossiped.host}:{gossiped.port}", gossiped)
