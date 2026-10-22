"""Receive-side playout: a jitter buffer per sender, mixed into one output.

Each (channel, sender) currently being heard gets its own JitterStream with
its own Opus decoder (decoders are stateful - sharing one between senders
corrupts both). The speaker's audio callback pulls one mixed frame per
frame period from Mixer.next_frame(), so playout is clocked by the sound
card itself rather than by packet arrival: frames that arrive in bursts are
smoothed out, a late or lost frame is concealed instead of shifting
everything after it later, and two people talking at once are summed
rather than interleaved.

Lost frames are recovered with Opus in-band FEC when the following frame
has already arrived, otherwise concealed (PLC). This relies on a sender's
AUDIO sequence numbers being contiguous within a transmission - the
sending side holds back its control traffic while PTT is held to keep them
so (see MeshTransport.hold_control). A sender that doesn't (an older
version) still plays fine; an occasional control packet just costs one
concealed frame, and any delay that adds is trimmed back automatically.
"""
from __future__ import annotations
import math
import threading
import time
from typing import Callable

import numpy as np

# Playout delay bounds, in frames (20ms at default settings), chosen per
# talk spurt from measured network jitter.
MIN_DELAY_FRAMES = 2
MAX_DELAY_FRAMES = 8
# Buffered frames beyond the target delay before skipping ahead to cut
# latency (e.g. after a network stall delivers a burst at once).
SKIP_SLACK_FRAMES = 4
# Consecutive frames with nothing to play (and no STOP seen) before a
# stream is treated as ended - covers a lost STOP packet.
MAX_CONCEALED_FRAMES = 10
# A packet this far ahead of the playout point starts a fresh talk spurt.
RESYNC_FRAMES = 50
# Idle streams are discarded (with their decoder) after this long.
IDLE_DISCARD_S = 60.0
# How much non-active channels are attenuated while the active channel has
# audio (duck_monitored_channels).
DUCK_GAIN = 0.35


class JitterStream:
    """Buffer and playout state for one sender on one channel."""

    def __init__(self, decoder, frame_s: float):
        self.decoder = decoder
        self.frame_s = frame_s
        self.buf: dict[int, bytes] = {}
        self.next_seq: int | None = None
        self.state = "idle"  # idle -> buffering -> playing -> idle
        self.started_at = 0.0
        self.stop_seen = False
        self.delay = MIN_DELAY_FRAMES
        # Delay the measured jitter calls for; playout stretches toward it
        # mid-transmission when frames start arriving late (see pull).
        self.target_delay = MIN_DELAY_FRAMES
        self.jitter_s = 0.0
        self._last_transit: float | None = None
        self.misses = 0
        self.last_activity = 0.0
        self.stats = {"late": 0, "concealed": 0, "fec": 0, "skipped": 0, "stretched": 0}

    def _start_spurt(self, seq: int, now: float) -> None:
        self.buf.clear()
        self.next_seq = seq
        self.state = "buffering"
        self.started_at = now
        self.stop_seen = False
        self.misses = 0
        self._last_transit = None
        self.delay = self.target_delay = self._wanted_delay()

    def _wanted_delay(self) -> int:
        # 4x the smoothed jitter covers nearly all arrivals; +1 for the
        # frame currently being received.
        wanted = math.ceil(4 * self.jitter_s / self.frame_s) + 1
        return max(MIN_DELAY_FRAMES, min(MAX_DELAY_FRAMES, wanted))

    def push(self, seq: int, opus: bytes, now: float) -> None:
        self.last_activity = now
        if self.state == "idle" or self.next_seq is None or seq - self.next_seq > RESYNC_FRAMES:
            self._start_spurt(seq, now)
        elif seq < self.next_seq:
            # Its slot was already played/concealed - buffer more from now on.
            self.stats["late"] += 1
            self.target_delay = min(MAX_DELAY_FRAMES, max(self.target_delay, self.delay + 1))
            return
        if seq in self.buf:
            return
        self.buf[seq] = opus
        # RFC 3550-style interarrival jitter estimate.
        transit = now - seq * self.frame_s
        if self._last_transit is not None:
            self.jitter_s += (abs(transit - self._last_transit) - self.jitter_s) / 16
            self.target_delay = max(self.target_delay, self._wanted_delay())
        self._last_transit = transit

    def stop(self) -> None:
        self.stop_seen = True

    def pull(self, now: float) -> bytes | None:
        """PCM for this frame period, or None if the stream has nothing to
        play (idle, still buffering, or just finished)."""
        if self.state == "idle":
            return None
        if self.state == "buffering":
            ahead = (max(self.buf) - self.next_seq + 1) if self.buf else 0
            if ahead >= self.delay or now - self.started_at >= self.delay * self.frame_s or self.stop_seen:
                self.state = "playing"
            else:
                return None

        if self.buf:
            ahead = max(self.buf) - self.next_seq + 1
            if ahead > self.delay + SKIP_SLACK_FRAMES:
                new_next = max(self.buf) - self.delay + 1
                for s in range(self.next_seq, new_next):
                    self.buf.pop(s, None)
                    self.stats["skipped"] += 1
                self.next_seq = new_next

        seq = self.next_seq
        if seq not in self.buf and not self.stop_seen and self.delay < self.target_delay:
            # The next frame isn't here and we're buffering less than the
            # link needs: rather than concealing it and moving on (making it
            # late if it's merely delayed), wait one frame for it. Stretches
            # playback by one frame and raises the delay by one.
            self.delay += 1
            self.stats["stretched"] += 1
            self.last_activity = now
            return self.decoder.decode_loss()
        if seq in self.buf:
            pcm = self.decoder.decode(self.buf.pop(seq))
            self.misses = 0
        elif not self.buf and self.stop_seen:
            self.state = "idle"
            return None
        elif (seq + 1) in self.buf:
            # Next frame already here - recover this one from its FEC data.
            try:
                pcm = self.decoder.decode_fec(self.buf[seq + 1])
                self.stats["fec"] += 1
            except Exception:
                pcm = self.decoder.decode_loss()
                self.stats["concealed"] += 1
            self.misses = 0
        else:
            self.misses += 1
            if self.misses > MAX_CONCEALED_FRAMES and not self.buf:
                self.state = "idle"
                return None
            pcm = self.decoder.decode_loss()
            self.stats["concealed"] += 1
        self.next_seq += 1
        self.last_activity = now
        return pcm


class Mixer:
    """Mixes every active JitterStream (plus local sounds like the lockout
    tone) into one frame per call. Thread-safe: packets are pushed from the
    network thread while next_frame() runs on the audio callback thread."""

    def __init__(self, frame_size: int, frame_ms: int, decoder_factory: Callable[[], object]):
        self.frame_size = frame_size
        self.frame_s = frame_ms / 1000.0
        self._decoder_factory = decoder_factory
        self._streams: dict[tuple[int, int], JitterStream] = {}
        self._local: list[bytes] = []
        self._lock = threading.Lock()
        self._silence = np.zeros(frame_size, dtype=np.int16).tobytes()
        self._gain = 1.0  # limiter gain, recovers toward 1.0
        # channel_id -> 0.0..1.0; missing means 1.0.
        self.volumes: dict[int, float] = {}
        self.duck_enabled = True
        self.active_channel_id: int | None = None
        # monotonic time this mixer last output anything audible - feeds the
        # client's echo guard.
        self.last_active = 0.0

    def push(self, channel_id: int, sender_id: int, seq: int, opus: bytes) -> None:
        key = (channel_id, sender_id)
        with self._lock:
            stream = self._streams.get(key)
            if stream is None:
                stream = self._streams[key] = JitterStream(self._decoder_factory(), self.frame_s)
            stream.push(seq, opus, time.monotonic())

    def stop(self, channel_id: int, sender_id: int) -> None:
        with self._lock:
            stream = self._streams.get((channel_id, sender_id))
            if stream is not None:
                stream.stop()

    def remove_channel(self, channel_id: int) -> None:
        """Stop playing a channel immediately (no longer listened to)."""
        with self._lock:
            for key in [k for k in self._streams if k[0] == channel_id]:
                del self._streams[key]

    def play_local(self, frames: list[bytes]) -> None:
        with self._lock:
            self._local.extend(frames)

    def stats(self) -> dict[tuple[int, int], dict]:
        with self._lock:
            return {k: dict(s.stats, delay_ms=round(s.delay * self.frame_s * 1000), state=s.state)
                    for k, s in self._streams.items()}

    def next_frame(self) -> bytes:
        now = time.monotonic()
        with self._lock:
            outputs = []
            for (channel_id, sender_id), stream in list(self._streams.items()):
                pcm = stream.pull(now)
                if pcm is not None:
                    outputs.append((channel_id, pcm))
                elif stream.state == "idle" and now - stream.last_activity > IDLE_DISCARD_S:
                    del self._streams[(channel_id, sender_id)]
            local = self._local.pop(0) if self._local else None

        if not outputs and local is None:
            return self._silence

        active_busy = self.duck_enabled and any(ch == self.active_channel_id for ch, _ in outputs)
        acc = np.zeros(self.frame_size, dtype=np.float32)
        for channel_id, pcm in outputs:
            gain = self.volumes.get(channel_id, 1.0)
            if active_busy and channel_id != self.active_channel_id:
                gain *= DUCK_GAIN
            acc += np.frombuffer(pcm, dtype=np.int16)[:self.frame_size] * gain
        if local is not None:
            acc += np.frombuffer(local, dtype=np.int16)[:self.frame_size]

        # Simple limiter: when several loud talkers sum past full scale,
        # turn the whole frame down rather than clipping, then let the gain
        # recover over the next few frames.
        peak = float(np.max(np.abs(acc))) if acc.size else 0.0
        target = min(1.0, 32000.0 / peak) if peak > 0 else 1.0
        self._gain = target if target < self._gain else min(target, self._gain + 0.1)
        out = np.clip(acc * self._gain, -32768, 32767).astype(np.int16)
        self.last_active = now
        return out.tobytes()
