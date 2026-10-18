"""Tests for outpost/subnet_scan.py's SubnetScanner - auto-discovery of peers
on a node's own directly-connected subnets (e.g. a WireGuard tunnel), which
mDNS can't reach since those links don't carry multicast."""
from __future__ import annotations
import ipaddress

import ifaddr

from outpost.subnet_scan import local_subnets, SubnetScanner


def _adapter(nice_name: str, ip: str, prefix: int) -> ifaddr.Adapter:
    return ifaddr.Adapter(nice_name, nice_name, [ifaddr.IP(ip, prefix, nice_name)])


class _FakePex:
    def __init__(self):
        self.probed: list[tuple[str, int]] = []

    def probe(self, host: str, port: int) -> None:
        self.probed.append((host, port))


def test_local_subnets_includes_small_subnet_and_skips_loopback(monkeypatch):
    monkeypatch.setattr(ifaddr, "get_adapters", lambda: [
        _adapter("lo", "127.0.0.1", 8),
        _adapter("wg0", "10.8.0.2", 24),
    ])

    subnets = local_subnets(max_hosts=512)

    assert len(subnets) == 1
    network = next(iter(subnets.values()))
    assert network == ipaddress.ip_network("10.8.0.0/24")


def test_local_subnets_skips_oversized_subnet(monkeypatch):
    monkeypatch.setattr(ifaddr, "get_adapters", lambda: [
        _adapter("eth0", "10.0.0.5", 8),  # a /8 has far more than 512 hosts
    ])

    subnets = local_subnets(max_hosts=512)

    assert subnets == {}


def test_scan_once_probes_every_other_host_in_subnet_excluding_self(monkeypatch):
    monkeypatch.setattr(ifaddr, "get_adapters", lambda: [
        _adapter("wg0", "10.8.0.1", 30),  # /30 -> exactly 2 usable hosts
    ])
    pex = _FakePex()
    scanner = SubnetScanner(pex, listen_port=5555, max_hosts=512)

    scanner.scan_once()

    assert pex.probed == [("10.8.0.2", 5555)]


def test_scan_once_picks_up_an_interface_added_after_the_first_round(monkeypatch):
    pex = _FakePex()
    scanner = SubnetScanner(pex, listen_port=5555, max_hosts=512)

    monkeypatch.setattr(ifaddr, "get_adapters", lambda: [])
    scanner.scan_once()
    assert pex.probed == []

    monkeypatch.setattr(ifaddr, "get_adapters", lambda: [
        _adapter("wg0", "10.8.0.1", 30),
    ])
    scanner.scan_once()
    assert pex.probed == [("10.8.0.2", 5555)]
