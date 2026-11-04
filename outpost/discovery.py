"""LAN peer discovery via mDNS/DNS-SD (zeroconf).

Replaces hand-maintained static peer lists: every node advertises itself as
soon as it starts, and discovers every other node already on the network (or
added later) automatically - no config changes needed when deploying new
devices.

Static `peers:` entries in config remain supported as a supplement, for
links mDNS can't reach (e.g. peers on a different subnet/VPN hop without
multicast routing).
"""
from __future__ import annotations
import ipaddress
import logging
import socket
import threading

import ifaddr
from zeroconf import ServiceBrowser, ServiceInfo, ServiceListener, Zeroconf

from .config import Peer

log = logging.getLogger(__name__)

SERVICE_TYPE = "_outpost._udp.local."
PROTOCOL_VERSION = 1


def _local_ip() -> str:
    """Best-effort primary local IP, without actually sending any traffic."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    finally:
        s.close()


# Container and VM bridges: their addresses only mean something on this
# machine (Docker's 172.17.0.1, ...), so they're never advertised - another
# node would pick one and send into nowhere. VPNs (wg*, tun*, ...) are real.
_VIRTUAL_IFACE_PREFIXES = ("docker", "br-", "veth", "virbr", "lxcbr", "lxdbr", "cni", "flannel",
                           "podman", "vmnet", "vboxnet", "vnet")


def _is_virtual(name: str) -> bool:
    return name.startswith(_VIRTUAL_IFACE_PREFIXES)


def _local_networks() -> list[tuple[str, ipaddress.IPv4Network, str]]:
    """(address, its network, interface name) for every usable IPv4
    address: not loopback, not link-local, not on a container/VM bridge."""
    nets = []
    for adapter in ifaddr.get_adapters():
        name = adapter.nice_name or str(adapter.name)
        if _is_virtual(name):
            continue
        for ip in adapter.ips:
            if not ip.is_IPv4:
                continue
            try:
                addr = ipaddress.IPv4Address(ip.ip)
                net = ipaddress.IPv4Network(f"{ip.ip}/{ip.network_prefix}", strict=False)
            except ValueError:
                continue
            if addr.is_loopback or addr.is_link_local:
                continue
            nets.append((ip.ip, net, name))
    return nets


def _all_local_ipv4s() -> list[str]:
    """Every usable IPv4 address across all interfaces (see _local_networks),
    primary/default-route one first. A node can have multiple active
    interfaces (e.g. LAN + VPN + mesh radio) and a peer on a secondary
    interface still needs an advertised address it can actually reach."""
    try:
        local = [ip for ip, _, _ in _local_networks()]
    except OSError as exc:
        log.warning("interface enumeration failed, advertising primary address only: %s", exc)
        local = []
    try:
        primary = _local_ip()
    except OSError:
        primary = None  # no default route (e.g. only a mesh)
    if primary and (primary in local or not local):
        return [primary] + [ip for ip in local if ip != primary]
    return local


def _order_candidates(hosts: list[str], port: int, networks=None) -> list[Peer]:
    """Addresses on one of this node's own networks first (a direct hop -
    by each interface's real netmask, so a /16 mesh counts as one network),
    other addresses (e.g. only reachable via a VPN/routed hop) as fallback -
    see MeshTransport.add_peer_group."""
    if networks is None:
        try:
            networks = [net for _, net, _ in _local_networks()]
        except OSError:
            networks = []
    peers = [Peer(host=host, port=port) for host in hosts]

    def local(p: Peer) -> bool:
        try:
            a = ipaddress.IPv4Address(p.host)
        except ValueError:
            return False
        return any(a in net for net in networks)
    return [p for p in peers if local(p)] + [p for p in peers if not local(p)]


class _Listener(ServiceListener):
    def __init__(self, own_name, on_add, on_remove):
        self._own_name = own_name
        self._on_add = on_add
        self._on_remove = on_remove

    def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        self._handle(zc, type_, name)

    def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        self._handle(zc, type_, name)

    def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        if name == self._own_name:
            return
        self._on_remove(name)

    def _handle(self, zc: Zeroconf, type_: str, name: str) -> None:
        if name == self._own_name:
            return
        info = zc.get_service_info(type_, name)
        if info is None or not info.addresses:
            return
        # One mDNS service name is one device - its several advertised
        # addresses (one per its own interfaces) are candidates for the
        # SAME logical peer, not separate peers (see add_peer_group).
        hosts = [socket.inet_ntoa(raw) for raw in info.addresses]
        self._on_add(name, _order_candidates(hosts, info.port))


class PeerDiscovery:
    """Advertises this node on the LAN and reports discovered peers via
    callbacks. Interfaces come and go (Wi-Fi switched off, a mesh coming up
    after start): check_networks() restarts it on the current ones."""

    def __init__(self, sender_id: int, listen_port: int, on_peer_added, on_peer_removed):
        self._sender_id = sender_id
        self._listen_port = listen_port
        self._on_peer_added = on_peer_added
        self._on_peer_removed = on_peer_removed
        self._service_name = f"outpost-{sender_id}.{SERVICE_TYPE}"
        self._zc: Zeroconf | None = None
        self._browser: ServiceBrowser | None = None
        self._info: ServiceInfo | None = None
        self._lock = threading.Lock()
        self.advertised: list[str] | None = None   # addresses of the running instance

    @staticmethod
    def _networks_now() -> list[str]:
        return sorted(_all_local_ipv4s())

    def check_networks(self) -> bool:
        """Restart on this machine's current interfaces if its addresses
        changed since the last start (or start if not running). Blocking -
        call from an executor thread. Returns whether it (re)started."""
        with self._lock:
            now = self._networks_now()
            if self._zc is not None and now == self.advertised:
                return False
            if self._zc is not None:
                log.info("network addresses changed (%s -> %s) - re-announcing on mDNS",
                         ", ".join(self.advertised or []) or "none", ", ".join(now) or "none")
                self._stop_locked()
            if not now:
                return False
            self._start_locked()
            return True

    def start(self) -> None:
        with self._lock:
            self._start_locked()

    def _start_locked(self) -> None:
        self._zc = Zeroconf()
        ips = _all_local_ipv4s()
        self._info = ServiceInfo(
            SERVICE_TYPE,
            self._service_name,
            addresses=[socket.inet_aton(ip) for ip in ips],
            port=self._listen_port,
            properties={"proto": str(PROTOCOL_VERSION)},
        )
        self._zc.register_service(self._info)
        listener = _Listener(self._service_name, self._on_peer_added, self._on_peer_removed)
        self._browser = ServiceBrowser(self._zc, SERVICE_TYPE, listener)
        self.advertised = sorted(ips)
        log.info("advertising as %s at %s:%d", self._service_name, ips, self._listen_port)

    def stop(self) -> None:
        with self._lock:
            self._stop_locked()

    def _stop_locked(self) -> None:
        if self._zc:
            try:
                if self._info:
                    self._zc.unregister_service(self._info)
            except Exception as exc:  # e.g. the interface it was on is gone
                log.debug("mDNS unregister failed: %s", exc)
            try:
                self._zc.close()
            except Exception as exc:
                log.debug("mDNS close failed: %s", exc)
            self._zc = None
            self._browser = None
        self.advertised = None
