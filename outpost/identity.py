"""Hardware fingerprint for the auto-generated sender_id.

sender_id is half of every packet's encryption nonce (see crypto.py), so two
nodes sharing one under the same channel key can reuse nonces - which leaks
audio and allows forged packets. The usual way that happens is cloning: an
SD card image or config folder copied from an already-running node carries
its `<config>.identity` file along. So that file also records hashed
identifiers of the hardware it was created on, and config._resolve_sender_id
picks a fresh sender_id when it finds itself on different hardware.

Identifiers:
- macOS: the IOPlatformUUID (per-machine, survives OS reinstalls).
- Linux: MAC addresses of physical network interfaces (ones backed by a
  real device - virtual/bridge/VPN/docker interfaces are skipped). Cloned
  Raspberry Pis share /etc/machine-id, so that's deliberately not used.

The file is considered to match if ANY recorded identifier is still present,
so adding hardware (a USB Wi-Fi dongle) doesn't trigger a new ID - only
finding none of the original hardware does. Identifiers are stored hashed.
"""
from __future__ import annotations
import hashlib
import os
import subprocess
import sys

_SYS_NET = "/sys/class/net"


def _hash(value: str) -> str:
    return hashlib.sha256(b"outpost-hw:" + value.strip().lower().encode()).hexdigest()[:16]


def _macos_ids() -> set[str]:
    try:
        out = subprocess.run(
            ["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"],
            capture_output=True, text=True, timeout=5, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return set()
    for line in out.splitlines():
        if "IOPlatformUUID" in line:
            return {line.split("=", 1)[1].strip().strip('"')}
    return set()


def _linux_ids(sys_net: str = _SYS_NET) -> set[str]:
    ids = set()
    try:
        names = os.listdir(sys_net)
    except OSError:
        return ids
    for name in names:
        # Only physical NICs have a "device" link; skips lo, docker0,
        # bridges, wg0, bat0, veth*, etc.
        if not os.path.exists(os.path.join(sys_net, name, "device")):
            continue
        try:
            with open(os.path.join(sys_net, name, "address")) as f:
                mac = f.read().strip()
        except OSError:
            continue
        if mac and mac != "00:00:00:00:00:00":
            ids.add(mac)
    return ids


def hardware_ids() -> set[str]:
    """Hashed identifiers of this machine's hardware - empty if none could
    be read, in which case the clone check is skipped."""
    if sys.platform == "darwin":
        raw = _macos_ids()
    elif sys.platform.startswith("linux"):
        raw = _linux_ids()
    else:
        raw = set()
    return {_hash(v) for v in raw}
