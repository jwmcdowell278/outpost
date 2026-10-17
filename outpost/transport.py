"""UDP mesh transport: sends encrypted Opus frames to all known peers and
dispatches incoming packets to a callback. Peer-to-peer - no central server."""
from __future__ import annotations
import asyncio
import logging
import threading

from .protocol import Header, TYPE_AUDIO, VERSION, encode_packet, decode_packet
from .config import Peer

log = logging.getLogger(__name__)

# Keep packets well under the typical 1500-byte Ethernet MTU (minus IP/UDP
# headers) to avoid IP fragmentation, which would hurt reliability far more
# than it helps throughput for these small voice frames. Constrained links
# (e.g. LoRa-style mesh radio) should configure a much smaller value.
DEFAULT_MAX_PACKET_BYTES = 1200

# Default ceiling on how many peers we'll track (static + discovered +
# gossiped), so a spoofed/noisy network can't make our peer table (and
# per-packet fan-out) grow without bound. Configurable per deployment - a
# mesh with thousands of nodes needs this raised well above the default to
# avoid silently dropping legitimate peers/channel members.
DEFAULT_MAX_PEERS = 128


class _MeshProtocol(asyncio.DatagramProtocol):
    def __init__(self, on_packet):
        self._on_packet = on_packet

    def datagram_received(self, data: bytes, addr) -> None:
        try:
            header, ciphertext = decode_packet(data)
        except ValueError as exc:
            log.debug("dropping malformed packet from %s: %s", addr, exc)
            return
        self._on_packet(header, ciphertext, addr)

    def error_received(self, exc: Exception) -> None:
        log.warning("UDP error: %s", exc)


class MeshTransport:
    """Sends outgoing audio to all known peers and dispatches incoming packets.

    Peers can come from static config and/or be added/removed later (e.g. by
    mDNS discovery) - add_peer/remove_peer are safe to call from any thread.
    """

    def __init__(self, sender_id: int, peers: list[Peer], on_packet, max_packet_bytes: int = DEFAULT_MAX_PACKET_BYTES,
                 max_peers: int = DEFAULT_MAX_PEERS):
        self.sender_id = sender_id
        self.max_packet_bytes = max_packet_bytes
        self.max_peers = max_peers
        self._on_packet = on_packet
        self._transport: asyncio.DatagramTransport | None = None
        self._seq = 0
        self._lock = threading.Lock()
        self._peers: dict[str, Peer] = {f"static-{i}": p for i, p in enumerate(peers)}

    @property
    def peers(self) -> list[Peer]:
        with self._lock:
            return list(self._peers.values())

    def add_peer(self, key: str, peer: Peer) -> None:
        with self._lock:
            if key not in self._peers and any(p.host == peer.host and p.port == peer.port for p in self._peers.values()):
                return  # already tracked under a different key - avoid duplicate fan-out
            is_new = key not in self._peers
            if is_new and len(self._peers) >= self.max_peers:
                log.warning("peer table full (%d), ignoring new peer %s", self.max_peers, key)
                return
            self._peers[key] = peer
        if is_new:
            log.info("peer added: %s -> %s:%d", key, peer.host, peer.port)

    def remove_peer(self, key: str) -> None:
        with self._lock:
            removed = self._peers.pop(key, None)
        if removed:
            log.info("peer removed: %s", key)

    async def start(self, listen_port: int) -> None:
        loop = asyncio.get_running_loop()
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: _MeshProtocol(self._on_packet),
            local_addr=("0.0.0.0", listen_port),
        )

    def stop(self) -> None:
        if self._transport:
            self._transport.close()
            self._transport = None

    def next_seq(self) -> int:
        seq = self._seq
        self._seq += 1
        return seq

    def send(self, channel_id: int, seq: int, ciphertext: bytes, ptype: int = TYPE_AUDIO,
              targets: list[Peer] | None = None) -> None:
        """Send to `targets` if given, otherwise fan out to every known peer.

        Callers that know which peers actually care about a packet (e.g.
        channel-scoped audio - see client.py's `_channel_target_peers`)
        should pass `targets` so bandwidth scales with interested peers
        instead of total mesh size. Discovery/gossip traffic (PEX, channel
        announces) still needs the default full flood since the recipient
        set isn't known in advance.
        """
        if self._transport is None:
            raise RuntimeError("transport not started")
        header = Header(VERSION, ptype, channel_id, self.sender_id, seq)
        packet = encode_packet(header, ciphertext)
        if len(packet) > self.max_packet_bytes:
            log.warning("packet size %d exceeds budget of %d bytes", len(packet), self.max_packet_bytes)
        for peer in (targets if targets is not None else self.peers):
            try:
                self._transport.sendto(packet, (peer.host, peer.port))
            except OSError as exc:
                # A single unreachable/dropped peer (common on partially
                # connected mesh radio links) must not stop delivery to
                # everyone else.
                log.debug("send to %s:%d failed: %s", peer.host, peer.port, exc)

