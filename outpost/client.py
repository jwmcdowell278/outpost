"""Outpost voice client: wires together audio I/O, PTT, codec, encryption and
the mesh transport for a configured set of channels."""
from __future__ import annotations
import asyncio
import logging
import random
import time

from .agc import AutomaticGainControl
from .audio import MicCapture, SpeakerPlayback
from .codec import OpusCodec
from .crypto import ChannelCipher
from .discovery import PeerDiscovery
from .noise_suppression import NoiseSuppressor, OnnxNoiseSuppressor, PassthroughSuppressor
from .pex import PexGossip
from .protocol import TYPE_AUDIO, TYPE_STOP, TYPE_PEX, TYPE_CHANNEL_ANNOUNCE
from .transport import MeshTransport
from .config import OutpostConfig, Peer
from .webui import WebUI
from zeroconf._exceptions import EventLoopBlocked

log = logging.getLogger(__name__)


class OutpostClient:
    def __init__(
        self,
        config: OutpostConfig,
        active_channel_name: str,
        noise_suppressor: NoiseSuppressor | None = None,
        agc: AutomaticGainControl | None = None,
    ):
        self.config = config
        self.active_channel = config.channel_by_name(active_channel_name)
        self.noise_suppressor = noise_suppressor or (
            OnnxNoiseSuppressor(config.noise_suppressor_model_path)
            if config.noise_suppressor_model_path
            else PassthroughSuppressor()
        )
        self.agc = agc if agc is not None else (
            AutomaticGainControl(target_rms=config.agc_target_rms) if config.enable_agc else None
        )

        self.codec = OpusCodec(
            bitrate=config.bitrate,
            frame_ms=config.frame_ms,
            inband_fec=config.inband_fec,
        )
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
                on_peer_added=self.transport.add_peer,
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

        # channel_id -> {sender_id: (Peer, last_seen_monotonic)} - built from
        # authenticated channel-announce beacons, so AUDIO/STOP sends can
        # target only known members instead of every peer in the mesh.
        self._channel_members: dict[int, dict[int, tuple[Peer, float]]] = {}
        self._membership_ttl_s = max(config.channel_announce_interval_s * 2.5, 60.0)

    # --- PTT control --------------------------------------------------

    def set_ptt(self, pressed: bool) -> None:
        """Call from a PTTSource callback (may run on any thread)."""
        self._ptt_active = pressed
        if not pressed:
            cipher = self.ciphers[self.active_channel.id]
            seq = self.transport.next_seq()
            ciphertext = cipher.encrypt(self.config.sender_id, seq, b"")
            targets = self._channel_target_peers(self.active_channel.id)
            self.transport.send(self.active_channel.id, seq, ciphertext, ptype=TYPE_STOP, targets=targets)

    def set_active_channel(self, name: str) -> None:
        self.active_channel = self.config.channel_by_name(name)

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
            if self.agc:
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

    def _on_packet(self, header, ciphertext: bytes, addr) -> None:
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
            self.transport.add_peer(f"peer-{header.sender_id}", peer)
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
            self.transport.add_peer(f"peer-{header.sender_id}", Peer(host=addr[0], port=addr[1]))
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
        self.transport.add_peer(f"peer-{header.sender_id}", Peer(host=addr[0], port=addr[1]))

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
