"""Outpost voice client: wires together audio I/O, PTT, codec, encryption and
the mesh transport for a configured set of channels."""
from __future__ import annotations
import asyncio
import logging
import math
import random
import time
from dataclasses import replace

import numpy as np

from .agc import AutomaticGainControl
from .audio import MicCapture, SpeakerPlayback
from .codec import OpusCodec
from .crypto import ChannelCipher
from .discovery import PeerDiscovery
from .dsp import HighPassFilter, Squelch
from .noise_suppression import NoiseSuppressor, PassthroughSuppressor
from .pex import PexGossip
from .protocol import TYPE_AUDIO, TYPE_STOP, TYPE_PEX, TYPE_CHANNEL_ANNOUNCE, encode_packet
from .transport import MeshTransport
from .config import OutpostConfig, Channel, Peer
from .webui import WebUI
from zeroconf._exceptions import EventLoopBlocked

log = logging.getLogger(__name__)

# Local-only "PTT blocked" busy tone (tx lockout) - short and quiet enough
# not to be mistaken for an incoming transmission.
_LOCKOUT_TONE_HZ = 480.0
_LOCKOUT_TONE_MS = 150.0
_LOCKOUT_TONE_AMPLITUDE = 8000


def _make_tone_frames(frame_size: int, sample_rate: int, freq_hz: float, duration_ms: float, amplitude: int) -> list[bytes]:
    """Synthesizes a mono int16 PCM sine tone (with a short fade in/out to
    avoid clicks), chunked into frame_size-sized frames matching what
    SpeakerPlayback.play_frame expects."""
    total_samples = int(sample_rate * duration_ms / 1000)
    t = np.arange(total_samples)
    tone = amplitude * np.sin(2 * math.pi * freq_hz * t / sample_rate)

    fade_samples = min(total_samples // 4, int(sample_rate * 0.01))
    if fade_samples > 0:
        fade = np.linspace(0.0, 1.0, fade_samples)
        tone[:fade_samples] *= fade
        tone[-fade_samples:] *= fade[::-1]

    pcm = tone.astype(np.int16)
    pad = (-len(pcm)) % frame_size
    if pad:
        pcm = np.concatenate([pcm, np.zeros(pad, dtype=np.int16)])

    return [pcm[i:i + frame_size].tobytes() for i in range(0, len(pcm), frame_size)]


class OutpostClient:
    def __init__(
        self,
        config: OutpostConfig,
        active_channel_name: str,
        noise_suppressor: NoiseSuppressor | None = None,
        agc: AutomaticGainControl | None = None,
        highpass: HighPassFilter | None = None,
        squelch: Squelch | None = None,
        config_path: str | None = None,
    ):
        self.config = config
        # Path to the YAML file `config` was loaded from, if any - lets the
        # web UI's settings editor persist changes back (see
        # config.save_config_fields). None in tests/embedding scenarios that
        # construct OutpostConfig directly rather than via load_config.
        self.config_path = config_path
        self.active_channel = config.channel_by_name(active_channel_name)
        self.noise_suppressor = noise_suppressor or PassthroughSuppressor()
        # Always construct the AGC object (cheap, pure DSP) and gate its use
        # with a separate mutable flag, rather than leaving self.agc None
        # when disabled - this lets the web UI toggle AGC on/off live
        # without restarting the client.
        self.agc = agc if agc is not None else AutomaticGainControl(target_rms=config.agc_target_rms)
        self.agc_enabled = config.enable_agc

        self.codec = OpusCodec(
            bitrate=config.bitrate,
            frame_ms=config.frame_ms,
            inband_fec=config.inband_fec,
        )

        # High-pass filter and squelch - same always-construct/separate-flag
        # pattern as AGC above, so the web UI can toggle/retune them live.
        self.highpass = highpass if highpass is not None else HighPassFilter(
            cutoff_hz=config.highpass_cutoff_hz, sample_rate=self.codec.sample_rate
        )
        self.highpass_enabled = config.enable_highpass
        self.squelch = squelch if squelch is not None else Squelch(
            threshold_rms=config.squelch_threshold_rms,
            hold_frames=max(1, round(config.squelch_hold_ms / config.frame_ms)),
        )
        self.squelch_enabled = config.enable_squelch

        self.ciphers = {cid: ChannelCipher(ch.key) for cid, ch in config.channels.items()}

        self.mic = MicCapture(self.codec.sample_rate, self.codec.frame_size)
        self.speaker = SpeakerPlayback(self.codec.sample_rate, self.codec.frame_size)
        self.transport = MeshTransport(
            config.sender_id, config.peers, self._on_packet,
            max_packet_bytes=config.max_packet_bytes, max_peers=config.max_peers,
        )

        # mDNS only helps on a plain multicast-reachable LAN; PEX gossip is
        # the discovery path that also works over VPN mesh and radio mesh.
        self.discovery = (
            PeerDiscovery(
                config.sender_id,
                config.listen_port,
                on_peer_added=self.transport.add_peer_group,
                on_peer_removed=self.transport.remove_peer,
            )
            if config.enable_mdns
            else None
        )
        self.pex = (
            PexGossip(config.sender_id, config.mesh_key, self.transport,
                      interval_s=config.pex_interval_s, fanout=config.pex_fanout)
            if config.enable_pex
            else None
        )
        self.web_ui = (
            WebUI(self, host=config.web_ui_host, port=config.web_ui_port, token=config.web_ui_token)
            if config.enable_web_ui
            else None
        )

        self._ptt_active = False
        self._tx_task: asyncio.Task | None = None
        self._channel_announce_task: asyncio.Task | None = None
        self._last_seq_seen: dict[int, int] = {}

        # Set by the web UI's Settings page "Restart" button - scripts/
        # run_client.py's main loop waits on this alongside SIGINT/SIGTERM
        # and, if it fires, cleanly stops and re-execs itself (same argv) so
        # restart-required config changes take effect without the user
        # needing shell access.
        self.restart_requested = asyncio.Event()

        # channel_id -> {sender_id: (Peer, last_seen_monotonic)} - built from
        # authenticated channel-announce beacons, so AUDIO/STOP sends can
        # target only known members instead of every peer in the mesh.
        self._channel_members: dict[int, dict[int, tuple[Peer, float]]] = {}
        self._membership_ttl_s = max(config.channel_announce_interval_s * 2.5, 60.0)
        self._peer_ttl_s = max(config.pex_interval_s * 2.5, 90.0)

        # (sender_id, ptype) -> highest seq already relayed - loop/duplicate
        # guard for relay mode (see `_relay_packet`). Bounded below since
        # sender_id here is an unauthenticated cleartext field an attacker
        # could vary freely to grow this table.
        self._relay_seen: dict[tuple[int, int], int] = {}

        # Mutable so the web UI can toggle relay mode live - see __init__'s
        # agc_enabled for the same pattern/rationale.
        self.enable_relay = config.enable_relay

        # Soft half-duplex lockout (see OutpostConfig.enable_tx_lockout) -
        # same always-construct/mutable-flag pattern as AGC/relay above.
        self.enable_tx_lockout = config.enable_tx_lockout
        self.tx_lockout_timeout_s = config.tx_lockout_timeout_ms / 1000.0
        # channel_id -> monotonic time of the last AUDIO frame received from
        # another peer on that channel - drives channel_busy() below.
        self._channel_last_audio: dict[int, float] = {}
        # Precomputed once - sample rate/frame size are fixed for the life
        # of the client.
        self._lockout_tone_frames = _make_tone_frames(
            self.codec.frame_size, self.codec.sample_rate,
            _LOCKOUT_TONE_HZ, _LOCKOUT_TONE_MS, _LOCKOUT_TONE_AMPLITUDE,
        )

    # --- PTT control --------------------------------------------------

    def channel_busy(self, channel_id: int | None = None) -> bool:
        """True if another peer's audio on `channel_id` (default: active
        channel) was received recently enough to still count as an ongoing
        transmission. Best-effort only - see enable_tx_lockout."""
        if channel_id is None:
            channel_id = self.active_channel.id
        last_audio = self._channel_last_audio.get(channel_id)
        if last_audio is None:
            return False
        return (time.monotonic() - last_audio) < self.tx_lockout_timeout_s

    def _play_lockout_tone(self) -> None:
        """Local-only audible feedback that a PTT press was blocked - never
        transmitted. Shares the speaker's single output queue with any audio
        currently playing, so it may briefly interrupt/overlay whoever is
        already talking rather than play on a separate line."""
        for frame in self._lockout_tone_frames:
            self.speaker.play_frame(frame)

    def set_ptt(self, pressed: bool) -> None:
        """Call from a PTTSource callback (may run on any thread)."""
        if pressed and self.enable_tx_lockout and self.channel_busy(self.active_channel.id):
            log.debug("PTT press ignored - channel busy (tx lockout)")
            self._play_lockout_tone()
            return
        self._ptt_active = pressed
        if not pressed:
            cipher = self.ciphers[self.active_channel.id]
            seq = self.transport.next_seq()
            ciphertext = cipher.encrypt(self.config.sender_id, seq, b"")
            targets = self._channel_target_peers(self.active_channel.id)
            self.transport.send(self.active_channel.id, seq, ciphertext, ptype=TYPE_STOP, targets=targets)

    def set_active_channel(self, name: str) -> None:
        self.active_channel = self.config.channel_by_name(name)

    def request_restart(self) -> None:
        """Signals scripts/run_client.py's main loop to cleanly stop and
        re-exec the process - only the entry point has the original argv
        needed to relaunch, so this just flips the flag it waits on."""
        self.restart_requested.set()

    def add_channel(self, channel_id: int, name: str, key: bytes) -> Channel:
        """Wires a newly-created channel into the running process (web UI's
        Channels page) - no restart needed, since `_channel_announce_loop`
        iterates `self.ciphers` fresh every cycle and `_on_packet`/
        `set_active_channel` look channels up via `self.config.channels`."""
        channel = Channel(id=channel_id, name=name, key=key)
        self.config.channels[channel_id] = channel
        self.ciphers[channel_id] = ChannelCipher(key)
        return channel

    def rename_channel(self, channel_id: int, new_name: str) -> None:
        renamed = replace(self.config.channels[channel_id], name=new_name)
        self.config.channels[channel_id] = renamed
        if self.active_channel.id == channel_id:
            self.active_channel = renamed

    def rotate_channel_key(self, channel_id: int, key: bytes) -> None:
        """Replaces a channel's key immediately - every other peer on that
        channel must be given the same new key before they can decrypt this
        node's traffic again (or decrypt each other's, once they rotate too)."""
        self.config.channels[channel_id] = replace(self.config.channels[channel_id], key=key)
        self.ciphers[channel_id] = ChannelCipher(key)

    def channel_member_details(self, channel_id: int | None = None) -> list[dict]:
        """Confirmed members (sender_id + address + seconds since last
        announce) of a channel - defaults to the active channel. For
        display only (e.g. the web UI's status detail)."""
        if channel_id is None:
            channel_id = self.active_channel.id
        now = time.monotonic()
        return [
            {"sender_id": sender_id, "host": peer.host, "port": peer.port, "age_s": round(now - last_seen, 1)}
            for sender_id, (peer, last_seen) in self._channel_members.get(channel_id, {}).items()
        ]

    # --- lifecycle ------------------------------------------------------

    async def start(self) -> None:
        await self.transport.start(self.config.listen_port)
        if self.discovery:
            try:
                # PeerDiscovery.start() blocks on zeroconf's own internal
                # event loop via run_coroutine_threadsafe().result(timeout) -
                # running it directly on this coroutine's thread would
                # deadlock against our own (still-running) loop, so it must
                # go through an executor thread instead.
                loop = asyncio.get_running_loop()
                await loop.run_in_executor(None, self.discovery.start)
            except (OSError, EventLoopBlocked) as exc:
                # Multicast is commonly unavailable on VPN/mesh-radio links -
                # PEX gossip and/or static peers still work without it.
                log.warning("mDNS discovery unavailable (%s); relying on PEX/static peers", exc)
        if self.pex:
            self.pex.start()
        self.mic.start()
        self.speaker.start()
        self._tx_task = asyncio.create_task(self._tx_loop())
        self._channel_announce_task = asyncio.create_task(self._channel_announce_loop())
        if self.web_ui:
            await self.web_ui.start()

    async def stop(self) -> None:
        if self._tx_task:
            self._tx_task.cancel()
            self._tx_task = None
        if self._channel_announce_task:
            self._channel_announce_task.cancel()
            self._channel_announce_task = None
        if self.web_ui:
            await self.web_ui.stop()
        if self.pex:
            self.pex.stop()
        if self.discovery:
            self.discovery.stop()
        self.mic.stop()
        self.speaker.stop()
        self.transport.stop()

    # --- transmit path ----------------------------------------------------

    async def _tx_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            frame = await loop.run_in_executor(None, self.mic.read_frame)
            if not self._ptt_active:
                continue
            if self.highpass_enabled:
                frame = self.highpass.process(frame)
            if self.squelch_enabled:
                frame = self.squelch.process(frame)
            if self.agc_enabled:
                frame = self.agc.process(frame)
            frame = self.noise_suppressor.process(frame)
            opus_bytes = self.codec.encode(frame)
            cipher = self.ciphers[self.active_channel.id]
            seq = self.transport.next_seq()
            ciphertext = cipher.encrypt(self.config.sender_id, seq, opus_bytes)
            targets = self._channel_target_peers(self.active_channel.id)
            self.transport.send(self.active_channel.id, seq, ciphertext, ptype=TYPE_AUDIO, targets=targets)

    async def _channel_announce_loop(self) -> None:
        """Periodically floods a tiny empty-payload beacon per held channel so
        peers can authenticate our membership and start targeting us
        directly, instead of every AUDIO/STOP packet having to flood the
        whole mesh forever."""
        await asyncio.sleep(random.uniform(0, min(2.0, self.config.channel_announce_interval_s)))
        while True:
            self.transport.prune_stale(self._peer_ttl_s)
            for channel_id, cipher in self.ciphers.items():
                seq = self.transport.next_seq()
                ciphertext = cipher.encrypt(self.config.sender_id, seq, b"")
                self.transport.send(channel_id, seq, ciphertext, ptype=TYPE_CHANNEL_ANNOUNCE)
            await asyncio.sleep(self.config.channel_announce_interval_s * random.uniform(0.8, 1.2))

    def _channel_target_peers(self, channel_id: int) -> list[Peer] | None:
        """Known, not-yet-stale members of a channel, or None to fall back to
        flooding every known peer (bootstrap case - before any announce has
        propagated, or if a channel currently has no confirmed members)."""
        members = self._channel_members.get(channel_id)
        if not members:
            return None
        cutoff = time.monotonic() - self._membership_ttl_s
        fresh = [peer for peer, last_seen in members.values() if last_seen >= cutoff]
        return fresh or None

    # --- receive path -----------------------------------------------------

    _RELAY_SEEN_MAX = 4096

    def _relay_packet(self, header, ciphertext: bytes, addr) -> None:
        """Blind flood-forward for AUDIO/STOP/CHANNEL_ANNOUNCE packets to
        every other known peer, keyed only on the cleartext header - no
        channel key needed, so a relay node can bridge two otherwise-
        unreachable peer groups (e.g. a local batman-adv mesh segment and a
        WireGuard-connected HQ/uplink segment) without ever being able to
        decrypt the traffic it forwards. Deliberately does NOT forward PEX:
        gossiped peer addresses from one side are typically unroutable from
        the other, so each side should discover its own peers independently
        and only use this relay node itself as a bootstrap `peers:` entry.
        """
        dedup_key = (header.sender_id, header.ptype)
        last_fwd = self._relay_seen.get(dedup_key)
        if last_fwd is not None and header.seq <= last_fwd:
            return  # already forwarded this or an older/duplicate packet
        if len(self._relay_seen) > self._RELAY_SEEN_MAX:
            self._relay_seen.clear()  # safety valve - sender_id is spoofable, don't grow unbounded
        self._relay_seen[dedup_key] = header.seq

        targets = [p for p in self.transport.peers if (p.host, p.port) != (addr[0], addr[1])]
        if targets:
            self.transport.forward(encode_packet(header, ciphertext), targets)

    def _on_packet(self, header, ciphertext: bytes, addr) -> None:
        if header.sender_id == self.config.sender_id:
            # Our own traffic looped back to us - e.g. a multi-hop relay mesh
            # can bounce a packet through a downstream relay and back to its
            # original sender if that relay also knows us as a peer. Without
            # this guard we'd decrypt and play our own voice back, which
            # sounds like a (delayed) audio echo whenever relay mode is on.
            return

        if self.enable_relay and header.ptype in (TYPE_AUDIO, TYPE_STOP, TYPE_CHANNEL_ANNOUNCE):
            self._relay_packet(header, ciphertext, addr)

        if header.ptype == TYPE_PEX:
            if self.pex:
                self.pex.handle_incoming(header, ciphertext, addr)
            return

        if header.ptype == TYPE_CHANNEL_ANNOUNCE:
            cipher = self.ciphers.get(header.channel_id)
            if cipher is None:
                return  # not a channel we hold the key for
            try:
                cipher.decrypt(header.sender_id, header.seq, ciphertext)
            except ValueError:
                log.debug("dropping unauthenticated channel announce from %s", addr)
                return
            peer = Peer(host=addr[0], port=addr[1])
            self.transport.confirm_peer(header.sender_id, peer)
            self._channel_members.setdefault(header.channel_id, {})[header.sender_id] = (peer, time.monotonic())
            return

        if header.channel_id != self.active_channel.id:
            return  # only play audio for the currently selected channel

        cipher = self.ciphers.get(header.channel_id)
        if cipher is None:
            return  # we don't hold this channel's key - can't be a member

        if header.ptype == TYPE_STOP:
            try:
                cipher.decrypt(header.sender_id, header.seq, ciphertext)
            except ValueError:
                log.debug("dropping unauthenticated stop from %s", addr)
                return
            self._last_seq_seen.pop(header.sender_id, None)
            self.transport.confirm_peer(header.sender_id, Peer(host=addr[0], port=addr[1]))
            self._channel_last_audio.pop(header.channel_id, None)
            return
        if header.ptype != TYPE_AUDIO:
            return

        last_seq = self._last_seq_seen.get(header.sender_id)
        if last_seq is not None and header.seq <= last_seq:
            return  # stale/duplicate/out-of-order - drop rather than play audio backwards

        try:
            opus_bytes = cipher.decrypt(header.sender_id, header.seq, ciphertext)
        except ValueError:
            log.debug("dropping packet with bad auth tag from %s", addr)
            return

        # Only an authenticated sender teaches us its address - raw UDP
        # source addresses are trivially spoofable and must not be trusted.
        self.transport.confirm_peer(header.sender_id, Peer(host=addr[0], port=addr[1]))
        self._channel_last_audio[header.channel_id] = time.monotonic()

        if last_seq is not None and header.seq == last_seq + 2:
            # Exactly one frame missing - try Opus in-band FEC to recover it
            # from the redundancy carried in this packet before falling back
            # to plain concealment.
            try:
                recovered = self.codec.decode_fec(opus_bytes)
                self.speaker.play_frame(recovered)
            except Exception:
                self.speaker.play_frame(self.codec.decode_loss())
        elif last_seq is not None and header.seq > last_seq + 1:
            for _ in range(header.seq - last_seq - 1):
                self.speaker.play_frame(self.codec.decode_loss())

        self._last_seq_seen[header.sender_id] = header.seq
        pcm = self.codec.decode(opus_bytes)
        self.speaker.play_frame(pcm)
