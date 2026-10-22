"""Channel and peer configuration loading (YAML)."""
from __future__ import annotations
import base64
import logging
import os
import secrets
from dataclasses import dataclass, field
import yaml

from .crypto import KEY_SIZE
from .identity import hardware_ids

log = logging.getLogger(__name__)

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

    # Optional human-readable name for this node, sent once per PTT press
    # (see protocol.TYPE_START) so other users see who's talking instead of
    # just a numeric sender_id. None/empty means no alias is sent. Not a
    # secret and not authenticated beyond the channel key itself (anyone
    # holding a channel's key could claim any alias on it) - purely a
    # display convenience, not an identity guarantee.
    sender_alias: str | None = None

    # Network-profile knobs - tune down for constrained links (mesh radio),
    # leave at defaults for LAN/VPN. See config/outpost.example.yaml.
    bitrate: int = 16000
    frame_ms: int = 20
    max_packet_bytes: int = 1200
    inband_fec: bool = True

    # Makes every AUDIO packet the same size, so packet lengths can't reveal
    # what's being said (Opus frame sizes otherwise follow the speech - see
    # protocol.pad_audio). Caps each frame at the configured bitrate and
    # pads up to it, so on-air usage is a steady `bitrate` while
    # transmitting. Turn off only on links where every byte counts and that
    # leak is acceptable. Receivers handle either setting.
    constant_size_audio: bool = True

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

    # Auto-discovery for subnets mDNS can't reach (e.g. a WireGuard tunnel -
    # point-to-point VPN links don't carry multicast) without hand-
    # maintaining a peers: entry per device: periodically re-reads this
    # node's own network interfaces (so a VPN/mesh interface that comes up
    # after startup is picked up without a restart) and unicast-probes
    # every other address on each interface's subnet via PEX - any other
    # Outpost node sharing mesh_key there is added as a peer automatically.
    # Off by default since it generates scan traffic; subnets larger than
    # subnet_scan_max_hosts are skipped entirely rather than partially
    # scanned (see outpost.subnet_scan).
    enable_subnet_scan: bool = False
    subnet_scan_interval_s: float = 60.0
    subnet_scan_max_hosts: int = 512

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

    # Soft half-duplex lockout: while enabled, a new local PTT press is
    # ignored if audio from another peer on the active channel was received
    # within the last tx_lockout_timeout_ms (cleared immediately once that
    # peer's STOP packet arrives). Best-effort only - there's no central
    # arbiter in a P2P mesh, so two peers keying up within one network
    # round-trip of each other can still both get through. Off by default,
    # since it changes behavior (a held PTT press can silently not
    # transmit) - see README's "Preventing people from talking over each
    # other" section.
    enable_tx_lockout: bool = False
    tx_lockout_timeout_ms: float = 500.0

    # Echo guard: while enabled, mutes captured mic audio (sends silence
    # rather than dropping the packet) for echo_guard_hold_ms after this
    # device's own speaker last played anything. Targets acoustic feedback
    # from two devices placed too close together - without this, one
    # device's speaker output can bleed into a nearby device's own mic and
    # get re-transmitted, which (unlike a one-off echo) can build into a
    # howling feedback loop if that re-transmission is in turn picked up
    # again. Off by default, since - like tx_lockout - it changes behavior
    # (a held PTT press can go out as silence); see README's "Preventing
    # acoustic feedback between nearby devices" section.
    enable_echo_guard: bool = False
    echo_guard_hold_ms: float = 500.0

    # Speaks the active channel's name out loud (local text-to-speech) each
    # time it changes - a hands-free convenience for operators who can't
    # glance at a screen. Uses pyttsx3 (installed by default); on Linux
    # also needs the system 'espeak' package. Off by default since most
    # nodes don't need it; applying a change to this flag requires a
    # restart (see outpost/announce.py).
    enable_channel_announce: bool = False

    # Case-insensitive substring matched against this machine's installed
    # TTS voices' id/name/language/gender (run `python -m outpost.announce`
    # to list them) - e.g. "Moira" selects macOS's built-in female Irish
    # English voice. Named voices aren't portable across platforms -
    # espeak's Linux voices aren't individually named, so use the portable
    # "female"/"male" there instead. None (default) uses the system
    # default voice. If no installed voice matches, falls back to the
    # default voice (logged as a warning) rather than failing
    # announcements outright. Only read once at startup, same as
    # enable_channel_announce.
    channel_announce_voice: str | None = None

    # Multicast delivery within a site (e.g. one batman-adv mesh): a talker
    # sends each frame once to a per-channel multicast group instead of one
    # copy per member, which is what makes a large channel workable on mesh
    # Wi-Fi. Members heard via multicast are skipped for unicast; everyone
    # else (VPN peers, nodes with this off) still gets unicast. Channels
    # with fewer than multicast_min_listeners listeners on this site stay
    # unicast, since a multicast floods the whole mesh. multicast_interface
    # is the interface (name, e.g. "bat0" or "br-lan", or its IPv4 address)
    # carrying the mesh - unset uses the OS default. Groups are
    # multicast_group_base + channel id (+0 for channel announces).
    # Subnets that make up this node's own site, for a relay acting as a
    # site bridge (see README "Bridging sites"). Unset: the subnet of
    # multicast_interface when multicast is on, otherwise none - a relay
    # with no site is a plain transit relay (e.g. a VPN hub).
    site_subnets: list[str] = field(default_factory=list)
    # Several bridges on one site: for each outside neighbour, the
    # highest-ranked bridge reaching it (bridge_priority 0-255, then lowest
    # sender_id) sends to it; another takes over once it has been silent for
    # 3 hello intervals. See README "Redundant bridges and mixed uplinks".
    bridge_priority: int = 100
    bridge_hello_interval_s: float = 2.0

    enable_multicast: bool = False
    multicast_interface: str | None = None
    multicast_group_base: str = "239.192.77.0"
    multicast_port: int = 5556
    multicast_min_listeners: int = 5

    # Channels to listen to in addition to the active (transmit) channel -
    # e.g. a team member on their team channel who also monitors the main
    # channel. Names must match configured channels. Audio from all of them
    # is mixed; channel_volumes (name -> 0.0-1.0) sets each one's level, and
    # with duck_monitored_channels the monitored ones are turned down while
    # someone is talking on the active channel. All three can be changed
    # live from the web UI's Status page.
    monitor_channels: list[str] = field(default_factory=list)
    channel_volumes: dict[str, float] = field(default_factory=dict)
    duck_monitored_channels: bool = True

    # Which microphone / speaker to use when a node has more than one (e.g.
    # a USB headset plus a built-in mic). Either a case-insensitive
    # substring of the device name (preferred - indices can change when USB
    # devices are replugged) or a device index. None uses the system
    # default. Run `python -m outpost.audio` to list devices. A value that
    # matches nothing logs a warning and falls back to the default. Changes
    # take effect after a restart.
    input_device: str | int | None = None
    output_device: str | int | None = None

    # Rotating file logging, in addition to scripts/run_client.py's console
    # output - off by default (None). Set log_file to also archive logs to
    # disk for diagnosing intermittent issues after the fact (console
    # scrollback is easily lost). Rotates purely by size: once a file
    # reaches log_max_bytes it's rolled over and a fresh one started; once
    # log_backup_count rolled-over files exist, the oldest is deleted - so
    # total disk usage stays bounded at roughly
    # log_max_bytes * (log_backup_count + 1) regardless of how long the
    # process runs. Not exposed to the web UI's settings editor (unlike
    # most other fields) since it's a filesystem path - letting a remote web
    # UI user redirect it would be an arbitrary-file-write primitive.
    log_file: str | None = None
    log_max_bytes: int = 10_000_000
    log_backup_count: int = 5

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


_MAX_ALIAS_LEN = 32


def _validate_alias(v):
    if v is None:
        return None
    if not isinstance(v, str):
        raise ValueError("must be a string")
    v = v.strip()
    if len(v) > _MAX_ALIAS_LEN:
        raise ValueError(f"must be at most {_MAX_ALIAS_LEN} characters")
    return v or None


_MAX_VOICE_LEN = 128


def _validate_voice(v):
    if v is None:
        return None
    if not isinstance(v, str):
        raise ValueError("must be a string")
    v = v.strip()
    if len(v) > _MAX_VOICE_LEN:
        raise ValueError(f"must be at most {_MAX_VOICE_LEN} characters")
    return v or None


_MAX_DEVICE_LEN = 128


def _validate_audio_device(v):
    """None/blank = system default, a non-negative int (or all-digit
    string, as the web UI's text inputs send) = device index, any other
    string = device name substring."""
    if v is None:
        return None
    if isinstance(v, bool):
        raise ValueError("must be a device name or index")
    if isinstance(v, int):
        if v < 0:
            raise ValueError("device index must not be negative")
        return v
    if not isinstance(v, str):
        raise ValueError("must be a device name or index")
    v = v.strip()
    if len(v) > _MAX_DEVICE_LEN:
        raise ValueError(f"must be at most {_MAX_DEVICE_LEN} characters")
    if v.isdigit():
        return int(v)
    return v or None


def _validate_priority(v) -> int:
    v = int(v)
    if not 0 <= v <= 255:
        raise ValueError("bridge_priority must be 0-255")
    return v


def _validate_subnets(v) -> list[str]:
    import ipaddress
    if v is None:
        return []
    if not isinstance(v, list):
        raise ValueError("site_subnets must be a list like [10.0.0.0/24]")
    return [str(ipaddress.IPv4Network(str(n), strict=False)) for n in v]


def _validate_group_base(v):
    import ipaddress
    addr = ipaddress.IPv4Address(str(v))
    if not addr.is_multicast or int(addr) & 0xFF:
        raise ValueError("multicast_group_base must be a multicast address ending in .0, e.g. 239.192.77.0")
    return str(addr)


def _validate_volume(v):
    v = float(v)
    if not 0.0 <= v <= 1.0:
        raise ValueError("volume must be between 0.0 and 1.0")
    return v


def _load_monitor_channels(raw_value, channels: dict) -> list[str]:
    if raw_value is None:
        return []
    if not isinstance(raw_value, list) or not all(isinstance(n, str) for n in raw_value):
        raise ValueError("monitor_channels must be a list of channel names")
    names = {ch.name for ch in channels.values()}
    unknown = [n for n in raw_value if n not in names]
    if unknown:
        log.warning("monitor_channels: ignoring unknown channel(s) %s", ", ".join(unknown))
    return [n for n in dict.fromkeys(raw_value) if n in names]


def _load_channel_volumes(raw_value) -> dict[str, float]:
    if raw_value is None:
        return {}
    if not isinstance(raw_value, dict):
        raise ValueError("channel_volumes must be a mapping of channel name -> volume")
    return {str(k): _validate_volume(v) for k, v in raw_value.items()}


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
    "sender_alias": (_validate_alias, True),
    "bitrate": (_validate_positive_int, False),
    "frame_ms": (_validate_positive_int, False),
    "max_packet_bytes": (_validate_positive_int, False),
    "inband_fec": (_validate_bool, False),
    "duck_monitored_channels": (_validate_bool, True),
    "enable_multicast": (_validate_bool, False),
    "multicast_min_listeners": (_validate_positive_int, True),
    "constant_size_audio": (_validate_bool, False),
    "enable_mdns": (_validate_bool, False),
    "enable_pex": (_validate_bool, False),
    "pex_interval_s": (_validate_positive_float, False),
    "pex_fanout": (_validate_positive_int, False),
    "max_peers": (_validate_positive_int, False),
    "enable_subnet_scan": (_validate_bool, False),
    "subnet_scan_interval_s": (_validate_positive_float, False),
    "subnet_scan_max_hosts": (_validate_positive_int, False),
    "channel_announce_interval_s": (_validate_positive_float, False),
    "enable_agc": (_validate_bool, True),
    "agc_target_rms": (_validate_positive_float, True),
    "enable_relay": (_validate_bool, True),
    "enable_highpass": (_validate_bool, True),
    "highpass_cutoff_hz": (_validate_positive_float, True),
    "enable_squelch": (_validate_bool, True),
    "squelch_threshold_rms": (_validate_nonnegative_float, True),
    "squelch_hold_ms": (_validate_nonnegative_float, True),
    "enable_tx_lockout": (_validate_bool, True),
    "tx_lockout_timeout_ms": (_validate_positive_float, True),
    "enable_echo_guard": (_validate_bool, True),
    "echo_guard_hold_ms": (_validate_positive_float, True),
    "enable_channel_announce": (_validate_bool, False),
    "channel_announce_voice": (_validate_voice, False),
    "input_device": (_validate_audio_device, False),
    "output_device": (_validate_audio_device, False),
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
    _atomic_write_yaml(path, raw)


def _atomic_write_yaml(path: str, raw: dict) -> None:
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        yaml.safe_dump(raw, f, sort_keys=False)
    os.replace(tmp_path, path)


def add_channel_to_file(path: str, channel_id: int, name: str, key: bytes) -> None:
    """Appends a new channel entry to the YAML config file's `channels:`
    list. Caller is responsible for picking a free `channel_id` and
    checking `name` isn't already in use."""
    with open(path, "r") as f:
        raw = yaml.safe_load(f) or {}
    raw.setdefault("channels", []).append({
        "id": channel_id, "name": name, "key": base64.b64encode(key).decode(),
    })
    _atomic_write_yaml(path, raw)


def rename_channel_in_file(path: str, channel_id: int, new_name: str) -> None:
    with open(path, "r") as f:
        raw = yaml.safe_load(f) or {}
    for entry in raw.get("channels", []):
        if entry["id"] == channel_id:
            entry["name"] = new_name
            break
    else:
        raise KeyError(f"no channel with id {channel_id}")
    _atomic_write_yaml(path, raw)


def rotate_channel_key_in_file(path: str, channel_id: int, key: bytes) -> None:
    with open(path, "r") as f:
        raw = yaml.safe_load(f) or {}
    for entry in raw.get("channels", []):
        if entry["id"] == channel_id:
            entry["key"] = base64.b64encode(key).decode()
            break
    else:
        raise KeyError(f"no channel with id {channel_id}")
    _atomic_write_yaml(path, raw)


def add_peer_to_file(path: str, host: str, port: int) -> None:
    """Appends a new entry to the YAML config file's `peers:` list."""
    with open(path, "r") as f:
        raw = yaml.safe_load(f) or {}
    raw.setdefault("peers", []).append({"host": host, "port": port})
    _atomic_write_yaml(path, raw)


def remove_peer_from_file(path: str, host: str, port: int) -> None:
    with open(path, "r") as f:
        raw = yaml.safe_load(f) or {}
    peers = raw.get("peers", [])
    remaining = [p for p in peers if not (p.get("host") == host and p.get("port") == port)]
    if len(remaining) == len(peers):
        raise KeyError(f"no peer {host}:{port}")
    raw["peers"] = remaining
    _atomic_write_yaml(path, raw)


def export_config_text(path: str) -> str:
    """Returns the config file's raw contents verbatim (comments/formatting
    included) - for the web UI's "export config" download, and for handing
    a ready-to-use file to a new node (minus its own sender_id, which is
    per-node and auto-generated on first run - see _resolve_sender_id)."""
    with open(path, "r") as f:
        return f.read()


def validate_config_yaml(text: str) -> None:
    """Structural validation of an uploaded config file before it's allowed
    to overwrite the running client's config file - raises ValueError with
    a human-readable reason on any problem. Deliberately does not resolve
    sender_id or construct an OutpostConfig (load_config does that, with
    its own defaulting, the next time the client actually starts on this
    file) - this only guards against writing an obviously broken/malicious
    file over a working config."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"not valid YAML: {exc}")
    if not isinstance(raw, dict):
        raise ValueError("must be a YAML mapping (object) at the top level")

    if "mesh_key" not in raw:
        raise ValueError("missing required field 'mesh_key'")
    try:
        mesh_key = base64.b64decode(raw["mesh_key"], validate=True)
    except Exception:
        raise ValueError("mesh_key is not valid base64")
    if len(mesh_key) != KEY_SIZE:
        raise ValueError(f"mesh_key must decode to {KEY_SIZE} bytes")

    channels = raw.get("channels")
    if not isinstance(channels, list) or not channels:
        raise ValueError("missing or empty required field 'channels' (list)")
    for entry in channels:
        if not isinstance(entry, dict) or not {"id", "name", "key"} <= entry.keys():
            raise ValueError("each channels entry needs id, name, and key")
        try:
            key = base64.b64decode(entry["key"], validate=True)
        except Exception:
            raise ValueError(f"channel {entry.get('name')!r} key is not valid base64")
        if len(key) != KEY_SIZE:
            raise ValueError(f"channel {entry.get('name')!r} key must decode to {KEY_SIZE} bytes")

    for entry in raw.get("peers", []) or []:
        if not isinstance(entry, dict) or not {"host", "port"} <= entry.keys():
            raise ValueError("each peers entry needs host and port")


def write_config_text(path: str, text: str) -> None:
    """Atomically overwrites the config file at `path` with raw `text`
    verbatim - unlike save_config_fields/_atomic_write_yaml, this does not
    re-serialize via yaml.safe_dump, so an imported file's own formatting/
    comments are preserved. Caller must validate with validate_config_yaml
    first."""
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        f.write(text)
    os.replace(tmp_path, path)



def _read_identity(identity_path: str) -> tuple[int, set[str]]:
    """(sender_id, recorded hardware ids) from an identity file. Files
    written before hardware ids were recorded are just the sender_id."""
    with open(identity_path, "r") as f:
        lines = f.read().splitlines()
    sender_id = int(lines[0].strip())
    hw: set[str] = set()
    for line in lines[1:]:
        if line.startswith("hardware:"):
            hw = {h for h in line.split(":", 1)[1].strip().split(",") if h}
    return sender_id, hw


def _write_identity(identity_path: str, sender_id: int, hw: set[str]) -> None:
    tmp_path = identity_path + ".tmp"
    with open(tmp_path, "w") as f:
        f.write(f"{sender_id}\n")
        if hw:
            f.write(f"hardware: {','.join(sorted(hw))}\n")
    os.replace(tmp_path, identity_path)


def _resolve_sender_id(config_path: str, raw: dict) -> int:
    """Returns the configured sender_id, or auto-generates and persists one.

    This lets the exact same outpost.yaml (mesh_key, channels, network
    profile) be copied to every node unmodified - the one genuinely
    per-node value is picked automatically on first run and cached next to
    the config so it stays stable across restarts (sender_id feeds the
    encryption nonce, so it must not change while old sequence numbers are
    still in play - see crypto.py).

    The cache also records which hardware it was made on (see identity.py).
    If it turns up on different hardware - an SD card image or folder copied
    from another node - a new sender_id is picked, since two nodes sharing
    one can reuse encryption nonces. An explicit sender_id in the config is
    never changed; client.py warns at runtime if another node uses it too.
    """
    if "sender_id" in raw and raw["sender_id"] is not None:
        return raw["sender_id"]

    identity_path = config_path + ".identity"
    current_hw = hardware_ids()
    if os.path.exists(identity_path):
        sender_id, recorded_hw = _read_identity(identity_path)
        if recorded_hw and current_hw and not (recorded_hw & current_hw):
            new_id = secrets.randbits(32)
            log.warning(
                "%s was created on different hardware (copied or cloned from another node?) - "
                "replacing sender_id %d with a new one, %d, so the two nodes don't share an "
                "encryption identity", identity_path, sender_id, new_id)
            _write_identity(identity_path, new_id, current_hw)
            return new_id
        if not recorded_hw and current_hw:
            # Pre-fingerprint identity file: assume it belongs here.
            _write_identity(identity_path, sender_id, current_hw)
        return sender_id

    sender_id = secrets.randbits(32)
    _write_identity(identity_path, sender_id, current_hw)
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
        sender_alias=raw.get("sender_alias"),
        bitrate=raw.get("bitrate", 16000),
        frame_ms=raw.get("frame_ms", 20),
        max_packet_bytes=raw.get("max_packet_bytes", 1200),
        inband_fec=raw.get("inband_fec", True),
        constant_size_audio=raw.get("constant_size_audio", True),
        enable_mdns=raw.get("enable_mdns", True),
        enable_pex=raw.get("enable_pex", True),
        pex_interval_s=raw.get("pex_interval_s", 30.0),
        pex_fanout=raw.get("pex_fanout", 8),
        max_peers=raw.get("max_peers", 128),
        enable_subnet_scan=raw.get("enable_subnet_scan", False),
        subnet_scan_interval_s=raw.get("subnet_scan_interval_s", 60.0),
        subnet_scan_max_hosts=raw.get("subnet_scan_max_hosts", 512),
        channel_announce_interval_s=raw.get("channel_announce_interval_s", 60.0),
        enable_highpass=raw.get("enable_highpass", True),
        highpass_cutoff_hz=raw.get("highpass_cutoff_hz", 100.0),
        enable_agc=raw.get("enable_agc", True),
        agc_target_rms=raw.get("agc_target_rms", 4000.0),
        enable_squelch=raw.get("enable_squelch", False),
        squelch_threshold_rms=raw.get("squelch_threshold_rms", 300.0),
        squelch_hold_ms=raw.get("squelch_hold_ms", 300.0),
        enable_relay=raw.get("enable_relay", False),
        enable_tx_lockout=raw.get("enable_tx_lockout", False),
        tx_lockout_timeout_ms=raw.get("tx_lockout_timeout_ms", 500.0),
        enable_echo_guard=raw.get("enable_echo_guard", False),
        echo_guard_hold_ms=raw.get("echo_guard_hold_ms", 500.0),
        enable_channel_announce=raw.get("enable_channel_announce", False),
        channel_announce_voice=raw.get("channel_announce_voice"),
        site_subnets=_validate_subnets(raw.get("site_subnets")),
        bridge_priority=_validate_priority(raw.get("bridge_priority", 100)),
        bridge_hello_interval_s=_validate_positive_float(raw.get("bridge_hello_interval_s", 2.0)),
        enable_multicast=raw.get("enable_multicast", False),
        multicast_interface=raw.get("multicast_interface"),
        multicast_group_base=_validate_group_base(raw.get("multicast_group_base", "239.192.77.0")),
        multicast_port=_validate_port(raw.get("multicast_port", 5556)),
        multicast_min_listeners=_validate_positive_int(raw.get("multicast_min_listeners", 5)),
        monitor_channels=_load_monitor_channels(raw.get("monitor_channels"), channels),
        channel_volumes=_load_channel_volumes(raw.get("channel_volumes")),
        duck_monitored_channels=raw.get("duck_monitored_channels", True),
        input_device=_validate_audio_device(raw.get("input_device")),
        output_device=_validate_audio_device(raw.get("output_device")),
        log_file=raw.get("log_file"),
        log_max_bytes=raw.get("log_max_bytes", 10_000_000),
        log_backup_count=raw.get("log_backup_count", 5),
        enable_web_ui=raw.get("enable_web_ui", False),
        web_ui_host=raw.get("web_ui_host", "127.0.0.1"),
        web_ui_port=raw.get("web_ui_port", 8765),
        web_ui_token=raw.get("web_ui_token"),
    )

