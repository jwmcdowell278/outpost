"""Auto-discovery of peers on a node's own directly-connected subnets
(LAN, WireGuard, batman-adv, etc.) by probing every candidate address with
an authenticated PEX packet.

mDNS needs multicast, which most VPN/point-to-point links (WireGuard) and
multi-hop mesh radio don't carry; static `peers:` entries need to be
hand-maintained per device. This fills the gap for a link where every
device sits in a known IP subnet (e.g. a WireGuard tunnel's /24) but
multicast doesn't reach: each round, a node re-reads its own interfaces (so
a VPN that connects after startup - or any other interface added later -
is picked up without a restart) and unicast-probes every other address in
each interface's subnet via PexGossip.probe(). Any real Outpost node
sharing mesh_key at that address authenticates the probe, is added as a
peer, and (per PexGossip.handle_incoming) replies immediately.

Bounded by max_hosts so a huge/default-route interface's subnet (e.g. a
wide corporate LAN, or a VPN handing out a /8) isn't swept at all - such an
interface is skipped and logged once rather than partially scanned, since a
partial scan would create a false impression of "none found here".
"""
from __future__ import annotations
import asyncio
import ipaddress
import logging
import random

import ifaddr

from .pex import PexGossip

log = logging.getLogger(__name__)

DEFAULT_MAX_HOSTS = 512


def local_subnets(max_hosts: int = DEFAULT_MAX_HOSTS) -> dict[str, ipaddress.IPv4Network]:
    """interface-identifying key -> subnet, for every up IPv4 interface whose
    subnet (host portion) is small enough to scan - skips loopback and
    oversized subnets. Keyed by adapter name + subnet (not just adapter
    name) so a change in an interface's subnet is itself detected as new."""
    subnets: dict[str, ipaddress.IPv4Network] = {}
    try:
        adapters = ifaddr.get_adapters()
    except OSError as exc:
        log.warning("interface enumeration failed, subnet scan skipped this round: %s", exc)
        return subnets
    for adapter in adapters:
        for ip in adapter.ips:
            if not ip.is_IPv4 or ip.ip == "127.0.0.1":
                continue
            try:
                network = ipaddress.ip_network(f"{ip.ip}/{ip.network_prefix}", strict=False)
            except ValueError:
                continue
            host_count = network.num_addresses - 2  # exclude network/broadcast addresses
            if host_count > max_hosts:
                log.debug("skipping oversized subnet %s on %s (%d hosts > max %d)",
                          network, adapter.nice_name, host_count, max_hosts)
                continue
            subnets[f"{adapter.nice_name}:{network}"] = network
    return subnets


class SubnetScanner:
    """Periodically probes every address of every small-enough local subnet."""

    def __init__(self, pex: PexGossip, listen_port: int, interval_s: float = 60.0, max_hosts: int = DEFAULT_MAX_HOSTS):
        self._pex = pex
        self._listen_port = listen_port
        self._interval_s = interval_s
        self._max_hosts = max_hosts
        self._task: asyncio.Task | None = None
        self._known_subnet_keys: set[str] = set()

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    def stop(self) -> None:
        if self._task:
            self._task.cancel()
            self._task = None

    async def _loop(self) -> None:
        # Jitter the first scan so many nodes starting at once don't all
        # flood their subnet in lockstep - same rationale as PexGossip.
        await asyncio.sleep(random.uniform(0, self._interval_s))
        while True:
            self.scan_once()
            await asyncio.sleep(self._interval_s * random.uniform(0.8, 1.2))

    def scan_once(self) -> None:
        subnets = local_subnets(self._max_hosts)
        new_keys = set(subnets) - self._known_subnet_keys
        for key in new_keys:
            log.info("subnet scan: new interface subnet detected, probing %s", subnets[key])
        self._known_subnet_keys = set(subnets)

        local_ips = {
            ip.ip
            for adapter in ifaddr.get_adapters()
            for ip in adapter.ips
            if ip.is_IPv4
        }
        for network in subnets.values():
            for host in network.hosts():
                ip = str(host)
                if ip in local_ips:
                    continue
                self._pex.probe(ip, self._listen_port)
