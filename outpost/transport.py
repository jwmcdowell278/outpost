"""UDP mesh transport: sends encrypted Opus frames to all known peers and
dispatches incoming packets to a callback. Peer-to-peer - no central server."""
from __future__ import annotations
import asyncio
import logging
import threading
import time

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
        # Static (config-file) peers are a deliberate, operator-chosen
        # bootstrap list and are kept forever even if quiet; every other key
        # ("peer-*", "gossip-*", mDNS) is refreshed on each sighting and can
        # go stale - see prune_stale().
        self._last_seen: dict[str, float] = {k: time.monotonic() for k in self._peers}
        # key -> every known candidate address for that one logical peer,
        # preferred/shortest-path first (e.g. a dual-homed device reachable
        # on both the local subnet and a VPN tunnel - see add_peer_group).
        # _peers[key] always holds whichever candidate is currently active.
        self._candidates: dict[str, list[Peer]] = {k: [p] for k, p in self._peers.items()}

    @property
    def peers(self) -> list[Peer]:
        """One entry per logical peer (its currently-active address) - a peer
        with several candidate addresses (see add_peer_group) still counts
        and fans out as a single peer, not one per address."""
        with self._lock:
            return list(self._peers.values())

    def add_peer(self, key: str, peer: Peer) -> None:
        self.add_peer_group(key, [peer])

    def add_peer_group(self, key: str, candidates: list[Peer]) -> None:
        """Register one logical peer that may be reachable via several
        candidate addresses (e.g. a device with both a LAN and a VPN
        interface). `candidates` should be ordered preferred-first (shortest/
        most-direct path first) - sends use the first candidate, falling
        back to the next on a socket-level send failure (see send())."""
        if not candidates:
            return
        primary = candidates[0]
        with self._lock:
            if key not in self._peers and any(p.host == primary.host and p.port == primary.port for p in self._peers.values()):
                return  # already tracked under a different key - avoid duplicate fan-out
            is_new = key not in self._peers
            if is_new and len(self._peers) >= self.max_peers:
                log.warning("peer table full (%d), ignoring new peer %s", self.max_peers, key)
                return
            self._peers[key] = primary
            self._candidates[key] = list(candidates)
            self._last_seen[key] = time.monotonic()
        if is_new:
            log.info("peer added: %s -> %s:%d (%d candidate address(es))", key, primary.host, primary.port, len(candidates))

    def confirm_peer(self, sender_id: int, peer: Peer) -> None:
        """Record `peer` as belonging to `sender_id`. Only call this for an
        address an authenticated packet actually arrived from - sender_id
        and raw UDP source addresses are otherwise trivially spoofable, so
        this must never be driven by unauthenticated sources (mDNS, PEX
        gossip) directly.

        If `peer`'s address is already tracked as a candidate of some other,
        not-yet-identified group (e.g. one of several addresses mDNS saw for
        one device), that whole candidate list - which may include OTHER
        addresses for the same physical device, like a secondary VPN
        interface - is adopted under this sender_id's identity key and the
        old, identity-less key is retired. This is how a peer first seen via
        mDNS/gossip gets permanently tied to its real sender_id once any one
        of its addresses is cryptographically confirmed."""
        key = f"peer-{sender_id}"
        with self._lock:
            donor_key = None
            candidates = [peer]
            for k, group in self._candidates.items():
                if k == key:
                    continue
                if any(c.host == peer.host and c.port == peer.port for c in group):
                    donor_key = k
                    candidates = [peer] + [c for c in group if (c.host, c.port) != (peer.host, peer.port)]
                    break
            if donor_key is not None:
                self._peers.pop(donor_key, None)
                self._last_seen.pop(donor_key, None)
                self._candidates.pop(donor_key, None)
            is_new = key not in self._peers
            if is_new and len(self._peers) >= self.max_peers:
                log.warning("peer table full (%d), ignoring confirmed peer %s", self.max_peers, key)
                return
            self._peers[key] = peer
            self._candidates[key] = candidates
            self._last_seen[key] = time.monotonic()
        if donor_key is not None:
            log.info("peer %s: tied to confirmed sender_id (merged %d address(es) from %s)", key, len(candidates), donor_key)
        elif is_new:
            log.info("peer confirmed: %s -> %s:%d", key, peer.host, peer.port)

    def prune_stale(self, max_age_s: float) -> None:
        """Drop non-static peer entries not refreshed within max_age_s.

        Second-hand sources (PEX gossip, mDNS) key entries by address, not
        sender identity, so a peer that moves/restarts leaves its old
        address behind as a permanent ghost entry unless something expires
        it - this inflates the peer count and wastes send fan-out on a dead
        address. Static config entries are exempt since those are a
        deliberate bootstrap list that should persist even if quiet."""
        cutoff = time.monotonic() - max_age_s
        with self._lock:
            stale = [k for k, last_seen in self._last_seen.items()
                     if not k.startswith("static-") and last_seen < cutoff]
            for key in stale:
                self._peers.pop(key, None)
                self._last_seen.pop(key, None)
                self._candidates.pop(key, None)
        for key in stale:
            log.info("peer expired (stale): %s", key)

    def remove_peer(self, key: str) -> None:
        with self._lock:
            removed = self._peers.pop(key, None)
            self._last_seen.pop(key, None)
            self._candidates.pop(key, None)
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
        if targets is not None:
            for peer in targets:
                try:
                    self._transport.sendto(packet, (peer.host, peer.port))
                except OSError as exc:
                    # A single unreachable/dropped peer (common on partially
                    # connected mesh radio links) must not stop delivery to
                    # everyone else.
                    log.debug("send to %s:%d failed: %s", peer.host, peer.port, exc)
            return
        with self._lock:
            keys = list(self._peers.keys())
        for key in keys:
            self._send_to_peer_with_failover(key, packet)

    def _send_to_peer_with_failover(self, key: str, packet: bytes) -> None:
        """Send to a peer's current primary address; on a socket-level
        failure (e.g. a VPN interface that just went down), try its other
        known candidate addresses and promote the first one that works, so
        later frames don't keep retrying a dead path every call."""
        with self._lock:
            primary = self._peers.get(key)
            candidates = self._candidates.get(key, [])
        if primary is None:
            return
        try:
            self._transport.sendto(packet, (primary.host, primary.port))
            return
        except OSError as exc:
            log.debug("send to %s (%s:%d) failed, trying fallback address: %s", key, primary.host, primary.port, exc)
        for candidate in candidates:
            if candidate.host == primary.host and candidate.port == primary.port:
                continue
            try:
                self._transport.sendto(packet, (candidate.host, candidate.port))
            except OSError as exc:
                log.debug("fallback send to %s (%s:%d) failed: %s", key, candidate.host, candidate.port, exc)
                continue
            with self._lock:
                self._peers[key] = candidate
            log.info("peer %s: switched to fallback address %s:%d", key, candidate.host, candidate.port)
            return

    def forward(self, packet: bytes, targets: list[Peer]) -> None:
        """Resend an already-encoded packet verbatim (header untouched, so
        the original sender_id/seq a receiver needs for decryption/replay
        checks is preserved) - used by relay-enabled nodes to bridge two
        otherwise-unreachable peer groups without re-originating the packet
        under their own identity. See client.py's `_relay_packet`."""
        if self._transport is None:
            raise RuntimeError("transport not started")
        for peer in targets:
            try:
                self._transport.sendto(packet, (peer.host, peer.port))
            except OSError as exc:
                log.debug("relay forward to %s:%d failed: %s", peer.host, peer.port, exc)

