"""Outpost voice client: wires together audio I/O, PTT, codec, encryption and
the mesh transport for a configured set of channels."""
from __future__ import annotations
import asyncio
import ipaddress
import logging
import math
import os
import random
import threading
import time
from typing import NamedTuple
from dataclasses import replace

import numpy as np

from .agc import AutomaticGainControl
from .announce import ChannelAnnouncer
try:
    from .audio import MicCapture, SpeakerPlayback, device_available, list_devices_fresh, restart_streams
except OSError as _audio_import_error:
    # PortAudio isn't installed - fine for a relay-only node (e.g. a
    # server), which never opens audio devices. A normal client fails
    # with this error when it constructs its audio streams.
    MicCapture = SpeakerPlayback = None
    _AUDIO_IMPORT_ERROR: OSError | None = _audio_import_error
else:
    _AUDIO_IMPORT_ERROR = None
from .bluetooth import BluetoothManager
from .buttons import ButtonController
from .codec import OpusCodec, OpusDecoder
from .mixer import Mixer
from .crypto import ChannelCipher
from .discovery import PeerDiscovery
from .dsp import HighPassFilter, Squelch
from .noise_suppression import NoiseSuppressor, PassthroughSuppressor
from .pex import PexGossip
from .replay import ReplayWindow
from .protocol import audio_pad_size, pad_audio, unpad_audio, TYPE_AUDIO, TYPE_STOP, TYPE_START, TYPE_PEX, TYPE_CHANNEL_ANNOUNCE, TYPE_BRIDGE_HELLO, encode_packet
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
# When reopening doesn't bring audio back, wait longer before each further
# try, from _MIN up to _MAX. macOS doesn't run audio at all while it's
# asleep with the lid closed (including its periodic background wakes), and
# retrying every few seconds for hours toggled the mic on and off tens of
# thousands of times overnight - enough to wedge the menu bar's mic
# indicator.
_AUDIO_RETRY_MIN_S = 3.0
# How long startup waits for the mic/speaker to open before carrying on.
_AUDIO_OPEN_TIMEOUT_S = 10.0
# With release_audio_when_idle: how long the speaker stays open after the
# last sound, and the mic after PTT is released.
_AUDIO_IDLE_RELEASE_S = 3.0
_AUDIO_RETRY_MAX_S = 300.0

# Local-only "PTT blocked" busy tone (tx lockout) - short and quiet enough
# not to be mistaken for an incoming transmission.
_LOCKOUT_TONE_HZ = 480.0
_LOCKOUT_TONE_MS = 150.0
_LOCKOUT_TONE_AMPLITUDE = 8000

# "Talk permit" chirp (talk_permit_tone): played once the mic is actually
# delivering audio after a PTT press - higher and shorter than the lockout
# tone so the two can't be confused.
_PERMIT_TONE_HZ = 1200.0
_PERMIT_TONE_MS = 80.0
_PERMIT_TONE_AMPLITUDE = 7000

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


class _BridgeInfo(NamedTuple):
    """What a bridge hello told us about another bridge."""
    addr: tuple[str, int]   # where its hellos come from (its site address)
    priority: int
    hosts: frozenset        # all its IPv4 addresses
    covers: frozenset       # sender_ids of silent bridges it stands in for
    seen: float


def _unique(peers: list[Peer]) -> list[Peer]:
    return list(dict.fromkeys(peers))


def _auto_multicast_interface(sys_net: str = "/sys/class/net") -> str | None:
    """The interface carrying the batman-adv mesh, for when
    multicast_interface isn't set: a bridge with a batman interface in it
    (e.g. br-lan containing bat0), else the batman interface itself. Linux
    only (batman-adv is); None elsewhere or with no mesh, meaning the OS
    default interface."""
    try:
        names = sorted(os.listdir(sys_net))
    except OSError:
        return None

    def is_batman(name: str) -> bool:
        return name.startswith("bat") or os.path.isdir(os.path.join(sys_net, name, "mesh"))

    for name in names:
        try:
            members = os.listdir(os.path.join(sys_net, name, "brif"))
        except OSError:
            continue
        if any(is_batman(m) for m in members):
            return name
    return next((n for n in names if is_batman(n)), None)


def _site_networks(config, interface: str | None) -> list[ipaddress.IPv4Network]:
    """This node's site: site_subnets if set, else the subnet of the
    multicast interface (configured or detected) when multicast is on."""
    if config.site_subnets:
        return [ipaddress.IPv4Network(n) for n in config.site_subnets]
    if not (config.enable_multicast and interface):
        return []
    try:
        import ifaddr
        wanted = _resolve_interface_ip(interface)
        for adapter in ifaddr.get_adapters():
            for ip in adapter.ips:
                if ip.is_IPv4 and ip.ip == wanted:
                    return [ipaddress.IPv4Network(f"{ip.ip}/{ip.network_prefix}", strict=False)]
    except OSError:
        pass
    return []


def _resolve_interface_ip(interface: str | None) -> str | None:
    """multicast_interface may be an IPv4 address or an interface name
    (e.g. "bat0", "br-lan"); returns its IPv4 address, or None for the OS
    default."""
    if not interface:
        return None
    try:
        return str(ipaddress.IPv4Address(interface))
    except ValueError:
        pass
    import ifaddr
    for adapter in ifaddr.get_adapters():
        if adapter.nice_name == interface or adapter.name == interface:
            for ip in adapter.ips:
                if ip.is_IPv4:
                    return ip.ip
    raise OSError(f"multicast_interface {interface!r} not found or has no IPv4 address")


class OutpostClient:
    def __init__(
        self,
        config: OutpostConfig,
        active_channel_name: str | None,
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
        # Relay-only: no audio, PTT or channels - just networking and relay
        # forwarding (see OutpostConfig.relay_only).
        self.relay_only = config.relay_only
        if self.relay_only:
            config = replace(config, enable_relay=True, channels={}, monitor_channels=[])
            self.config = config
            self.active_channel = None
        else:
            if active_channel_name is None:
                raise ValueError("an active channel is required unless relay_only is set")
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
        # Fixed AUDIO payload size (None = off) - see protocol.pad_audio.
        self.audio_pad = (audio_pad_size(config.bitrate, config.frame_ms)
                          if config.constant_size_audio else None)

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

        if not self.relay_only and _AUDIO_IMPORT_ERROR is not None:
            raise RuntimeError(f"audio support unavailable ({_AUDIO_IMPORT_ERROR}) - install PortAudio, "
                               "or set relay_only: true to run as a relay without audio")
        self.mic = (None if self.relay_only else
                    MicCapture(self.codec.sample_rate, self.codec.frame_size, device=config.input_device))
        # Received audio goes through per-sender jitter buffers and is mixed
        # (see mixer.py); the speaker's callback pulls each frame from it.
        self.mixer = Mixer(
            self.codec.frame_size, config.frame_ms,
            decoder_factory=lambda: OpusDecoder(self.codec.sample_rate, frame_ms=config.frame_ms),
        )
        self.mixer.duck_enabled = config.duck_monitored_channels
        self.mixer.active_channel_id = self.active_channel.id if self.active_channel else None
        self.speaker = (None if self.relay_only else
                        SpeakerPlayback(self.codec.sample_rate, self.codec.frame_size,
                                        device=config.output_device, source=self.mixer.next_frame))
        # Channels listened to besides the active one (by id, so renames
        # don't break them) - see OutpostConfig.monitor_channels.
        self.monitored_channel_ids: set[int] = {
            config.channel_by_name(name).id for name in config.monitor_channels
        }
        for name, volume in config.channel_volumes.items():
            try:
                self.mixer.volumes[config.channel_by_name(name).id] = volume
            except KeyError:
                log.warning("channel_volumes: ignoring unknown channel %r", name)
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
            on_multicast_packet=lambda h, c, a: self._on_packet(h, c, a, via_multicast=True),
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
        # This node's own site, for bridging (see is_bridge).
        # multicast_interface, or the batman-adv interface if one is found.
        self._multicast_interface = config.multicast_interface or (
            _auto_multicast_interface() if config.enable_multicast else None)
        self._site_networks = _site_networks(config, self._multicast_interface)
        self.pex = (
            PexGossip(config.sender_id, config.mesh_key, self.transport,
                      interval_s=config.pex_interval_s, fanout=config.pex_fanout,
                      # Only a node with a site can be a bridge; everyone
                      # else shares its whole list in one packet.
                      share_filter=((lambda target, peer: not self.is_bridge or self._same_side(target, peer))
                                    if self._site_networks else None),
                      on_gossip=self._note_gossip)
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
        # Hardware buttons (PTT, volume, channel) - see buttons.py. Only read
        # input devices when there's something to do with them: bindings to
        # act on, or the web UI's Buttons page to set them up from.
        self.buttons = (
            ButtonController(self, config.button_bindings)
            if not self.relay_only and (config.button_bindings or config.enable_web_ui)
            else None
        )
        # Shared with the web UI's Bluetooth page.
        self.bluetooth = BluetoothManager()
        self._bluetooth_task: asyncio.Task | None = None
        self.web_ui = (
            WebUI(self, host=config.web_ui_host, port=config.web_ui_port, token=config.web_ui_token)
            if config.enable_web_ui
            else None
        )

        self._ptt_active = False
        # release_audio_when_idle state - see _demand_audio.
        self._release_audio = config.release_audio_when_idle
        self._audio_lock = threading.Lock()  # one open/close/restart at a time
        self._audio_opening: set[str] = set()
        self._ptt_released_at = 0.0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._audio_idle_task: asyncio.Task | None = None
        self._tx_task: asyncio.Task | None = None
        self._channel_announce_task: asyncio.Task | None = None
        self._audio_watchdog_task: asyncio.Task | None = None
        # Rejects replayed channel packets - see replay.py.
        self._replay = ReplayWindow()
        self._replay_warned_at: dict[int, float] = {}
        # Random per-run marker carried in our CHANNEL_ANNOUNCE payloads. If
        # we ever receive an authenticated announce with our own sender_id
        # but a different marker, another node is using our sender_id -
        # see _check_sender_id_clash.
        self._instance_token = os.urandom(8)
        self._clash_warned_at = -1e9

        # Set by the web UI's Settings page "Restart" button - scripts/
        # run_client.py's main loop waits on this alongside SIGINT/SIGTERM
        # and, if it fires, cleanly stops and re-execs itself (same argv) so
        # restart-required config changes take effect without the user
        # needing shell access.
        self.restart_requested = asyncio.Event()

        # channel_id -> {sender_id: (Peer, last_seen_monotonic)} - built from
        # authenticated channel-announce beacons, so AUDIO/STOP sends can
        # target only known members instead of every peer in the mesh.
        # channel_id -> {sender_id: (Peer, last_seen, listening)} - listening
        # is whether that member is actually playing the channel (from its
        # announce), not just holding the key; only listeners get audio.
        self._channel_members: dict[int, dict[int, tuple[Peer, float, bool]]] = {}
        # sender_id -> ((host, port), last_seen) for senders whose packets
        # reach us by multicast, i.e. on this site - see _channel_delivery.
        self._mcast_heard: dict[int, tuple[tuple[str, int], float]] = {}
        self.multicast_min_listeners = config.multicast_min_listeners
        # (channel_id, sender_id) -> when that member was last confirmed at
        # its own address (not via a relay) - see the announce handling.
        self._member_direct_seen: dict[tuple[int, int], float] = {}
        self._membership_ttl_s = max(config.channel_announce_interval_s * 2.5, 60.0)
        self._peer_ttl_s = max(config.pex_interval_s * 2.5, 90.0)

        # (sender_id, ptype) -> highest seq already relayed - loop/duplicate
        # guard for relay mode (see `_relay_packet`). Bounded below since
        # sender_id here is an unauthenticated cleartext field an attacker
        # could vary freely to grow this table.
        self._relay_seen: dict[tuple[int, int], int] = {}
        # Relay next-hop table: channel_id -> {(host, port): last seen} of
        # neighbours that CHANNEL_ANNOUNCEs for that channel have arrived
        # from - see _relay_targets.
        self._relay_routes: dict[int, dict[tuple[str, int], float]] = {}
        # (host, port) -> last seen, for known peers whose packets reached
        # this relay by multicast. Unlike _mcast_heard this needs no channel
        # key, so a keyless relay/bridge can still tell which site nodes get
        # multicast - see _site_multicast_addresses.
        self._relay_mcast_heard: dict[tuple[str, int], float] = {}
        # (host, port) -> (addresses it reaches directly, when it said so),
        # from its peer exchange - see _forward.
        self._peer_reach: dict[tuple[str, int], tuple[frozenset, float]] = {}
        # Redundant bridges (see "redundant bridges" below), from hellos.
        self._mesh_cipher = ChannelCipher(config.mesh_key)
        self._bridge_hellos: dict[int, _BridgeInfo] = {}
        self._announce_cache: dict[tuple[int, int], tuple] = {}
        self._live_bridges: set[int] = set()  # see _check_bridges
        self._hello_count = 0
        self._bridge_hello_task: asyncio.Task | None = None

        # Settable live from the web UI - see the enable_relay property.
        self._enable_relay = config.enable_relay

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
        # updated by _play_local() for local sounds; received audio is
        # tracked by mixer.last_active (see _own_speaker_active).
        self._last_own_speaker_activity = 0.0
        # Precomputed once - sample rate/frame size are fixed for the life
        # of the client.
        self._lockout_tone_frames = _make_tone_frames(
            self.codec.frame_size, self.codec.sample_rate,
            _LOCKOUT_TONE_HZ, _LOCKOUT_TONE_MS, _LOCKOUT_TONE_AMPLITUDE,
        )
        self.talk_permit_tone = config.talk_permit_tone
        self._permit_tone_frames = _make_tone_frames(
            self.codec.frame_size, self.codec.sample_rate,
            _PERMIT_TONE_HZ, _PERMIT_TONE_MS, _PERMIT_TONE_AMPLITUDE,
        )
        self._awaiting_permit = False
        # Mic frames are sent as silence until then, so the chirp itself
        # isn't transmitted.
        self._permit_mute_until = 0.0

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
            if self.active_channel is None:
                return False
            channel_id = self.active_channel.id
        last_audio = self._channel_last_audio.get(channel_id)
        if last_audio is None:
            return False
        return (time.monotonic() - last_audio) < self.tx_lockout_timeout_s

    def _play_lockout_tone(self) -> None:
        """Local-only audible feedback that a PTT press was blocked - never
        transmitted. Mixed over whatever is currently playing."""
        self._play_local(self._lockout_tone_frames)

    def _play_local(self, frames: list[bytes]) -> None:
        """Plays locally-generated sound (not received audio) through the
        mixer, recording it for echo_guard."""
        self._last_own_speaker_activity = time.monotonic()
        self.mixer.play_local(frames)
        self._demand_audio_threadsafe("output")

    def _own_speaker_active(self) -> bool:
        """True if this device's own speaker played something within the
        last echo_guard_hold_s - used to mute the mic so audio bleeding from
        this device's own speaker (e.g. two devices placed too close
        together) isn't picked back up and re-transmitted into a feedback
        loop."""
        last = max(self._last_own_speaker_activity, self.mixer.last_active)
        return (time.monotonic() - last) < self.echo_guard_hold_s

    def set_ptt(self, pressed: bool) -> None:
        """Call from a PTTSource callback (may run on any thread)."""
        if self.active_channel is None:
            return  # relay-only: nothing to transmit
        if pressed:
            self._demand_audio_threadsafe("input")
        else:
            self._ptt_released_at = time.monotonic()
        if pressed and self.enable_tx_lockout and self.channel_busy(self.active_channel.id):
            log.debug("PTT press ignored - channel busy (tx lockout)")
            self._play_lockout_tone()
            return
        self._ptt_active = pressed
        if pressed and self.talk_permit_tone:
            self._awaiting_permit = True
        cipher = self.ciphers[self.active_channel.id]
        targets, group = self._channel_delivery(self.active_channel.id)
        if pressed:
            # Keep our AUDIO seqs contiguous for receivers' jitter buffers.
            self.transport.hold_control(True)
        try:
            if pressed:
                # Sent once per transmission (not per AUDIO frame) so other
                # users learn who's speaking without paying per-frame
                # overhead for it - see protocol.TYPE_START.
                alias_bytes = self.sender_alias.encode("utf-8") if self.sender_alias else b""
                self.transport.send_encrypted(cipher, self.active_channel.id, TYPE_START, alias_bytes,
                                              targets=targets, multicast_group=group)
            else:
                self.transport.send_encrypted(cipher, self.active_channel.id, TYPE_STOP, b"",
                                              targets=targets, multicast_group=group)
        except RuntimeError:
            # Transport not started yet (e.g. PTT pressed during startup) -
            # local PTT state still toggles above; the peer just won't get a
            # START/STOP for this edge. Must not raise into a PTTSource
            # callback, which may run on an arbitrary driver/audio thread.
            log.debug("PTT edge not sent - transport not started")
        if not pressed:
            self.transport.hold_control(False)

    def set_active_channel(self, name: str) -> None:
        previous = self.active_channel
        self.active_channel = self.config.channel_by_name(name)
        self.mixer.active_channel_id = self.active_channel.id
        if previous.id != self.active_channel.id and previous.id not in self.monitored_channel_ids:
            self.mixer.remove_channel(previous.id)
        if previous.id != self.active_channel.id:
            self._listening_changed({previous.id, self.active_channel.id})
        if self.channel_announcer is not None:
            self.channel_announcer.announce(f"Channel {name}")

    def step_channel(self, delta: int) -> None:
        """Make the next (+1) or previous (-1) channel, by id, the active
        one - wrapping around at either end."""
        if self.active_channel is None or not self.config.channels:
            return
        ids = sorted(self.config.channels)
        index = ids.index(self.active_channel.id) if self.active_channel.id in ids else 0
        self.set_active_channel(self.config.channels[ids[(index + delta) % len(ids)]].name)

    def adjust_volume(self, delta: float) -> float:
        """Raise/lower the active channel's volume by `delta`, kept within
        0.0-1.0. Returns the new volume."""
        if self.active_channel is None:
            return 0.0
        volume = round(min(1.0, max(0.0, self.channel_volume(self.active_channel.id) + delta)), 2)
        self.set_channel_volume(self.active_channel.name, volume)
        return volume

    def listening_channel_ids(self) -> set[int]:
        active = {self.active_channel.id} if self.active_channel else set()
        return active | self.monitored_channel_ids

    @property
    def enable_relay(self) -> bool:
        return self._enable_relay

    @enable_relay.setter
    def enable_relay(self, value: bool) -> None:
        if self.relay_only and not value:
            raise ValueError("relay can't be turned off on a relay-only node")
        # Relays join extra multicast groups (see _update_groups).
        self._enable_relay = value
        self._update_groups()

    @property
    def is_bridge(self) -> bool:
        """A relay that knows its own site: it forwards between its site and
        other sites (split horizon) instead of to everyone."""
        return self.enable_relay and bool(self._site_networks)

    def _is_site_local(self, host: str) -> bool:
        try:
            ip = ipaddress.IPv4Address(host)
        except ValueError:
            return False
        return any(ip in net for net in self._site_networks)

    def _same_side(self, a: Peer, b: Peer) -> bool:
        """Whether to tell `a` about `b`. Another bridge on this site is on
        both sides, so it hears about everything."""
        if (a.host, a.port) in self._site_bridge_addresses():
            return True
        return self._is_site_local(a.host) == self._is_site_local(b.host)

    def _site_bridge_addresses(self) -> set[tuple[str, int]]:
        return {b.addr for b in self._site_bridge_peers().values()}

    def _group(self, channel_id: int) -> str:
        """Multicast group for a channel; channel 0 is the shared group for
        channel announces."""
        return str(ipaddress.IPv4Address(self.config.multicast_group_base) + channel_id)

    def _update_groups(self) -> None:
        """Join the announce group plus the group of every channel being
        listened to - with batman-adv multicast optimizations, that keeps a
        channel's audio away from parts of the mesh nobody listens on."""
        if self.transport.multicast_enabled:
            channels = set(self.listening_channel_ids())
            if self.enable_relay:
                # A relay carries site traffic for channels it has routes
                # for, keys or not: talkers leave listeners reached through
                # it to the multicast (see _channel_delivery).
                channels |= self._routed_channels(self._MAX_RELAY_GROUPS)
            self.transport.set_groups({self._group(0)} | {self._group(c) for c in channels})

    # Extra groups a relay joins beyond its own channels. Linux allows 20
    # memberships per socket by default (net.ipv4.igmp_max_memberships), and
    # channel ids come from unauthenticated announce headers.
    _MAX_RELAY_GROUPS = 16

    def _routed_channels(self, limit: int) -> set[int]:
        """Channels with a live relay route, most recently announced first."""
        cutoff = time.monotonic() - self._membership_ttl_s
        latest = {cid: max(routes.values()) for cid, routes in self._relay_routes.items() if routes}
        fresh = sorted((seen, cid) for cid, seen in latest.items() if seen >= cutoff and cid != 0)
        return {cid for _, cid in fresh[-limit:]}

    def _listening_changed(self, channel_ids) -> None:
        """Rejoin groups and tell members straight away (rather than at the
        next periodic announce) that we've started/stopped listening, so
        audio starts reaching us - or stops being sent to us - promptly."""
        self._update_groups()
        for channel_id in channel_ids:
            try:
                self._send_announce(channel_id)
            except RuntimeError:
                pass  # transport not started yet

    def set_monitor(self, name: str, listen: bool) -> None:
        """Start/stop listening to a channel besides the active one. Raises
        KeyError for an unknown channel name."""
        channel_id = self.config.channel_by_name(name).id
        if listen:
            self.monitored_channel_ids.add(channel_id)
        else:
            self.monitored_channel_ids.discard(channel_id)
            if channel_id != self.active_channel.id:
                self.mixer.remove_channel(channel_id)
        self.config = replace(self.config, monitor_channels=self.monitored_channel_names())
        self._listening_changed({channel_id})

    def set_channel_volume(self, name: str, volume: float) -> None:
        channel_id = self.config.channel_by_name(name).id
        self.mixer.volumes[channel_id] = volume
        volumes = dict(self.config.channel_volumes)
        volumes[name] = volume
        self.config = replace(self.config, channel_volumes=volumes)

    def monitored_channel_names(self) -> list[str]:
        return [ch.name for cid, ch in self.config.channels.items() if cid in self.monitored_channel_ids]

    def channel_volume(self, channel_id: int) -> float:
        return self.mixer.volumes.get(channel_id, 1.0)

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
        if self.relay_only:
            raise ValueError("a relay-only node holds no channel keys")
        channel = Channel(id=channel_id, name=name, key=key)
        self.config.channels[channel_id] = channel
        self.ciphers[channel_id] = ChannelCipher(key)
        return channel

    def rename_channel(self, channel_id: int, new_name: str) -> None:
        renamed = replace(self.config.channels[channel_id], name=new_name)
        self.config.channels[channel_id] = renamed
        if self.active_channel is not None and self.active_channel.id == channel_id:
            self.active_channel = renamed
        # Monitored channels are tracked by id; refresh the saved names.
        self.config = replace(self.config, monitor_channels=self.monitored_channel_names())

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
            if self.active_channel is None:
                return []
            channel_id = self.active_channel.id
        now = time.monotonic()
        return [
            {"sender_id": sender_id, "host": peer.host, "port": peer.port, "age_s": round(now - last_seen, 1),
             "listening": listening, "multicast": self._is_multicast_local(sender_id)}
            for sender_id, (peer, last_seen, listening) in self._channel_members.get(channel_id, {}).items()
        ]

    def speaking_details(self, channel_id: int | None = None) -> list[dict]:
        """Senders currently transmitting on `channel_id` (default: active
        channel) - sender_id, alias (if one was sent/known), and seconds
        since their last AUDIO/START frame. Best-effort: normally cleared by
        a received STOP, with a short staleness fallback (_SPEAKING_STALE_S)
        in case that STOP is lost."""
        if channel_id is None:
            if self.active_channel is None:
                return []
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
        if self.config.enable_multicast:
            try:
                await self.transport.start_multicast(
                    self.config.multicast_port, _resolve_interface_ip(self._multicast_interface))
                log.info("multicast on %s", self._multicast_interface or "the default interface")
                self._update_groups()
            except OSError as exc:
                log.warning("multicast unavailable (%s) - using unicast only", exc)
        if self.enable_relay:
            if self.is_bridge:
                log.info("relaying as a site bridge for %s (priority %d) - see README \"Bridging sites\"",
                         ", ".join(str(n) for n in self._site_networks), self.config.bridge_priority)
            else:
                log.info("relaying as a plain transit relay (no site configured)")
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
        # The web UI comes up before audio, so it's reachable (to see and fix
        # what's wrong) even when an audio device won't open.
        if self.buttons:
            self.buttons.start()
        if self.config.bluetooth_auto_reconnect and self.bluetooth.unavailable_reason() is None:
            self._bluetooth_task = asyncio.create_task(self.bluetooth.reconnect_loop())
        if self.web_ui:
            await self.web_ui.start()
        self._channel_announce_task = asyncio.create_task(self._channel_announce_loop())
        if self._site_networks:
            self._bridge_hello_task = asyncio.create_task(self._bridge_hello_loop())
        self._loop = asyncio.get_running_loop()
        if not self.relay_only:
            if self._release_audio:
                # Opened on demand - see _demand_audio.
                self._audio_idle_task = asyncio.create_task(self._audio_idle_loop())
            else:
                await self._open_audio()
            self._tx_task = asyncio.create_task(self._tx_loop())
            self._audio_watchdog_task = asyncio.create_task(self._audio_watchdog_loop())

    # --- on-demand audio (release_audio_when_idle) ------------------------

    def _demand_audio_threadsafe(self, kind: str) -> None:
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._demand_audio, kind)

    def _demand_audio(self, kind: str) -> None:
        """Make sure the speaker ("output") or mic ("input") is open, when
        they're opened on demand. Runs on the event loop thread."""
        if not self._release_audio or self.relay_only or self._loop is None:
            return
        stream = self.speaker if kind == "output" else self.mic
        if stream.wanted or kind in self._audio_opening:
            return
        self._audio_opening.add(kind)

        def open_stream():
            with self._audio_lock:
                try:
                    stream.start()
                except Exception as exc:
                    log.warning("could not open the %s audio device (%s)", stream.kind, exc)
                    return False
            return True

        future = self._loop.run_in_executor(None, open_stream)

        def opened(f):
            self._audio_opening.discard(kind)
            if kind == "output" and not f.cancelled() and f.exception() is None and f.result():
                self.mixer.output_started()
        future.add_done_callback(opened)

    def _locked_restart(self, streams) -> None:
        with self._audio_lock:
            restart_streams(*streams)

    def _release_idle_audio(self, now: float | None = None) -> list[str]:
        """Close the speaker/mic if they've been unused for
        _AUDIO_IDLE_RELEASE_S. Returns which ones are closed (for tests)."""
        now = time.monotonic() if now is None else now
        to_close = []
        if (self.speaker.wanted and "output" not in self._audio_opening and not self.mixer.has_pending()
                and now - max(self.mixer.last_active, self._last_own_speaker_activity) > _AUDIO_IDLE_RELEASE_S):
            to_close.append(self.speaker)
        if (self.mic.wanted and "input" not in self._audio_opening and not self._ptt_active
                and now - self._ptt_released_at > _AUDIO_IDLE_RELEASE_S):
            to_close.append(self.mic)
        return to_close

    async def _audio_idle_loop(self) -> None:
        loop = asyncio.get_running_loop()

        def close(streams):
            with self._audio_lock:
                for s in streams:
                    s.stop()
        while True:
            await asyncio.sleep(1.0)
            streams = self._release_idle_audio()
            if streams:
                log.debug("releasing idle audio: %s", ", ".join(s.kind for s in streams))
                try:
                    await asyncio.wait_for(loop.run_in_executor(None, close, streams), _AUDIO_OPEN_TIMEOUT_S)
                except asyncio.TimeoutError:
                    log.warning("closing idle audio devices is taking a long time")

    async def _open_audio(self) -> None:
        """Opens the mic and speaker without letting a bad device stop the
        client from starting: one that fails to open, or hangs opening
        (seen with a stuck Bluetooth hands-free link), is left to the audio
        watchdog to keep retrying."""
        def open_streams():
            for stream in (self.mic, self.speaker):
                try:
                    stream.start()
                except Exception as exc:
                    log.error("could not open the %s audio device (%s) - will keep retrying", stream.kind, exc)
        loop = asyncio.get_running_loop()
        try:
            await asyncio.wait_for(loop.run_in_executor(None, open_streams), _AUDIO_OPEN_TIMEOUT_S)
        except asyncio.TimeoutError:
            log.error("audio devices didn't open within %.0fs (a Bluetooth audio link may be stuck) - "
                      "starting without them and retrying", _AUDIO_OPEN_TIMEOUT_S)

    async def stop(self) -> None:
        if self._audio_idle_task:
            self._audio_idle_task.cancel()
            self._audio_idle_task = None
        if self._tx_task:
            self._tx_task.cancel()
            self._tx_task = None
        if self._channel_announce_task:
            self._channel_announce_task.cancel()
            self._channel_announce_task = None
        if self._audio_watchdog_task:
            self._audio_watchdog_task.cancel()
            self._audio_watchdog_task = None
        if self._bridge_hello_task:
            self._bridge_hello_task.cancel()
            self._bridge_hello_task = None
        if self.web_ui:
            await self.web_ui.stop()
        if self.buttons:
            self.buttons.stop()
        if self._bluetooth_task:
            self._bluetooth_task.cancel()
            self._bluetooth_task = None
        self.bluetooth.stop()
        if self.pex:
            self.pex.stop()
        if self.subnet_scan:
            self.subnet_scan.stop()
        if self.discovery:
            self.discovery.stop()
        if self.mic:
            self.mic.stop()
            self.mic.wake_reader()
        if self.speaker:
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
        back to once it reappears. Retries back off (see _AUDIO_RETRY_*)
        while reopening doesn't help, e.g. for as long as the computer is
        asleep."""
        loop = asyncio.get_running_loop()
        streams = (self.mic, self.speaker)
        last_device_check = time.monotonic()
        failing = False        # an outage has been logged and not yet recovered
        retry_delay = 0.0
        next_retry = 0.0
        reopened_at: float | None = None
        while True:
            await asyncio.sleep(_AUDIO_WATCHDOG_INTERVAL_S)
            now = time.monotonic()
            if reopened_at is not None and all(s.last_callback > reopened_at for s in streams):
                # Audio has flowed since the last reopen - it worked.
                if failing:
                    log.info("audio is flowing again")
                failing = False
                retry_delay = 0.0
                reopened_at = None

            reason = None
            stalled = [s.kind for s in streams if s.stalled()]
            if stalled:
                if now < next_retry:
                    continue
                reason = f"{' and '.join(stalled)} stopped delivering audio"
            elif (any(s.on_fallback for s in streams)
                  and now - last_device_check >= _AUDIO_DEVICE_RECHECK_S):
                last_device_check = now
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
            elif retry_delay == _AUDIO_RETRY_MIN_S:
                log.warning("audio: still no audio after reopening - retrying less often (up to every %.0fs) "
                            "until it returns; expected while the computer is asleep", _AUDIO_RETRY_MAX_S)
            else:
                log.debug("audio: %s - retrying (next wait up to %.0fs)", reason, retry_delay)
            try:
                await loop.run_in_executor(None, self._locked_restart, streams)
            except Exception as exc:
                if not failing:
                    log.error("could not reopen audio devices (%s) - will keep retrying", exc)
            reopened_at = time.monotonic()
            if stalled:
                failing = True
                retry_delay = min(max(retry_delay * 2, _AUDIO_RETRY_MIN_S), _AUDIO_RETRY_MAX_S)
                next_retry = reopened_at + retry_delay

    # --- transmit path ----------------------------------------------------

    async def _tx_loop(self) -> None:
        loop = asyncio.get_running_loop()
        while True:
            frame = await loop.run_in_executor(None, self.mic.read_frame)
            if not self._ptt_active:
                continue
            if self._awaiting_permit:
                # First audio since the press: the mic (and a Bluetooth
                # headset's audio link) is live - tell the talker.
                self._awaiting_permit = False
                self._play_local(self._permit_tone_frames)
                self._permit_mute_until = time.monotonic() + _PERMIT_TONE_MS / 1000 + 0.15
            if time.monotonic() < self._permit_mute_until:
                frame = b"\x00" * len(frame)
            elif self.enable_echo_guard and self._own_speaker_active():
                frame = b"\x00" * len(frame)
            else:
                if self.highpass_enabled:
                    frame = self.highpass.process(frame)
                if self.squelch_enabled:
                    frame = self.squelch.process(frame)
                if self.agc_enabled:
                    frame = self.agc.process(frame)
                frame = self.noise_suppressor.process(frame)
            # Cap leaves room for pad_audio's 1-byte marker.
            opus_bytes = self.codec.encode(frame, max_bytes=self.audio_pad - 1 if self.audio_pad else None)
            payload = pad_audio(opus_bytes, self.audio_pad)
            cipher = self.ciphers[self.active_channel.id]
            targets, group = self._channel_delivery(self.active_channel.id)
            self.transport.send_encrypted(cipher, self.active_channel.id, TYPE_AUDIO, payload,
                                          targets=targets, multicast_group=group)

    async def _channel_announce_loop(self) -> None:
        """Periodically floods a tiny empty-payload beacon per held channel so
        peers can authenticate our membership and start targeting us
        directly, instead of every AUDIO/STOP packet having to flood the
        whole mesh forever."""
        await asyncio.sleep(random.uniform(0, min(2.0, self.config.channel_announce_interval_s)))
        while True:
            self.transport.prune_stale(self._peer_ttl_s)
            if self.enable_relay:
                self._update_groups()  # drop groups for channels whose routes expired
            for channel_id in list(self.ciphers):
                self._send_announce(channel_id)
            await asyncio.sleep(self.config.channel_announce_interval_s * random.uniform(0.8, 1.2))

    def _send_announce(self, channel_id: int) -> None:
        """Payload: our per-run token (clash detection) + a flags byte, bit 0
        = we're listening to this channel. With multicast on, one copy goes
        to the announce group for everyone on this site, and unicast only
        to peers not heard via multicast."""
        cipher = self.ciphers.get(channel_id)
        if cipher is None:
            return
        listening = channel_id in self.listening_channel_ids()
        payload = self._instance_token + bytes([1 if listening else 0])
        if self.transport.multicast_enabled:
            local = self._multicast_local_addresses()
            targets = [p for p in self.transport.peers if (p.host, p.port) not in local]
            self.transport.send_encrypted(cipher, channel_id, TYPE_CHANNEL_ANNOUNCE, payload,
                                          targets=targets, multicast_group=self._group(0))
        else:
            self.transport.send_encrypted(cipher, channel_id, TYPE_CHANNEL_ANNOUNCE, payload)

    def multicast_status(self) -> dict:
        if self.active_channel is None:
            listeners, group = [], None
        else:
            listeners = self._channel_listeners(self.active_channel.id) or []
            _, group = self._channel_delivery(self.active_channel.id)
        return {
            "enabled": self.transport.multicast_enabled,
            "site_listeners": sum(1 for sid, _ in listeners if self._is_multicast_local(sid)),
            "off_site_listeners": sum(1 for sid, _ in listeners if not self._is_multicast_local(sid)),
            "active_channel_uses_multicast": group is not None,
            "min_listeners": self.multicast_min_listeners,
        }

    def _is_multicast_local(self, sender_id: int) -> bool:
        entry = self._mcast_heard.get(sender_id)
        return entry is not None and time.monotonic() - entry[1] < self._membership_ttl_s

    def _multicast_local_addresses(self) -> set[tuple[str, int]]:
        cutoff = time.monotonic() - self._membership_ttl_s
        return {addr for addr, seen in self._mcast_heard.values() if seen >= cutoff}

    def _channel_delivery(self, channel_id: int) -> tuple[list[Peer] | None, str | None]:
        """(unicast targets, multicast group or None) for sending on a
        channel. With enough listeners on this site (heard via multicast),
        one multicast reaches them all and only off-site listeners get
        unicast; otherwise every listener gets unicast. None targets = no
        members known yet, flood every peer (bootstrap)."""
        listeners = self._channel_listeners(channel_id)
        if listeners is None:
            return None, None
        redirects = self._bridge_redirects()
        if redirects:
            listeners = [(sid, redirects.get((peer.host, peer.port), peer)) for sid, peer in listeners]
        if self.transport.multicast_enabled:
            local = [sid for sid, _ in listeners if self._is_multicast_local(sid)]
            if len(local) >= self.multicast_min_listeners:
                on_site = self._multicast_local_addresses()
                remote = [peer for sid, peer in listeners
                          if not self._is_multicast_local(sid) and (peer.host, peer.port) not in on_site]
                return _unique(remote), self._group(channel_id)
        # Members behind the same bridge share its address: one copy each.
        return _unique([peer for _, peer in listeners]), None

    def _channel_listeners(self, channel_id: int) -> list[tuple[int, Peer]] | None:
        members = self._channel_members.get(channel_id)
        if not members:
            return None
        cutoff = time.monotonic() - self._membership_ttl_s
        fresh = [(sid, peer, listening) for sid, (peer, seen, listening) in members.items() if seen >= cutoff]
        if not fresh:
            return None
        return [(sid, peer) for sid, peer, listening in fresh if listening]

    def _channel_target_peers(self, channel_id: int) -> list[Peer] | None:
        """Listening members of a channel, or None to fall back to flooding
        every known peer (bootstrap case - before any announce has
        propagated, or if a channel currently has no confirmed members)."""
        listeners = self._channel_listeners(channel_id)
        if listeners is None:
            return None
        return [peer for _, peer in listeners] or None

    # --- receive path -----------------------------------------------------

    _RELAY_SEEN_MAX = 4096

    def _relay_packet(self, header, ciphertext: bytes, addr, via_multicast: bool = False) -> None:
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
        source = (addr[0], addr[1])
        known_source = self.transport.is_known_address(*source)
        if via_multicast and known_source:
            self._relay_mcast_heard[source] = time.monotonic()
        if header.ptype == TYPE_CHANNEL_ANNOUNCE and known_source:
            # Recorded before the dedup check below: the same announce
            # arriving via a second neighbour is a second route.
            new_channel = not self._relay_routes.get(header.channel_id)
            self._relay_routes.setdefault(header.channel_id, {})[source] = time.monotonic()
            if new_channel:
                self._update_groups()  # start carrying this channel's site multicast

        dedup_key = (header.sender_id, header.ptype)
        last_fwd = self._relay_seen.get(dedup_key)
        if last_fwd is not None and header.seq <= last_fwd:
            return  # already forwarded this or an older/duplicate packet
        if len(self._relay_seen) > self._RELAY_SEEN_MAX:
            self._relay_seen.clear()  # safety valve - sender_id is spoofable, don't grow unbounded
        self._relay_seen[dedup_key] = header.seq

        packet = encode_packet(header, ciphertext)
        if self.is_bridge and header.ptype == TYPE_CHANNEL_ANNOUNCE and known_source \
                and self._is_site_local(source[0]) and source not in self._site_bridge_addresses():
            self._cache_announce(header, packet, source, via_multicast)
        self._forward(header, packet, source, via_multicast)

    def _forward(self, header, packet: bytes, source, via_multicast: bool) -> None:
        """Send a relayed packet on to every neighbour that leads to the
        channel's members, except those that already have it:
          - the neighbour it came from,
          - neighbours the sender reaches directly - both have listed each
            other in their peer exchange, so the link works both ways and the
            sender sends to them itself,
          - for a packet that arrived by multicast, the neighbours that got
            the same multicast.
        This needs no configuration: bridges that all reach each other over
        the VPN never pass packets between each other (one copy per site),
        while a hub between VPN clients that can't reach each other, or
        between site nodes that can't (e.g. Wi-Fi client isolation), relays
        between them. Where enough targets get this site's multicast, they
        get one multicast instead of one copy each."""
        if header.ptype == TYPE_CHANNEL_ANNOUNCE:
            # Announces still go everywhere - they're how routes and
            # channel membership are learned in the first place.
            targets = [p for p in self.transport.peers if (p.host, p.port) != source]
        else:
            targets = self._relay_targets(header.channel_id, source)
        reached = self._reached_by(source)
        targets = [p for p in _unique(targets)
                   if not ((p.host, p.port) in reached and source in self._reached_by((p.host, p.port)))]
        if self.is_bridge:
            live = self._site_bridge_peers()
            others = self._other_bridge_hosts(live)
            targets = [p for p in targets if p.host not in others]
            # Another bridge on this site that also got this packet and
            # outranks us sends it to the neighbours it reaches itself.
            better = self._better_bridges_with(header, source, via_multicast, live)
            if better:
                targets = [p for p in targets
                           if not any((p.host, p.port) in self._reached_by(addr) for addr in better)]
        if via_multicast:
            on_multicast = self._site_multicast_addresses()
            targets = [p for p in targets if (p.host, p.port) not in on_multicast]
        elif self.transport.multicast_enabled:
            on_multicast = self._site_multicast_addresses()
            if sum((p.host, p.port) in on_multicast for p in targets) >= self.multicast_min_listeners:
                # Multicast reaches only the nodes that use it; the rest
                # (multicast off, or on a routed subnet) still get unicast.
                group = self._group(0 if header.ptype == TYPE_CHANNEL_ANNOUNCE else header.channel_id)
                self.transport.send_multicast(packet, group)
                targets = [p for p in targets if (p.host, p.port) not in on_multicast]
        if targets:
            self.transport.forward(packet, targets)

    def _site_multicast_addresses(self) -> set[tuple[str, int]]:
        """Addresses known to get this site's multicast: senders heard via
        multicast, authenticated or (for channels we have no key for) not."""
        cutoff = time.monotonic() - self._membership_ttl_s
        for address in [a for a, seen in self._relay_mcast_heard.items() if seen < cutoff]:
            del self._relay_mcast_heard[address]
        return self._multicast_local_addresses() | set(self._relay_mcast_heard)

    def _note_gossip(self, header, peers: list[Peer], addr) -> None:
        """PEX callback: `addr` reaches `peers` directly (PEX only lists
        peers heard from first-hand). Replay-checked, since a stale list
        would make relays skip a neighbour the sender no longer reaches."""
        if not self._replay.is_fresh(header.sender_id, header.seq):
            return
        self._replay.mark(header.sender_id, header.seq)
        self._peer_reach[(addr[0], addr[1])] = (frozenset((p.host, p.port) for p in peers), time.monotonic())

    def _reached_by(self, source) -> frozenset:
        """Addresses `source` recently said it reaches directly (empty if it
        hasn't gossiped to us - then we forward to everyone, which can only
        cost duplicates, never lose audio)."""
        entry = self._peer_reach.get(source)
        if entry is None:
            return frozenset()
        if time.monotonic() - entry[1] > self._peer_ttl_s:
            del self._peer_reach[source]
            return frozenset()
        return entry[0]

    # --- redundant bridges --------------------------------------------------
    #
    # A site can have several bridges, and they may lead to different
    # networks (one on the VPN, another on Ethernet to a neighbouring
    # group...). So there's no single "active" bridge: for each neighbour,
    # the highest-ranked bridge (bridge_priority, then lowest sender_id) that
    # reaches it is the one that sends to it. Bridges learn about each other
    # from bridge hellos, and what each reaches from its peer exchange.

    _ANNOUNCE_CACHE_MAX = 1024
    _MAX_COVERED = 32

    def _rank(self, sender_id: int, priority: int) -> tuple[int, int]:
        return (priority, -sender_id)

    def _my_rank(self) -> tuple[int, int]:
        return self._rank(self.config.sender_id, self.config.bridge_priority)

    def _cache_announce(self, header, packet: bytes, source, via_multicast: bool) -> None:
        """Latest announce per site member, so a bridge that takes over a
        neighbour can pass them on straight away instead of waiting up to a
        full announce interval for routes through it to be learned."""
        if len(self._announce_cache) >= self._ANNOUNCE_CACHE_MAX:
            self._announce_cache.clear()  # sender_id is unauthenticated - bound it
        self._announce_cache[(header.sender_id, header.channel_id)] = (
            header, packet, source, via_multicast, time.monotonic())

    def _site_bridge_peers(self) -> dict[int, "_BridgeInfo"]:
        """Other bridges heard from recently on this site, by sender_id."""
        cutoff = time.monotonic() - 3 * self.config.bridge_hello_interval_s
        return {sid: b for sid, b in self._bridge_hellos.items()
                if b.seen >= cutoff and self._is_site_local(b.addr[0])}

    def _other_bridge_hosts(self, live: dict) -> set[str]:
        """Off-site addresses (e.g. VPN) of this site's other bridges. Never
        worth sending to: they're on this site and get its traffic here."""
        return {h for b in live.values() for h in b.hosts if not self._is_site_local(h)}

    def _better_bridges_with(self, header, source, via_multicast: bool, live: dict) -> list[tuple[str, int]]:
        """Addresses of this site's bridges that outrank us and certainly got
        this packet too: the one it came from, those heard on the multicast
        it arrived by, or - for an announce straight from its sender, which
        sends announces to every peer - those the sender reaches. (Unicast
        audio goes only where the sender chose, so every bridge forwards
        the audio it gets.)"""
        me = self._my_rank()
        better = {b.addr for sid, b in live.items() if self._rank(sid, b.priority) > me}
        if not better:
            return []
        got = {source} & better
        if via_multicast:
            got |= better & self._site_multicast_addresses()
        elif header.ptype == TYPE_CHANNEL_ANNOUNCE and self.transport.sender_address(header.sender_id) == source:
            got |= better & self._reached_by(source)
        return list(got)

    def _designated_for(self, address, live: dict) -> bool:
        """Whether we're the top-ranked bridge here reaching `address`."""
        me = self._my_rank()
        return not any(self._rank(sid, b.priority) > me and address in self._reached_by(b.addr)
                       for sid, b in live.items())

    def bridge_role(self) -> str | None:
        """For status: "active", "standby", or "active for k of n uplink
        peers" when other bridges on the site cover the rest."""
        if not self.is_bridge:
            return None
        live = self._site_bridge_peers()
        others = self._other_bridge_hosts(live)
        uplink = [(p.host, p.port) for p in self.transport.live_peers
                  if not self._is_site_local(p.host) and p.host not in others]
        if not live or not uplink:
            return "active"
        mine = sum(self._designated_for(a, live) for a in uplink)
        if mine == len(uplink):
            return "active"
        if mine == 0:
            return "standby"
        return f"active for {mine} of {len(uplink)} uplink peers"

    def _covered_bridges(self, live: dict) -> list[int]:
        """Silent bridges we now stand in for: we're the top-ranked live
        bridge for at least one neighbour they used to reach."""
        cutoff = time.monotonic() - 3 * self.config.bridge_hello_interval_s
        forget = time.monotonic() - self._membership_ttl_s
        mine = {(p.host, p.port) for p in self.transport.live_peers}
        covered = []
        for sid, b in self._bridge_hellos.items():
            if b.seen >= cutoff or b.seen < forget or not self._is_site_local(b.addr[0]):
                continue
            entry = self._peer_reach.get(b.addr)
            if entry and any(a in mine and not self._is_site_local(a[0]) and self._designated_for(a, live)
                             for a in entry[0]):
                covered.append(sid)
        return covered[:self._MAX_COVERED]

    def _check_bridges(self) -> None:
        """Hello timer: when a bridge on the site goes silent (or a new one
        appears), neighbours change hands - pass recent announces on so
        routes through their new bridge are learned straight away."""
        live = set(self._site_bridge_peers())
        if live == self._live_bridges:
            return
        lost = self._live_bridges - live
        self._live_bridges = live
        if self.is_bridge:
            if lost:
                log.info("bridge(s) %s on this site went silent - taking over what they reached",
                         ", ".join(str(s) for s in sorted(lost)))
            self._replay_cached_announces()

    def _replay_cached_announces(self) -> None:
        cutoff = time.monotonic() - self._membership_ttl_s
        for key, (header, packet, source, via_multicast, seen) in list(self._announce_cache.items()):
            if seen < cutoff:
                del self._announce_cache[key]
            else:
                self._forward(header, packet, source, via_multicast)

    _MAX_HELLO_ADDRESSES = 16

    def _own_addresses(self) -> list[str]:
        """This node's IPv4 addresses, so other bridges on the site can tell
        its VPN/uplink address from a separate node's."""
        try:
            import ifaddr
            return [ip.ip for a in ifaddr.get_adapters() for ip in a.ips
                    if ip.is_IPv4 and not ip.ip.startswith("127.")][:self._MAX_HELLO_ADDRESSES]
        except OSError:
            return []

    def _send_bridge_hello(self) -> None:
        """Tell this site's other bridges (and its nodes) that we're a bridge:
        our priority, our addresses, and which silent bridges we stand in
        for. Multicast where possible; otherwise unicast to the site, every
        time once another bridge is known (failover then reaches nodes
        quickly), or now and then until one is (so a lone bridge costs next
        to nothing).

        Payload: priority (1 byte), address count (1 byte), IPv4 addresses
        (4 bytes each), then sender_ids of covered bridges (4 bytes each)."""
        if not self.is_bridge:
            return
        self._hello_count += 1
        others = self._site_bridge_peers()
        covered = self._covered_bridges(others)
        hosts = self._own_addresses()
        payload = (bytes([self.config.bridge_priority, len(hosts)])
                   + b"".join(ipaddress.IPv4Address(h).packed for h in hosts)
                   + b"".join(sid.to_bytes(4, "big") for sid in covered))
        site = [p for p in self.transport.peers if self._is_site_local(p.host)]
        if self.transport.multicast_enabled:
            on_multicast = self._site_multicast_addresses()
            targets = [p for p in site if (p.host, p.port) not in on_multicast]
            group = self._group(0)
        else:
            targets, group = site, None
        if not others and not covered and self._hello_count % 15 != 1:
            targets = []
        if targets or group:
            self.transport.send_encrypted(self._mesh_cipher, 0, TYPE_BRIDGE_HELLO, payload,
                                          targets=targets, multicast_group=group)

    def _on_bridge_hello(self, header, ciphertext: bytes, addr) -> None:
        if not self._is_fresh(header, addr):
            return
        try:
            payload = self._mesh_cipher.decrypt(header, ciphertext)
        except ValueError:
            log.debug("dropping unauthenticated bridge hello from %s", addr)
            return
        self._replay.mark(header.sender_id, header.seq)
        if len(payload) < 2 or len(payload) < 2 + 4 * payload[1]:
            return
        n = payload[1]
        hosts = frozenset(str(ipaddress.IPv4Address(payload[2 + 4 * i:6 + 4 * i])) for i in range(n)) | {addr[0]}
        rest = payload[2 + 4 * n:]
        covers = frozenset(int.from_bytes(rest[i:i + 4], "big") for i in range(0, len(rest) - 3, 4))
        self._bridge_hellos[header.sender_id] = _BridgeInfo(
            (addr[0], addr[1]), payload[0], hosts, covers, time.monotonic())
        # Authenticated, so it's a peer like any other - this is also how
        # site nodes find a newly added bridge.
        self.transport.confirm_peer(header.sender_id, Peer(host=addr[0], port=addr[1]))
        self._check_bridges()

    def _bridge_redirects(self) -> dict[tuple[str, int], Peer]:
        """For site nodes: address of a bridge that went silent -> the
        top-ranked live bridge standing in for it, so traffic for other
        networks fails over without waiting for new announces."""
        if not self._bridge_hellos:
            return {}
        cutoff = time.monotonic() - 3 * self.config.bridge_hello_interval_s
        live = [(self._rank(sid, b.priority), b.addr, b.covers)
                for sid, b in self._bridge_hellos.items() if b.seen >= cutoff]
        redirects = {}
        for sid, b in self._bridge_hellos.items():
            if b.seen >= cutoff:
                continue
            standing_in = [(rank, a) for rank, a, covers in live if sid in covers]
            if standing_in:
                best = max(standing_in)[1]
                redirects[b.addr] = Peer(host=best[0], port=best[1])
        return redirects

    async def _bridge_hello_loop(self) -> None:
        while True:
            self._check_bridges()
            self._send_bridge_hello()
            await asyncio.sleep(self.config.bridge_hello_interval_s * random.uniform(0.9, 1.1))

    def _is_fresh(self, header, addr) -> bool:
        """Replay check (see replay.py) - False means drop the packet.
        Recent duplicates are routine on a multi-path mesh and only logged
        at debug; anything older than the window is a likely replay (or a
        sender whose seq went backwards, e.g. a lost .seq file) and gets a
        rate-limited warning."""
        if self._replay.is_fresh(header.sender_id, header.seq):
            return True
        if self._replay.is_too_old(header.sender_id, header.seq):
            now = time.monotonic()
            if now - self._replay_warned_at.get(header.sender_id, -1e9) >= 60.0:
                self._replay_warned_at[header.sender_id] = now
                log.warning("dropping old (likely replayed) packets claiming to be sender %d, from %s:%d",
                            header.sender_id, addr[0], addr[1])
        else:
            log.debug("dropping duplicate packet from sender %d (seq %d)", header.sender_id, header.seq)
        return False

    def _note_direct_duplicate(self, header, addr) -> None:
        """A relayed copy of an announce can beat the direct one, which is
        then dropped as a duplicate - still evidence that the member is
        reachable at the address we already trust for it, so the member
        isn't switched over to the relay's address. (Only ever keeps the
        address we already have; it never sets a new one.)"""
        if self._replay.is_too_old(header.sender_id, header.seq):
            return
        key = (header.channel_id, header.sender_id)
        existing = self._channel_members.get(header.channel_id, {}).get(header.sender_id)
        if key in self._member_direct_seen and existing is not None and \
                (existing[0].host, existing[0].port) == (addr[0], addr[1]):
            self._member_direct_seen[key] = time.monotonic()

    def _check_sender_id_clash(self, header, ciphertext: bytes, addr) -> None:
        """Called for packets carrying our own sender_id. Usually that's our
        own traffic looped back through a relay - but an announce whose
        marker isn't ours means a second node shares our sender_id (cloned
        identity file, or the same explicit sender_id in two configs). Two
        nodes with one sender_id on a shared channel key reuse encryption
        nonces, which leaks audio and allows forgery, so this is an error."""
        if header.ptype != TYPE_CHANNEL_ANNOUNCE:
            return
        cipher = self.ciphers.get(header.channel_id)
        if cipher is None:
            return
        try:
            token = cipher.decrypt(header, ciphertext)
        except ValueError:
            return
        if token[:8] == self._instance_token:
            return
        now = time.monotonic()
        if now - self._clash_warned_at < 60.0:
            return
        self._clash_warned_at = now
        log.error(
            "another node at %s:%d is using this node's sender_id %d - their traffic can compromise "
            "each other's encryption. If sender_id is set in the config, give each node its own (or "
            "remove it to auto-generate); otherwise delete the copied %s.identity file on one of them "
            "and restart it.", addr[0], addr[1], self.config.sender_id,
            self.config_path or "<config>")

    def _relay_targets(self, channel_id: int, source: tuple[str, int]) -> list[Peer]:
        """Where a relay forwards AUDIO/START/STOP for `channel_id`: only
        neighbours that channel's announces have recently arrived from,
        i.e. those that lead to at least one member - possibly via further
        relays. A relay can't decrypt channels it holds no key for, so this
        is learned from cleartext announce headers rather than
        authenticated membership; restricting it to already-known peer
        addresses (see _relay_packet) stops spoofed announces from turning
        the relay into a flood amplifier aimed at arbitrary addresses.
        Falls back to every known peer until a route is known, same as
        _channel_target_peers' bootstrap flood."""
        routes = self._relay_routes.get(channel_id, {})
        cutoff = time.monotonic() - self._membership_ttl_s
        for address in [a for a, seen in routes.items() if seen < cutoff]:
            del routes[address]
        known = {(p.host, p.port): p for p in self.transport.peers}
        targets = [known[a] for a in routes if a != source and a in known]
        if routes:
            return targets  # may be empty: the only route leads back to the sender
        return [p for a, p in known.items() if a != source]

    def _note_multicast(self, header, addr) -> None:
        self._mcast_heard[header.sender_id] = ((addr[0], addr[1]), time.monotonic())

    def _on_packet(self, header, ciphertext: bytes, addr, via_multicast: bool = False) -> None:
        if header.sender_id == self.config.sender_id:
            self._check_sender_id_clash(header, ciphertext, addr)
            # Our own traffic looped back to us - e.g. a multi-hop relay mesh
            # can bounce a packet through a downstream relay and back to its
            # original sender if that relay also knows us as a peer. Without
            # this guard we'd decrypt and play our own voice back, which
            # sounds like a (delayed) audio echo whenever relay mode is on.
            return

        if self.enable_relay and header.ptype in (TYPE_AUDIO, TYPE_START, TYPE_STOP, TYPE_CHANNEL_ANNOUNCE):
            self._relay_packet(header, ciphertext, addr, via_multicast)

        if header.ptype == TYPE_BRIDGE_HELLO:
            self._on_bridge_hello(header, ciphertext, addr)
            return

        if header.ptype == TYPE_PEX:
            if self.pex:
                self.pex.handle_incoming(header, ciphertext, addr)
            return

        if header.ptype == TYPE_CHANNEL_ANNOUNCE:
            cipher = self.ciphers.get(header.channel_id)
            if cipher is None:
                return  # not a channel we hold the key for
            if not self._is_fresh(header, addr):
                self._note_direct_duplicate(header, addr)
                return
            try:
                payload = cipher.decrypt(header, ciphertext)
            except ValueError:
                log.debug("dropping unauthenticated channel announce from %s", addr)
                return
            self._replay.mark(header.sender_id, header.seq)
            # Older nodes send no flags byte - assume they're listening.
            listening = bool(payload[8] & 1) if len(payload) > 8 else True
            peer = Peer(host=addr[0], port=addr[1])
            members = self._channel_members.setdefault(header.channel_id, {})
            now = time.monotonic()
            if self.transport.confirm_peer(header.sender_id, peer):
                if via_multicast:
                    self._note_multicast(header, addr)
                members[header.sender_id] = (peer, now, listening)
                self._member_direct_seen[(header.channel_id, header.sender_id)] = now
            else:
                # Address rejected (e.g. relayed through another node's
                # address, not this sender's own) - keep whatever address we
                # already trusted for this sender, if any, just refresh
                # liveness so a relayed-only member doesn't go stale/get
                # dropped from _channel_target_peers.
                # Relayed: reach this member through the relay it came from,
                # unless we've recently heard from it directly.
                direct = self._member_direct_seen.get((header.channel_id, header.sender_id), -1e9)
                existing = members.get(header.sender_id)
                if existing is not None and now - direct < self._membership_ttl_s:
                    members[header.sender_id] = (existing[0], now, listening)
                else:
                    members[header.sender_id] = (peer, now, listening)
            return

        if header.channel_id not in self.listening_channel_ids():
            return  # only play the active channel and any monitored ones

        cipher = self.ciphers.get(header.channel_id)
        if cipher is None:
            return  # we don't hold this channel's key - can't be a member
        if header.ptype not in (TYPE_START, TYPE_STOP, TYPE_AUDIO):
            return
        if not self._is_fresh(header, addr):
            return

        if header.ptype == TYPE_START:
            try:
                alias_bytes = cipher.decrypt(header, ciphertext)
            except ValueError:
                log.debug("dropping unauthenticated start from %s", addr)
                return
            self._replay.mark(header.sender_id, header.seq)
            alias = alias_bytes.decode("utf-8", errors="replace") or None
            if self.transport.confirm_peer(header.sender_id, Peer(host=addr[0], port=addr[1])) and via_multicast:
                self._note_multicast(header, addr)
            self._channel_last_audio[header.channel_id] = time.monotonic()
            self._channel_speaking.setdefault(header.channel_id, {})[header.sender_id] = (alias, time.monotonic())
            return
        if header.ptype == TYPE_STOP:
            try:
                cipher.decrypt(header, ciphertext)
            except ValueError:
                log.debug("dropping unauthenticated stop from %s", addr)
                return
            self._replay.mark(header.sender_id, header.seq)
            self.mixer.stop(header.channel_id, header.sender_id)
            if self.transport.confirm_peer(header.sender_id, Peer(host=addr[0], port=addr[1])) and via_multicast:
                self._note_multicast(header, addr)
            self._channel_last_audio.pop(header.channel_id, None)
            self._channel_speaking.get(header.channel_id, {}).pop(header.sender_id, None)
            return
        try:
            opus_bytes = unpad_audio(cipher.decrypt(header, ciphertext))
        except ValueError:
            log.debug("dropping packet with bad auth tag or padding from %s", addr)
            return
        self._replay.mark(header.sender_id, header.seq)

        # Only an authenticated sender teaches us its address - raw UDP
        # source addresses are trivially spoofable and must not be trusted.
        # A relayed copy (from a bridge's address) isn't confirmed, and so
        # doesn't make the original sender look like it's on this site.
        if self.transport.confirm_peer(header.sender_id, Peer(host=addr[0], port=addr[1])) and via_multicast:
            self._note_multicast(header, addr)
        self._channel_last_audio[header.channel_id] = time.monotonic()
        # Keep "who's speaking" fresh even if this sender's TYPE_START was
        # lost - preserve whatever alias we already know (if any) rather
        # than dropping it back to unknown.
        speaking = self._channel_speaking.setdefault(header.channel_id, {})
        prev_alias = speaking.get(header.sender_id, (None, 0.0))[0]
        speaking[header.sender_id] = (prev_alias, time.monotonic())

        # Ordering, loss recovery and timing are the jitter buffer's job.
        self.mixer.push(header.channel_id, header.sender_id, header.seq, opus_bytes)
        self._demand_audio("output")
