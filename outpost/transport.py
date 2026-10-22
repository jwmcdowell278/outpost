"""UDP mesh transport: sends encrypted Opus frames to all known peers and
dispatches incoming packets to a callback. Peer-to-peer - no central server."""
from __future__ import annotations
import asyncio
import logging
import os
import threading
import time

import ifaddr

from .protocol import Header, TYPE_AUDIO, VERSION, UnsupportedVersion, encode_packet, decode_packet
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

# How far past the last persisted seq high-water mark to resume counting
# from on startup - must comfortably exceed how many packets could plausibly
# have been sent between the last flush and an unclean shutdown (crash/power
# loss), so a restart can never replay a seq (and thus a nonce) already used
# under the current channel key. At the voice path's frame rate (e.g. 50
# packets/sec for 20ms frames) this is ~1.5 hours of margin.
_SEQ_PERSIST_MARGIN = 256_000

# Flush the seq high-water mark to disk every this many next_seq() calls,
# rather than on every single call - disk I/O on every 20ms audio frame
# would be wasteful. A larger value only costs a larger (but still bounded
# by the margin above) resume gap after an unclean shutdown.
_SEQ_PERSIST_EVERY = 1000

# How long a cached snapshot of this host's own local addresses is trusted
# before being recomputed - see MeshTransport._is_own_address. Interfaces
# can come and go (e.g. a VPN tunnel), but checking this on every packet
# would mean re-enumerating adapters ~50 times/sec during active audio.
_OWN_ADDRESS_CACHE_TTL_S = 30.0


def _local_addresses() -> set[str]:
    """Every IP address (any family) currently assigned to a local
    interface, plus loopback - used to recognize our own address in a
    gossiped/discovered peer candidate. Best-effort: an enumeration failure
    just means we temporarily can't self-filter, not a hard error."""
    hosts = {"127.0.0.1", "::1", "0.0.0.0"}
    try:
        for adapter in ifaddr.get_adapters():
            for ip in adapter.ips:
                addr = ip.ip[0] if isinstance(ip.ip, tuple) else ip.ip
                hosts.add(addr)
    except OSError:
        pass
    return hosts


# Seconds between repeated "incompatible version" warnings for one host.
_VERSION_WARN_INTERVAL_S = 300.0


class _MeshProtocol(asyncio.DatagramProtocol):
    def __init__(self, on_packet):
        self._on_packet = on_packet
        self._version_warned_at: dict[str, float] = {}

    def datagram_received(self, data: bytes, addr) -> None:
        try:
            header, ciphertext = decode_packet(data)
        except UnsupportedVersion as exc:
            # Visible (not debug): a half-upgraded mesh otherwise just goes
            # quiet between old and new nodes with no clue why.
            now = time.monotonic()
            if now - self._version_warned_at.get(addr[0], -1e9) >= _VERSION_WARN_INTERVAL_S:
                self._version_warned_at[addr[0]] = now
                log.warning("ignoring %s:%d - it runs Outpost protocol version %d, this node runs %d; "
                            "upgrade every node to the same version", addr[0], addr[1], exc.version, VERSION)
            return
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
                 max_peers: int = DEFAULT_MAX_PEERS, seq_persist_path: str | None = None):
        self.sender_id = sender_id
        self.max_packet_bytes = max_packet_bytes
        self.max_peers = max_peers
        self._on_packet = on_packet
        self._transport: asyncio.DatagramTransport | None = None
        # The encryption nonce is derived from (sender_id, seq) - see
        # crypto.py - which is only safe as long as seq never repeats for
        # this sender under the same channel key. sender_id is persisted
        # across restarts (see config._resolve_sender_id) specifically so it
        # doesn't collide with another node, but a plain in-memory counter
        # starting back at 0 on every restart would then reuse the exact
        # nonces from the previous run with the same persistent channel
        # key - a real nonce-reuse break, and separately it makes every
        # peer's stale/duplicate check (client.py's `_last_seq_seen`) treat
        # this node's post-restart packets as old duplicates of its own
        # higher pre-restart seq, silently dropping its audio until the
        # counter climbs back past that high-water mark (or a STOP happens
        # to get through and clear it). Persisting a high-water mark and
        # resuming well above it (see _SEQ_PERSIST_MARGIN) fixes both.
        self._seq_persist_path = seq_persist_path
        self._seq_calls_since_persist = 0
        self._seq = self._load_seq_start(seq_persist_path)
        self.listen_port: int | None = None
        self._own_hosts: set[str] = set()
        self._own_hosts_checked_at = 0.0
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
        # Keys that have gone through a successful confirm_peer() at least
        # once - lets confirm_peer tell a cryptographically-confirmed
        # identity's address apart from a raw, not-yet-confirmed candidate
        # (static bootstrap/gossip/mDNS), see confirm_peer's address-conflict
        # guard below.
        self._confirmed_keys: set[str] = set()

    def _load_seq_start(self, seq_persist_path: str | None) -> int:
        if seq_persist_path is None:
            return 0  # no durable identity (e.g. tests/embedding) - best effort only
        try:
            with open(seq_persist_path, "r") as f:
                return int(f.read().strip()) + _SEQ_PERSIST_MARGIN
        except (OSError, ValueError):
            return 0  # first run, or no prior persisted value yet

    def _persist_seq(self) -> None:
        if self._seq_persist_path is None:
            return
        tmp_path = self._seq_persist_path + ".tmp"
        try:
            with open(tmp_path, "w") as f:
                f.write(str(self._seq))
            os.replace(tmp_path, self._seq_persist_path)
        except OSError as exc:
            log.warning("failed to persist seq high-water mark to %s: %s", self._seq_persist_path, exc)

    @property
    def peers(self) -> list[Peer]:
        """One entry per logical peer (its currently-active address) - a peer
        with several candidate addresses (see add_peer_group) still counts
        and fans out as a single peer, not one per address."""
        with self._lock:
            return list(self._peers.values())

    def peer_details(self) -> list[dict]:
        """One dict per logical peer - key, active address, how many
        candidate addresses it has, and seconds since last refreshed (for
        display, e.g. the web UI's peer table)."""
        now = time.monotonic()
        with self._lock:
            return [
                {
                    "key": key,
                    "host": peer.host,
                    "port": peer.port,
                    "candidate_count": len(self._candidates.get(key, [peer])),
                    "age_s": round(now - self._last_seen.get(key, now), 1),
                    "static": key.startswith("static-"),
                }
                for key, peer in self._peers.items()
            ]

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
        if self._is_own_address(primary.host, primary.port):
            log.debug("ignoring peer %s -> %s:%d: that's our own listening address", key, primary.host, primary.port)
            return
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

    def confirm_peer(self, sender_id: int, peer: Peer) -> bool:
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
        of its addresses is cryptographically confirmed.

        Refuses to confirm our own listening address under someone else's
        sender_id - see _is_own_address for why (a relayed loop-back packet
        would otherwise be trusted at face value and silently redirect that
        real peer's outbound traffic to ourselves).

        Also refuses to attribute this address to `sender_id` if it's
        already confirmed as a DIFFERENT sender_id's address - with relay
        mode on, a relay's own address is the immediate UDP source for
        every packet it forwards, so without this guard every sender_id
        relayed through it would get attributed the relay's address
        instead of its own (visible e.g. as two different channel members
        both showing the relay's IP). The first sender_id to claim an
        address keeps it; this can't distinguish the relay's own true
        identity from one of the senders passing through it without a
        protocol-level hop marker, but it does guarantee one address is
        never attributed to more than one identity at a time.

        If the donor was a static (config-file) entry, the new identity key
        is kept "static-" prefixed too, so confirming a static peer doesn't
        silently strip its exemption from prune_stale() - without this, the
        first authenticated packet from a static peer would demote it to a
        normal prunable entry, defeating the point of configuring it as a
        permanent rendezvous address.

        Returns whether `peer` was actually attributed to `sender_id` -
        False if it was rejected per either guard above."""
        if self._is_own_address(peer.host, peer.port):
            log.debug("ignoring confirm_peer for sender %d at %s:%d: that's our own listening address",
                      sender_id, peer.host, peer.port)
            return False
        peer_key = f"peer-{sender_id}"
        static_key = f"static-{sender_id}"
        with self._lock:
            # Check which key (if either) this sender_id is already settled
            # under *before* scanning for a donor, so a repeat confirmation
            # (this runs on every authenticated packet, e.g. every audio
            # frame) correctly recognizes and skips its own existing entry
            # instead of re-merging it as its own "donor" every single time.
            key = static_key if static_key in self._peers else peer_key
            conflicting_key = next(
                (k for k in self._confirmed_keys
                 if k != key and k in self._peers
                 and self._peers[k].host == peer.host and self._peers[k].port == peer.port),
                None,
            )
            if conflicting_key is not None:
                log.debug("ignoring confirm_peer for sender %d at %s:%d: already confirmed as %s (likely relayed through that address, not %d's own)",
                          sender_id, peer.host, peer.port, conflicting_key, sender_id)
                return False
            donor_key = None
            candidates = [peer]
            for k, group in self._candidates.items():
                if k == key:
                    continue
                if any(c.host == peer.host and c.port == peer.port for c in group):
                    donor_key = k
                    candidates = [peer] + [c for c in group if (c.host, c.port) != (peer.host, peer.port)]
                    break
            if donor_key is not None and donor_key.startswith("static-"):
                key = static_key
            if donor_key is not None:
                self._peers.pop(donor_key, None)
                self._last_seen.pop(donor_key, None)
                self._candidates.pop(donor_key, None)
                self._confirmed_keys.discard(donor_key)
            is_new = key not in self._peers
            if is_new and len(self._peers) >= self.max_peers:
                log.warning("peer table full (%d), ignoring confirmed peer %s", self.max_peers, key)
                return False
            self._peers[key] = peer
            self._candidates[key] = candidates
            self._last_seen[key] = time.monotonic()
            self._confirmed_keys.add(key)
        if donor_key is not None:
            log.info("peer %s: tied to confirmed sender_id (merged %d address(es) from %s)", key, len(candidates), donor_key)
        elif is_new:
            log.info("peer confirmed: %s -> %s:%d", key, peer.host, peer.port)
        return True

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
                self._confirmed_keys.discard(key)
        for key in stale:
            log.info("peer expired (stale): %s", key)

    def remove_peer(self, key: str) -> None:
        with self._lock:
            removed = self._peers.pop(key, None)
            self._last_seen.pop(key, None)
            self._candidates.pop(key, None)
            self._confirmed_keys.discard(key)
        if removed:
            log.info("peer removed: %s", key)

    async def start(self, listen_port: int) -> None:
        self.listen_port = listen_port
        loop = asyncio.get_running_loop()
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: _MeshProtocol(self._on_packet),
            local_addr=("0.0.0.0", listen_port),
        )

    def _is_own_address(self, host: str, port: int) -> bool:
        """True if host:port is this node's own listening endpoint.

        A peer-exchange gossip message naturally includes us (we're one of
        OUR peers' peers), and would otherwise get added as a regular peer.
        With relay mode on, that makes us bounce our own relayed packets
        back to ourselves - mostly harmless for AUDIO (deduped by seq), but
        TYPE_STOP has no such dedup, so a looped-back STOP would blindly
        confirm_peer() a real peer's address as "ourselves", permanently
        redirecting all further outbound traffic to that peer into a black
        hole. Called on every authenticated packet (confirm_peer), so the
        local address set is cached rather than re-enumerated each time."""
        if self.listen_port is None or port != self.listen_port:
            return False
        if time.monotonic() - self._own_hosts_checked_at > _OWN_ADDRESS_CACHE_TTL_S:
            self._own_hosts = _local_addresses()
            self._own_hosts_checked_at = time.monotonic()
        return host in self._own_hosts

    def stop(self) -> None:
        if self._transport:
            self._transport.close()
            self._transport = None
        # Flush on a clean shutdown (including the web UI's restart-now
        # path, which awaits stop() before re-exec'ing) so the next process
        # resumes from a tight high-water mark instead of the full
        # _SEQ_PERSIST_MARGIN gap - see _load_seq_start.
        self._persist_seq()

    def next_seq(self) -> int:
        seq = self._seq
        self._seq += 1
        self._seq_calls_since_persist += 1
        if self._seq_calls_since_persist >= _SEQ_PERSIST_EVERY:
            self._seq_calls_since_persist = 0
            self._persist_seq()
        return seq

    def send_encrypted(self, cipher, channel_id: int, ptype: int, plaintext: bytes,
                       targets: list[Peer] | None = None) -> None:
        """Encrypts `plaintext` with `cipher` bound to this packet's exact
        header (so the header can't be altered in transit - see crypto.py)
        and sends it. Use this rather than encrypting and calling send()
        separately, so the header that's authenticated is always the one
        that goes on the wire."""
        seq = self.next_seq()
        header = Header(VERSION, ptype, channel_id, self.sender_id, seq)
        self.send(channel_id, seq, cipher.encrypt(header, plaintext), ptype=ptype, targets=targets)

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

