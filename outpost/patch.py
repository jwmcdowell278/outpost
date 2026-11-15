"""Channel patching: a node holding two channels' keys re-sends what it
hears on one onto the other, like a cross-patch on a radio console.

Each talker heard on a patched channel is re-sent under a stand-in sender
id ("virtual sender") that belongs to this node, one per talker, so
receivers keep talkers apart (two people talking at once don't garble each
other) and see a normal stream. The original sequence numbers are kept:
receivers' jitter buffers rely on them being contiguous, and (virtual id,
seq) stays unique per key - virtual ids are random per run, and the
original sender never reuses a seq. The Opus audio isn't re-encoded, only
re-encrypted, so both channels' nodes need the same frame_ms.

Loops (patches forming a ring, possibly across several nodes and meshes)
are stopped without marking packets: every patched frame's (seq, content
hash) is remembered for a few seconds, and a frame arriving again - under
whatever sender id - isn't patched again. A talker's STOP is only passed
on if their audio was, so STOPs don't circle a ring either.

Several nodes may run the same patch for redundancy. They say which
patches they run in a PATCH_HELLO (mesh key), and for each patch only the
highest-ranked node heard recently runs it; the rest stand by.
"""
from __future__ import annotations
import hashlib
import logging
import os
import time
from typing import TYPE_CHECKING

from .config import Patch
from .protocol import Header, VERSION, TYPE_AUDIO, TYPE_START, TYPE_STOP, encode_packet

if TYPE_CHECKING:
    from .client import OutpostClient

log = logging.getLogger(__name__)

TYPE_PATCH_HELLO = 8
PATCH_HELLO_INTERVAL_S = 5.0
_LOOP_MEMORY_S = 5.0        # how long a patched frame is remembered
_STREAM_IDLE_S = 3.0        # a talker silent this long has stopped (lost STOP)
_VIRTUAL_IDLE_S = 3600.0    # forget a talker's virtual id after this
_MAX_TRACKED = 4096         # bound on per-talker tables (sender_id is unauthenticated)
_MAX_ALIAS_BYTES = 32


class PatchEngine:
    def __init__(self, client: OutpostClient, patches: list[Patch], priority: int):
        self.client = client
        self.priority = priority
        # (sender_id of the original talker) -> (virtual sender id, last used)
        self._virtual: dict[int, tuple[int, float]] = {}
        self._own_virtual: set[int] = set()
        # (seq, content hash) -> when it was patched - the loop guard.
        self._recent: dict[tuple[int, bytes], float] = {}
        self._recent_pruned = 0.0
        # (original sender, destination channel) -> (last time, last seq sent)
        # for talkers whose audio is being patched right now.
        self._streams: dict[tuple[int, int], tuple[float, int]] = {}
        self._aliases: dict[int, str] = {}
        # sender_id -> (rank, edges it runs, last heard) from PATCH_HELLOs.
        self._hellos: dict[int, tuple[tuple[int, int], frozenset, float]] = {}
        self.counts = {"patched": 0, "looped": 0}
        self.set_patches(patches)

    # --- configuration ----------------------------------------------------

    def set_patches(self, patches: list[Patch]) -> None:
        self.patches = list(patches)
        self.edges: set[tuple[int, int]] = {e for p in self.patches for e in p.edges()}
        self._targets: dict[int, list[int]] = {}
        for src, dst in sorted(self.edges):
            self._targets.setdefault(src, []).append(dst)
        self._streams = {k: v for k, v in self._streams.items() if any(k[1] == d for _, d in self.edges)}

    def is_source(self, channel_id: int) -> bool:
        return channel_id in self._targets

    def is_virtual(self, sender_id: int) -> bool:
        return sender_id in self._own_virtual

    @property
    def channels(self) -> set[int]:
        return {c for e in self.edges for c in e}

    # --- election ---------------------------------------------------------

    def _rank(self) -> tuple[int, int]:
        return (self.priority, -self.client.config.sender_id)

    def is_active(self, edge: tuple[int, int], now: float | None = None) -> bool:
        """Whether this node runs `edge` - no higher-ranked node that also
        runs it has been heard from in the last 3 hello intervals."""
        now = time.monotonic() if now is None else now
        cutoff = now - 3 * PATCH_HELLO_INTERVAL_S
        mine = self._rank()
        return not any(rank > mine and edge in edges and seen >= cutoff
                       for rank, edges, seen in self._hellos.values())

    def hello_payload(self) -> bytes:
        edges = sorted(self.edges)[:255]
        return bytes([self.priority, len(edges)]) + b"".join(bytes(e) for e in edges)

    def on_hello(self, sender_id: int, payload: bytes) -> None:
        if len(payload) < 2 or len(payload) < 2 + 2 * payload[1]:
            return
        edges = frozenset((payload[2 + 2 * i], payload[3 + 2 * i]) for i in range(payload[1]))
        if len(self._hellos) >= _MAX_TRACKED:
            self._hellos.clear()
        self._hellos[sender_id] = ((payload[0], -sender_id), edges, time.monotonic())

    # --- patching ---------------------------------------------------------

    def on_input(self, header, plaintext: bytes) -> None:
        """An authenticated, fresh START/STOP/AUDIO on a patched channel."""
        now = time.monotonic()
        src = header.channel_id
        sender = header.sender_id
        if header.ptype == TYPE_START:
            # Not passed on as is - one is sent ahead of the first patched
            # audio instead, so STARTs can't circle a ring of patches.
            if len(self._aliases) >= _MAX_TRACKED:
                self._aliases.clear()
            self._aliases[sender] = plaintext.decode("utf-8", errors="replace")
            return
        if header.ptype == TYPE_STOP:
            for dst in self._targets.get(src, []):
                if self._streams.pop((sender, dst), None) is not None:
                    self._send(dst, sender, TYPE_STOP, header.seq, plaintext)
            return
        if header.ptype != TYPE_AUDIO:
            return
        key = (header.seq, hashlib.blake2b(plaintext, digest_size=8).digest())
        self._prune_recent(now)
        if key in self._recent:
            self.counts["looped"] += 1
            return  # this frame has been patched already - a loop
        self._recent[key] = now
        for dst in self._targets.get(src, []):
            if not self.is_active((src, dst), now):
                continue
            stream = self._streams.get((sender, dst))
            if stream is None or now - stream[0] > _STREAM_IDLE_S:
                last = stream[1] if stream else -1
                if header.seq - 1 > last:
                    self._send(dst, sender, TYPE_START, header.seq - 1, self._alias(sender, src))
            if len(self._streams) >= _MAX_TRACKED:
                self._streams.clear()
            self._streams[(sender, dst)] = (now, header.seq)
            self._send(dst, sender, TYPE_AUDIO, header.seq, plaintext)
            self.counts["patched"] += 1

    def _alias(self, sender: int, src: int) -> bytes:
        """Shown to listeners as who's talking: "Alice via ops"."""
        channel = self.client.config.channels.get(src)
        via = f" via {channel.name}".encode("utf-8") if channel else b""
        name = (self._aliases.get(sender) or f"#{sender}").encode("utf-8")
        name = name[:max(0, _MAX_ALIAS_BYTES - len(via))]
        return (name.decode("utf-8", errors="ignore").encode("utf-8") + via)[:_MAX_ALIAS_BYTES]

    def _virtual_id(self, sender: int, now: float) -> int:
        entry = self._virtual.get(sender)
        if entry is None:
            if len(self._virtual) >= _MAX_TRACKED:
                self._expire_virtual(now)
            vid = int.from_bytes(os.urandom(4), "big")
            while vid in self._own_virtual or vid == self.client.config.sender_id:
                vid = int.from_bytes(os.urandom(4), "big")
            self._own_virtual.add(vid)
        else:
            vid = entry[0]
        self._virtual[sender] = (vid, now)
        return vid

    def _expire_virtual(self, now: float) -> None:
        for sender, (vid, seen) in list(self._virtual.items()):
            if now - seen > _VIRTUAL_IDLE_S or len(self._virtual) >= _MAX_TRACKED:
                del self._virtual[sender]
                self._own_virtual.discard(vid)

    def _prune_recent(self, now: float) -> None:
        if now - self._recent_pruned < 1.0 and len(self._recent) < _MAX_TRACKED * 4:
            return
        self._recent_pruned = now
        cutoff = now - _LOOP_MEMORY_S
        self._recent = {k: t for k, t in self._recent.items() if t >= cutoff}

    def _send(self, dst: int, sender: int, ptype: int, seq: int, plaintext: bytes) -> None:
        c = self.client
        cipher = c.ciphers.get(dst)
        if cipher is None:
            return
        vid = self._virtual_id(sender, time.monotonic())
        header = Header(VERSION, ptype, dst, vid, seq)
        packet = encode_packet(header, cipher.encrypt(header, plaintext))
        targets, group = c._channel_delivery(dst)
        if group is not None:
            c.transport.send_multicast(packet, group)
        if targets:
            # Not back to the talker itself, if it listens here too (only
            # when it's reached at its own address, not through a relay
            # others share).
            own = c.transport.sender_address(sender)
            c.transport.forward(packet, [p for p in targets if (p.host, p.port) != own])
        if c.config.gateways:
            c._forward_to_gateways(header, packet, exclude_links=set())

    # --- status -----------------------------------------------------------

    def status(self) -> list[dict]:
        names = {cid: ch.name for cid, ch in self.client.config.channels.items()}
        now = time.monotonic()
        result = []
        for p in self.patches:
            edges = p.edges()
            result.append({
                "a": p.a, "b": p.b, "two_way": p.two_way,
                "a_name": names.get(p.a), "b_name": names.get(p.b),
                "active": any(self.is_active(e, now) for e in edges),
                "talking": sorted({sender for (sender, dst), (seen, _) in self._streams.items()
                                   if now - seen <= _STREAM_IDLE_S and any(dst == e[1] for e in edges)}),
            })
        return result
