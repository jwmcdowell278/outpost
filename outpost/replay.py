"""Anti-replay protection for channel traffic (AUDIO/START/STOP/
CHANNEL_ANNOUNCE).

Encryption alone doesn't stop replays: a packet captured off the network
still decrypts fine if resent later, so without this anyone - no key needed
- could record a transmission and play it back to the whole channel.

Each sender numbers every packet it sends from one strictly increasing
counter (transport.next_seq, persisted across restarts), so a receiver
tracks, per sender, the highest seq it has accepted plus a bitmap of which
of the `size` seqs just below it were already seen - the same sliding
window IPsec and DTLS use. A packet is accepted only if its seq is new and
not older than the window. The window (rather than a plain "must be higher
than the last one" rule) tolerates the mild reordering of a multi-path
mesh, e.g. a START overtaken by the first AUDIO frame.

Unlike client.py's `_last_seq_seen` (playback ordering, which is reset on
every STOP), this state is never reset while the process runs.
"""
from __future__ import annotations

DEFAULT_WINDOW = 1024  # ~20s of audio at 20ms frames


class ReplayWindow:
    def __init__(self, size: int = DEFAULT_WINDOW):
        self.size = size
        self._mask_all = (1 << size) - 1
        self._highest: dict[int, int] = {}
        # Bit i set = seq (highest - i) already accepted.
        self._seen: dict[int, int] = {}

    def is_fresh(self, sender_id: int, seq: int) -> bool:
        """True if (sender_id, seq) hasn't been accepted before and isn't
        older than the window. Doesn't record anything - call mark() once
        the packet has authenticated, so forged packets (which fail to
        decrypt) can't push the window forward and lock out real traffic."""
        highest = self._highest.get(sender_id)
        if highest is None or seq > highest:
            return True
        offset = highest - seq
        if offset >= self.size:
            return False
        return not (self._seen[sender_id] >> offset) & 1

    def is_too_old(self, sender_id: int, seq: int) -> bool:
        """True if seq is below the window entirely (as opposed to a recent
        duplicate) - normal multi-path duplicates are recent, so this is
        what distinguishes a likely replay or a sender whose counter went
        backwards."""
        highest = self._highest.get(sender_id)
        return highest is not None and highest - seq >= self.size

    def mark(self, sender_id: int, seq: int) -> None:
        highest = self._highest.get(sender_id)
        if highest is None:
            self._highest[sender_id] = seq
            self._seen[sender_id] = 1
        elif seq > highest:
            shift = seq - highest
            mask = 1 if shift >= self.size else ((self._seen[sender_id] << shift) | 1) & self._mask_all
            self._highest[sender_id] = seq
            self._seen[sender_id] = mask
        else:
            self._seen[sender_id] |= 1 << (highest - seq)
