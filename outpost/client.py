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
from .announce import ChannelAnnouncer
from .audio import MicCapture, SpeakerPlayback, device_available, list_devices_fresh, restart_streams
from .codec import OpusCodec
from .crypto import ChannelCipher
from .discovery import PeerDiscovery
from .dsp import HighPassFilter, Squelch
from .noise_suppression import NoiseSuppressor, PassthroughSuppressor
from .pex import PexGossip
from .protocol import TYPE_AUDIO, TYPE_STOP, TYPE_START, TYPE_PEX, TYPE_CHANNEL_ANNOUNCE, encode_packet
from .subnet_scan import SubnetScanner
from .transport import MeshTransport
from .config import OutpostConfig, Channel, Peer
from .webui import WebUI
from zeroconf._exceptions import EventLoopBlocked

log = logging.getLogger(__name__)

# Audio watchdog (see OutpostClient._audio_watchdog_loop): how often to check
# that the mic/speaker are still delivering audio, and - while a configured
# device is missing and the system default is standing in - how often to
# look for it coming back.
_AUDIO_WATCHDOG_INTERVAL_S = 1.0
_AUDIO_DEVICE_RECHECK_S = 15.0

# Local-only "PTT blocked" busy tone (tx lockout) - short and quiet enough
# not to be mistaken for an incoming transmission.
_LOCKOUT_TONE_HZ = 480.0
_LOCKOUT_TONE_MS = 150.0
_LOCKOUT_TONE_AMPLITUDE = 8000

# How long a received sender stays listed as "speaking" (speaking_details)
# after its last AUDIO/START frame with no STOP seen - a fallback for a lost
# STOP packet, well above normal frame/announce cadence.
_SPEAKING_STALE_S = 2.0


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
        channel_announcer: ChannelAnnouncer | None = None,
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

        # Unlike agc/highpass/squelch above, this is an optional external
        # dependency (pyttsx3) and spawns a background thread, so - like
        # web_ui/subnet_scan below - it's only constructed when enabled,
        # not always-constructed-with-a-flag; toggling it live from the web
        # UI isn't supported, see EDITABLE_FIELDS.
        self.channel_announcer = (
            channel_announcer if channel_announcer is not None
            else (ChannelAnnouncer(voice=config.channel_announce_voice) if config.enable_channel_announce else None)
        )

        self.ciphers = {cid: ChannelCipher(ch.key) for cid, ch in config.channels.items()}

        self.mic = MicCapture(self.codec.sample_rate, self.codec.frame_size, device=config.input_device)
        self.speaker = SpeakerPlayback(self.codec.sample_rate, self.codec.frame_size, device=config.output_device)
        self.transport = MeshTransport(
            config.sender_id, config.peers, self._on_packet,
            max_packet_bytes=config.max_packet_bytes, max_peers=config.max_peers,
            # Persist the seq high-water mark next to the config file (same
            # convention as config._resolve_sender_id's ".identity" file) so
            # a restart never reuses a (sender_id, seq) nonce pair or makes
            # peers treat this node's fresh packets as stale duplicates of
            # its pre-restart sequence numbers - see transport.py for
            # details. None (no persistence) in tests/embedding scenarios
            # that construct OutpostConfig directly without config_path.
            seq_persist_path=(config_path + ".seq") if config_path else None,
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
        if config.enable_subnet_scan and self.pex is None:
            log.warning("enable_subnet_scan is on but enable_pex is off - subnet scan needs PEX to probe with, skipping")
        self.subnet_scan = (
            SubnetScanner(self.pex, config.listen_port,
                          interval_s=config.subnet_scan_interval_s, max_hosts=config.subnet_scan_max_hosts)
            if config.enable_subnet_scan and self.pex is not None
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
        self._audio_watchdog_task: asyncio.Task | None = None
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

        # Echo guard (see OutpostConfig.enable_echo_guard) - same always-
        # construct/mutable-flag pattern as AGC/relay/tx_lockout above.
        self.enable_echo_guard = config.enable_echo_guard
        self.echo_guard_hold_s = config.echo_guard_hold_ms / 1000.0
        # monotonic time this device's own speaker last played anything -
        # updated by _play_on_speaker(), the single path everything that
        # plays audio locally goes through.
        self._last_own_speaker_activity = 0.0
        # Precomputed once - sample rate/frame size are fixed for the life
        # of the client.
        self._lockout_tone_frames = _make_tone_frames(
            self.codec.frame_size, self.codec.sample_rate,
            _LOCKOUT_TONE_HZ, _LOCKOUT_TONE_MS, _LOCKOUT_TONE_AMPLITUDE,
        )

        # Optional human-readable name sent once per PTT press (see
        # OutpostConfig.sender_alias) - mutable mirror so the web UI can
        # change it live, same pattern as agc_enabled etc. above.
        self.sender_alias = config.sender_alias
        # channel_id -> {sender_id: (alias | None, last_seen_monotonic)} -
        # who's currently transmitting on each channel, for the web UI's
        # "who is speaking" display (see speaking_details below).
        self._channel_speaking: dict[int, dict[int, tuple[str | None, float]]] = {}

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
            self._play_on_speaker(frame)

    def _play_on_speaker(self, frame: bytes) -> None:
        """Every path that plays audio through this device's own speaker
        goes through here, so echo_guard (see _tx_loop) knows when this
        device was last actually making sound."""
        self._last_own_speaker_activity = time.monotonic()
        self.speaker.play_frame(frame)

    def _own_speaker_active(self) -> bool:
        """True if this device's own speaker played something within the
        last echo_guard_hold_s - used to mute the mic so audio bleeding from
        this device's own speaker (e.g. two devices placed too close
        together) isn't picked back up and re-transmitted into a feedback
        loop."""
        return (time.monotonic() - self._last_own_speaker_activity) < self.echo_guard_hold_s

    def set_ptt(self, pressed: bool) -> None:
        """Call from a PTTSource callback (may run on any thread)."""
        if pressed and self.enable_tx_lockout and self.channel_busy(self.active_channel.id):
            log.debug("PTT press ignored - channel busy (tx lockout)")
            self._play_lockout_tone()
            return
        self._ptt_active = pressed
        cipher = self.ciphers[self.active_channel.id]
        targets = self._channel_target_peers(self.active_channel.id)
        try:
            if pressed:
                # Sent once per transmission (not per AUDIO frame) so other
                # users learn who's speaking without paying per-frame
                # overhead for it - see protocol.TYPE_START.
                seq = self.transport.next_seq()
                alias_bytes = self.sender_alias.encode("utf-8") if self.sender_alias else b""
                ciphertext = cipher.encrypt(self.config.sender_id, seq, alias_bytes)
                self.transport.send(self.active_channel.id, seq, ciphertext, ptype=TYPE_START, targets=targets)
            else:
                seq = self.transport.next_seq()
                ciphertext = cipher.encrypt(self.config.sender_id, seq, b"")
                self.transport.send(self.active_channel.id, seq, ciphertext, ptype=TYPE_STOP, targets=targets)
        except RuntimeError:
            # Transport not started yet (e.g. PTT pressed during startup) -
            # local PTT state still toggles above; the peer just won't get a
            # START/STOP for this edge. Must not raise into a PTTSource
            # callback, which may run on an arbitrary driver/audio thread.
            log.debug("PTT edge not sent - transport not started")

    def set_active_channel(self, name: str) -> None:
        self.active_channel = self.config.channel_by_name(name)
        if self.channel_announcer is not None:
            self.channel_announcer.announce(f"Channel {name}")

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

    def add_peer(self, host: str, port: int) -> Peer:
        """Adds a static peer to the running process (web UI's Peers page) -
        no restart needed. Keyed "static-" like config-loaded peers so it's
        exempt from prune_stale (see MeshTransport)."""
        peer = Peer(host=host, port=port)
        self.config.peers.append(peer)
        self.transport.add_peer(f"static-{host}:{port}", peer)
        return peer

    def remove_peer(self, host: str, port: int) -> None:
        """Removes a static peer immediately. Also drops any live transport
        entry at that address, even if it was since confirmed under a
        sender_id-keyed identity (see MeshTransport.confirm_peer) rather
        than still sitting under its original static- key."""
        self.config.peers[:] = [p for p in self.config.peers if (p.host, p.port) != (host, port)]
        for detail in self.transport.peer_details():
            if (detail["host"], detail["port"]) == (host, port):
                self.transport.remove_peer(detail["key"])

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

    def speaking_details(self, channel_id: int | None = None) -> list[dict]:
        """Senders currently transmitting on `channel_id` (default: active
        channel) - sender_id, alias (if one was sent/known), and seconds
        since their last AUDIO/START frame. Best-effort: normally cleared by
        a received STOP, with a short staleness fallback (_SPEAKING_STALE_S)
        in case that STOP is lost."""
        if channel_id is None:
            channel_id = self.active_channel.id
        now = time.monotonic()
        return [
            {"sender_id": sender_id, "alias": alias, "age_s": round(now - last_seen, 1)}
            for sender_id, (alias, last_seen) in self._channel_speaking.get(channel_id, {}).items()
            if now - last_seen < _SPEAKING_STALE_S
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
        if self.subnet_scan:
            self.subnet_scan.start()
        self.mic.start()
        self.speaker.start()
        self._tx_task = asyncio.create_task(self._tx_loop())
        self._channel_announce_task = asyncio.create_task(self._channel_announce_loop())
        self._audio_watchdog_task = asyncio.create_task(self._audio_watchdog_loop())
        if self.web_ui:
            await self.web_ui.start()

    async def stop(self) -> None:
        if self._tx_task:
            self._tx_task.cancel()
            self._tx_task = None
        if self._channel_announce_task:
            self._channel_announce_task.cancel()
            self._channel_announce_task = None
        if self._audio_watchdog_task:
            self._audio_watchdog_task.cancel()
            self._audio_watchdog_task = None
        if self.web_ui:
            await self.web_ui.stop()
        if self.pex:
            self.pex.stop()
        if self.subnet_scan:
            self.subnet_scan.stop()
        if self.discovery:
            self.discovery.stop()
        self.mic.stop()
        self.speaker.stop()
        self.transport.stop()

    # --- audio device recovery -------------------------------------------

    async def _audio_watchdog_loop(self) -> None:
        """Reopens the mic/speaker when they stop delivering audio - e.g.
        Bluetooth earbuds disconnecting, or a stream stalling across laptop
        sleep. Without this a dead mic fails silently: PTT still sends
        TYPE_START (so peers see who's "speaking") but _tx_loop never gets a
        frame, so no audio goes out. While a configured device is missing
        the system default stands in, and the configured one is switched
        back to once it reappears."""
        loop = asyncio.get_running_loop()
        streams = (self.mic, self.speaker)
        last_device_check = time.monotonic()
        failing = False
        while True:
            await asyncio.sleep(_AUDIO_WATCHDOG_INTERVAL_S)
            reason = None
            stalled = [s.kind for s in streams if s.stalled()]
            if stalled:
                reason = f"{' and '.join(stalled)} stopped delivering audio"
            elif (any(s.on_fallback for s in streams)
                  and time.monotonic() - last_device_check >= _AUDIO_DEVICE_RECHECK_S):
                last_device_check = time.monotonic()
                try:
                    devices = await list_devices_fresh()
                except Exception as exc:
                    log.debug("audio device recheck failed: %s", exc)
                    continue
                back = [s.kind for s in streams if s.on_fallback and device_available(s.device, devices[s.kind])]
                if back:
                    reason = f"configured {' and '.join(back)} device is available again"
            if reason is None:
                continue

            if not failing:
                log.warning("audio: %s - reopening audio devices", reason)
            try:
                await loop.run_in_executor(None, restart_streams, *streams)
            except Exception as exc:
                if not failing:
                    log.error("could not reopen audio devices (%s) - will keep retrying", exc)
                failing = True
            else:
                if failing:
                    log.info("audio devices reopened")
                failing = False

    # --- transmit path ----------------------------------------------------

    async def _tx_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            frame = await loop.run_in_executor(None, self.mic.read_frame)
            if not self._ptt_active:
                continue
            if self.enable_echo_guard and self._own_speaker_active():
                frame = b"\x00" * len(frame)
            else:
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

    # Cap on how many synthetic loss-concealment frames a single seq gap can
    # produce (see _on_packet's TYPE_AUDIO handling) - a real burst loss on a
    # lossy link is at most a handful of frames; anything larger reflects
    # non-audio traffic sharing the seq counter during an idle gap, not
    # genuine loss, and must not flood playback with fake audio.
    _MAX_CONCEALED_FRAMES = 10

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

        if self.enable_relay and header.ptype in (TYPE_AUDIO, TYPE_START, TYPE_STOP, TYPE_CHANNEL_ANNOUNCE):
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
            members = self._channel_members.setdefault(header.channel_id, {})
            if self.transport.confirm_peer(header.sender_id, peer):
                members[header.sender_id] = (peer, time.monotonic())
            else:
                # Address rejected (e.g. relayed through another node's
                # address, not this sender's own) - keep whatever address we
                # already trusted for this sender, if any, just refresh
                # liveness so a relayed-only member doesn't go stale/get
                # dropped from _channel_target_peers.
                existing = members.get(header.sender_id)
                if existing is not None:
                    members[header.sender_id] = (existing[0], time.monotonic())
            return

        if header.channel_id != self.active_channel.id:
            return  # only play audio for the currently selected channel

        cipher = self.ciphers.get(header.channel_id)
        if cipher is None:
            return  # we don't hold this channel's key - can't be a member

        if header.ptype == TYPE_START:
            try:
                alias_bytes = cipher.decrypt(header.sender_id, header.seq, ciphertext)
            except ValueError:
                log.debug("dropping unauthenticated start from %s", addr)
                return
            alias = alias_bytes.decode("utf-8", errors="replace") or None
            self.transport.confirm_peer(header.sender_id, Peer(host=addr[0], port=addr[1]))
            self._channel_last_audio[header.channel_id] = time.monotonic()
            self._channel_speaking.setdefault(header.channel_id, {})[header.sender_id] = (alias, time.monotonic())
            return
        if header.ptype == TYPE_STOP:
            try:
                cipher.decrypt(header.sender_id, header.seq, ciphertext)
            except ValueError:
                log.debug("dropping unauthenticated stop from %s", addr)
                return
            self._last_seq_seen.pop(header.sender_id, None)
            self.transport.confirm_peer(header.sender_id, Peer(host=addr[0], port=addr[1]))
            self._channel_last_audio.pop(header.channel_id, None)
            self._channel_speaking.get(header.channel_id, {}).pop(header.sender_id, None)
            return
        if header.ptype != TYPE_AUDIO:
            return

        last_seq = self._last_seq_seen.get(header.sender_id)
        if last_seq is not None and header.seq <= last_seq:
            log.debug("dropping stale/duplicate/out-of-order audio from sender %d (seq %d <= last %d)",
                      header.sender_id, header.seq, last_seq)
            return  # drop rather than play audio backwards

        try:
            opus_bytes = cipher.decrypt(header.sender_id, header.seq, ciphertext)
        except ValueError:
            log.debug("dropping packet with bad auth tag from %s", addr)
            return

        # Only an authenticated sender teaches us its address - raw UDP
        # source addresses are trivially spoofable and must not be trusted.
        self.transport.confirm_peer(header.sender_id, Peer(host=addr[0], port=addr[1]))
        self._channel_last_audio[header.channel_id] = time.monotonic()
        # Keep "who's speaking" fresh even if this sender's TYPE_START was
        # lost - preserve whatever alias we already know (if any) rather
        # than dropping it back to unknown.
        speaking = self._channel_speaking.setdefault(header.channel_id, {})
        prev_alias = speaking.get(header.sender_id, (None, 0.0))[0]
        speaking[header.sender_id] = (prev_alias, time.monotonic())

        if last_seq is not None and header.seq == last_seq + 2:
            # Exactly one frame missing - try Opus in-band FEC to recover it
            # from the redundancy carried in this packet before falling back
            # to plain concealment.
            try:
                recovered = self.codec.decode_fec(opus_bytes)
                self._play_on_speaker(recovered)
            except Exception:
                self._play_on_speaker(self.codec.decode_loss())
        elif last_seq is not None and header.seq > last_seq + 1:
            missing = header.seq - last_seq - 1
            # seq is one counter shared across every packet type from this
            # sender (START/STOP/CHANNEL_ANNOUNCE/PEX too - see transport.py)
            # so it never repeats a nonce, but it also means a gap here isn't
            # purely lost AUDIO frames - a sender idle between transmissions
            # still advances seq via its periodic channel-announce/PEX
            # traffic, which would otherwise inflate `missing` with every
            # minute of silence and flood playback with fake concealment
            # right as real audio resumes. Only treat a small gap as genuine
            # loss; a larger one almost certainly reflects elapsed idle time,
            # not dropped frames, so just play the real frame as-is.
            if missing <= self._MAX_CONCEALED_FRAMES:
                for _ in range(missing):
                    self._play_on_speaker(self.codec.decode_loss())
            else:
                log.debug("seq gap of %d from sender %d treated as idle time, not loss - skipping concealment",
                          missing, header.sender_id)

        self._last_seq_seen[header.sender_id] = header.seq
        pcm = self.codec.decode(opus_bytes)
        self._play_on_speaker(pcm)
