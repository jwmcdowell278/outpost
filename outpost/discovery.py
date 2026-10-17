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
import logging
import socket

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


def _all_local_ipv4s() -> list[str]:
    """Every non-loopback IPv4 address across all interfaces, primary/default-
    route one first. A node can have multiple active interfaces (e.g. LAN +
    VPN + mesh radio) and a peer on a secondary interface still needs an
    advertised address it can actually reach."""
    ips = []
    try:
        primary = _local_ip()
        ips.append(primary)
    except OSError:
        primary = None
    try:
        for adapter in ifaddr.get_adapters():
            for ip in adapter.ips:
                if ip.is_IPv4 and ip.ip != "127.0.0.1" and ip.ip != primary:
                    ips.append(ip.ip)
    except OSError as exc:
        log.warning("interface enumeration failed, advertising primary address only: %s", exc)
    return ips


def _subnet_prefix(ip: str) -> str:
    return ".".join(ip.split(".")[:3])


def _order_candidates(hosts: list[str], port: int) -> list[Peer]:
    """Same-subnet ("shortest path" - likely a direct LAN hop) addresses
    first, other addresses (e.g. only reachable via a VPN/routed hop) as
    fallback - see MeshTransport.add_peer_group."""
    local_prefixes = {_subnet_prefix(ip) for ip in _all_local_ipv4s()}
    peers = [Peer(host=host, port=port) for host in hosts]
    local_first = [p for p in peers if _subnet_prefix(p.host) in local_prefixes]
    others = [p for p in peers if _subnet_prefix(p.host) not in local_prefixes]
    return local_first + others


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
    """Advertises this node on the LAN and reports discovered peers via callbacks."""

    def __init__(self, sender_id: int, listen_port: int, on_peer_added, on_peer_removed):
        self._sender_id = sender_id
        self._listen_port = listen_port
        self._on_peer_added = on_peer_added
        self._on_peer_removed = on_peer_removed
        self._service_name = f"outpost-{sender_id}.{SERVICE_TYPE}"
        self._zc: Zeroconf | None = None
        self._browser: ServiceBrowser | None = None
        self._info: ServiceInfo | None = None

    def start(self) -> None:
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
        log.info("advertising as %s at %s:%d", self._service_name, ips, self._listen_port)

    def stop(self) -> None:
        if self._zc:
            if self._info:
                self._zc.unregister_service(self._info)
            self._zc.close()
            self._zc = None
            self._browser = None
