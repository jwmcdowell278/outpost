"""Channel and peer configuration loading (YAML)."""
from __future__ import annotations
import base64
import os
import secrets
from dataclasses import dataclass, field
import yaml

from .crypto import KEY_SIZE

DEFAULT_LISTEN_PORT = 5555


@dataclass(frozen=True)
class Channel:
    id: int
    name: str
    key: bytes


@dataclass(frozen=True)
class Peer:
    host: str
    port: int


@dataclass(frozen=True)
class OutpostConfig:
    sender_id: int
    listen_port: int
    mesh_key: bytes
    channels: dict[int, Channel] = field(default_factory=dict)
    peers: list[Peer] = field(default_factory=list)

    # Network-profile knobs - tune down for constrained links (mesh radio),
    # leave at defaults for LAN/VPN. See config/outpost.example.yaml.
    bitrate: int = 16000
    frame_ms: int = 20
    max_packet_bytes: int = 1200
    inband_fec: bool = True

    # Discovery - mDNS only works where multicast is reachable (typically
    # plain LAN, not most VPNs or mesh radio links); PEX gossip works over
    # any unicast-reachable network, including VPN mesh and multi-hop radio
    # mesh, as long as it is seeded with at least one reachable peer.
    enable_mdns: bool = True
    enable_pex: bool = True
    pex_interval_s: float = 30.0

    # Bounded random subset of known peers to gossip to each round, instead
    # of every known peer - keeps PEX control-plane traffic per node roughly
    # constant (not growing with total mesh size) while still reaching
    # everyone within a few rounds via hop-by-hop propagation. Matters once a
    # mesh has many hundreds-to-thousands of nodes.
    pex_fanout: int = 8

    # Ceiling on how many peer addresses a single node will track at once
    # (static + mDNS + PEX-gossiped). The default suits small/medium
    # deployments; raise it for a mesh with many hundreds/thousands of
    # devices so legitimate peers/channel members aren't silently dropped.
    max_peers: int = 128

    # How often to flood a small per-channel presence beacon so senders can
    # learn which known peers actually hold a given channel's key and target
    # AUDIO/STOP sends only at them instead of every known peer - this is
    # what keeps per-transmission bandwidth from scaling with total mesh
    # size rather than just channel membership.
    channel_announce_interval_s: float = 60.0

    # High-pass filter - strips DC offset and sub-audible rumble/handling
    # noise below the cutoff before AGC/squelch see that energy. Pure DSP,
    # on by default (cheap, rarely harms voice quality).
    enable_highpass: bool = True
    highpass_cutoff_hz: float = 100.0

    # Automatic gain control - normalizes captured mic volume toward a
    # target RMS level before encoding, so nodes with different mics/gain
    # settings don't need per-device manual volume tuning. On by default
    # since it's pure DSP (no extra dependency, no model to vet).
    enable_agc: bool = True
    agc_target_rms: float = 4000.0

    # Noise gate (squelch) - mutes frames whose RMS stays below
    # squelch_threshold_rms for longer than squelch_hold_ms, so continuous
    # background noise between words isn't transmitted. Off by default -
    # needs the threshold calibrated to your mic/environment to avoid
    # clipping quiet speech; see README's "Improving audio in noisy
    # environments" section.
    enable_squelch: bool = False
    squelch_threshold_rms: float = 300.0
    squelch_hold_ms: float = 300.0

    # Lightweight local web UI (see outpost/webui.py) for viewing status and
    # switching channels/PTT without a hardware PTT source. Off by default -
    # requires the 'web' extra (aiohttp). Binds to loopback only by default;
    # set web_ui_token before binding anywhere else reachable by others, as
    # the control endpoints are otherwise unauthenticated.
    enable_web_ui: bool = False
    web_ui_host: str = "127.0.0.1"
    web_ui_port: int = 8765
    web_ui_token: str | None = None

    # Relay/bridge mode: blind-floods AUDIO/STOP/CHANNEL_ANNOUNCE packets to
    # every other known peer, keyed only on the cleartext header (no channel
    # key needed) - lets a multi-homed node (e.g. local batman-adv mesh +
    # WireGuard uplink to HQ/other relays) bridge two otherwise-unreachable
    # peer groups without ever being able to decrypt their traffic. See
    # outpost/client.py's `_relay_packet` and the README's "Relay/bridge
    # mode" section. Off by default - most nodes are plain clients.
    enable_relay: bool = False

    def channel_by_name(self, name: str) -> Channel:
        for ch in self.channels.values():
            if ch.name == name:
                return ch
        raise KeyError(f"unknown channel {name!r}")


def _validate_bool(v):
    if isinstance(v, bool):
        return v
    raise ValueError("must be true or false")


def _validate_positive_int(v):
    v = int(v)
    if v <= 0:
        raise ValueError("must be a positive integer")
    return v


def _validate_positive_float(v):
    v = float(v)
    if v <= 0:
        raise ValueError("must be a positive number")
    return v


def _validate_port(v):
    v = int(v)
    if not (1 <= v <= 65535):
        raise ValueError("must be between 1 and 65535")
    return v


def _validate_nonnegative_float(v):
    v = float(v)
    if v < 0:
        raise ValueError("must not be negative")
    return v


# Fields the web UI's settings editor is allowed to change, each mapped to
# (validator, applies_live). applies_live=True means OutpostClient can adopt
# the new value on the running process immediately; everything else only
# takes effect after persisting to the YAML file and restarting the client.
# Deliberately excludes sender_id/mesh_key/channels/peers (identity/secrets,
# need their own dedicated flows, not blind value edits) and web_ui_host/
# web_ui_port/web_ui_token (editing the web UI's own bind address/auth via
# the web UI itself risks a self-lockout or accidental exposure).
EDITABLE_FIELDS: dict[str, tuple] = {
    "listen_port": (_validate_port, False),
    "bitrate": (_validate_positive_int, False),
    "frame_ms": (_validate_positive_int, False),
    "max_packet_bytes": (_validate_positive_int, False),
    "inband_fec": (_validate_bool, False),
    "enable_mdns": (_validate_bool, False),
    "enable_pex": (_validate_bool, False),
    "pex_interval_s": (_validate_positive_float, False),
    "pex_fanout": (_validate_positive_int, False),
    "max_peers": (_validate_positive_int, False),
    "channel_announce_interval_s": (_validate_positive_float, False),
    "enable_agc": (_validate_bool, True),
    "agc_target_rms": (_validate_positive_float, True),
    "enable_relay": (_validate_bool, True),
    "enable_highpass": (_validate_bool, True),
    "highpass_cutoff_hz": (_validate_positive_float, True),
    "enable_squelch": (_validate_bool, True),
    "squelch_threshold_rms": (_validate_nonnegative_float, True),
    "squelch_hold_ms": (_validate_nonnegative_float, True),
}


def editable_config_values(config: OutpostConfig) -> dict:
    """Current values of every web-UI-editable field, for display - never
    includes sender_id/mesh_key/channels/peers/web_ui_token."""
    return {name: getattr(config, name) for name in EDITABLE_FIELDS}


def save_config_fields(path: str, updates: dict) -> None:
    """Persist `updates` (field name -> already-validated value) into the
    YAML config file at `path`, leaving every other key (including
    mesh_key/channels/peers) untouched. Only ever called with keys already
    checked against EDITABLE_FIELDS by the caller.

    Note: re-serializing with yaml.safe_dump does not preserve comments or
    formatting from the original file - subsequent hand-edits will lose any
    comments that were present before the first web UI save."""
    with open(path, "r") as f:
        raw = yaml.safe_load(f) or {}
    raw.update(updates)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        yaml.safe_dump(raw, f, sort_keys=False)
    os.replace(tmp_path, path)


def _resolve_sender_id(config_path: str, raw: dict) -> int:
    """Returns the configured sender_id, or auto-generates and persists one.

    This lets the exact same outpost.yaml (mesh_key, channels, network
    profile) be copied to every node unmodified - the one genuinely
    per-node value is picked automatically on first run and cached next to
    the config so it stays stable across restarts (sender_id feeds the
    encryption nonce, so it must not change while old sequence numbers are
    still in play - see crypto.py).
    """
    if "sender_id" in raw and raw["sender_id"] is not None:
        return raw["sender_id"]

    identity_path = config_path + ".identity"
    if os.path.exists(identity_path):
        with open(identity_path, "r") as f:
            return int(f.read().strip())

    sender_id = secrets.randbits(32)
    with open(identity_path, "w") as f:
        f.write(str(sender_id))
    return sender_id


def load_config(path: str) -> OutpostConfig:
    with open(path, "r") as f:
        raw = yaml.safe_load(f)

    channels: dict[int, Channel] = {}
    for entry in raw["channels"]:
        key = base64.b64decode(entry["key"])
        if len(key) != KEY_SIZE:
            raise ValueError(f"channel {entry['name']!r} key must decode to {KEY_SIZE} bytes")
        channels[entry["id"]] = Channel(id=entry["id"], name=entry["name"], key=key)

    peers = [Peer(host=p["host"], port=p["port"]) for p in raw.get("peers", [])]

    mesh_key = base64.b64decode(raw["mesh_key"])
    if len(mesh_key) != KEY_SIZE:
        raise ValueError(f"mesh_key must decode to {KEY_SIZE} bytes")

    return OutpostConfig(
        sender_id=_resolve_sender_id(path, raw),
        listen_port=raw.get("listen_port", DEFAULT_LISTEN_PORT),
        mesh_key=mesh_key,
        channels=channels,
        peers=peers,
        bitrate=raw.get("bitrate", 16000),
        frame_ms=raw.get("frame_ms", 20),
        max_packet_bytes=raw.get("max_packet_bytes", 1200),
        inband_fec=raw.get("inband_fec", True),
        enable_mdns=raw.get("enable_mdns", True),
        enable_pex=raw.get("enable_pex", True),
        pex_interval_s=raw.get("pex_interval_s", 30.0),
        pex_fanout=raw.get("pex_fanout", 8),
        max_peers=raw.get("max_peers", 128),
        channel_announce_interval_s=raw.get("channel_announce_interval_s", 60.0),
        enable_agc=raw.get("enable_agc", True),
        agc_target_rms=raw.get("agc_target_rms", 4000.0),
        enable_relay=raw.get("enable_relay", False),
        enable_web_ui=raw.get("enable_web_ui", False),
        web_ui_host=raw.get("web_ui_host", "127.0.0.1"),
        web_ui_port=raw.get("web_ui_port", 8765),
        web_ui_token=raw.get("web_ui_token"),
    )

