"""mDNS discovery on a machine whose interfaces change (outpost/discovery.py):
which addresses are advertised, how a peer's addresses are ordered, and
restarting on new interfaces. ifaddr and zeroconf are faked."""
from __future__ import annotations
import ipaddress
from types import SimpleNamespace

from outpost import discovery as d


def _adapter(name, *ips):
    return SimpleNamespace(nice_name=name, name=name, ips=[
        SimpleNamespace(ip=ip, network_prefix=prefix, is_IPv4=True) for ip, prefix in ips])


PISERVER = [
    _adapter("lo", ("127.0.0.1", 8)),
    _adapter("eth0", ("192.168.88.10", 24)),
    _adapter("bat0", ("10.42.226.215", 16)),
    _adapter("docker0", ("172.17.0.1", 16)),
    _adapter("br-a5619397951f", ("172.18.0.1", 16)),
    _adapter("veth1", ("169.254.3.3", 16)),
    _adapter("wg0", ("10.20.31.80", 24)),
]


def test_container_bridges_loopback_and_link_local_are_not_advertised(monkeypatch):
    monkeypatch.setattr(d.ifaddr, "get_adapters", lambda: PISERVER)
    monkeypatch.setattr(d, "_local_ip", lambda: "192.168.88.10")
    assert d._all_local_ipv4s() == ["192.168.88.10", "10.42.226.215", "10.20.31.80"]


def test_no_default_route_still_lists_every_address(monkeypatch):
    monkeypatch.setattr(d.ifaddr, "get_adapters", lambda: [_adapter("bat0", ("10.42.69.146", 16))])

    def no_route():
        raise OSError("Network is unreachable")
    monkeypatch.setattr(d, "_local_ip", no_route)
    assert d._all_local_ipv4s() == ["10.42.69.146"]


def test_mesh_addresses_count_as_local_by_real_netmask():
    # pi3 (only on the /16 mesh) and piserver's advertised addresses, Docker's
    # first (as an old node advertised them): the mesh one must come first.
    nets = [ipaddress.IPv4Network("10.42.0.0/16")]
    ordered = d._order_candidates(["172.17.0.1", "192.168.88.10", "10.42.226.215"], 5555, networks=nets)
    assert [p.host for p in ordered] == ["10.42.226.215", "172.17.0.1", "192.168.88.10"]


class _FakeZeroconf:
    instances = []

    def __init__(self):
        self.registered = []
        self.closed = False
        _FakeZeroconf.instances.append(self)

    def register_service(self, info): self.registered.append(info)
    def unregister_service(self, info): pass
    def close(self): self.closed = True


def test_restarts_when_addresses_change(monkeypatch):
    _FakeZeroconf.instances.clear()
    monkeypatch.setattr(d, "Zeroconf", _FakeZeroconf)
    monkeypatch.setattr(d, "ServiceBrowser", lambda zc, type_, listener: object())
    addresses = {"now": ["192.168.88.36", "10.42.69.146"]}
    monkeypatch.setattr(d, "_all_local_ipv4s", lambda: list(addresses["now"]))
    disc = d.PeerDiscovery(229427481, 5555, on_peer_added=lambda *a: None, on_peer_removed=lambda *a: None)

    assert disc.check_networks() is True              # not running yet: starts
    assert disc.advertised == ["10.42.69.146", "192.168.88.36"]
    assert disc.check_networks() is False             # nothing changed
    assert len(_FakeZeroconf.instances) == 1

    addresses["now"] = ["10.42.69.146"]               # Wi-Fi switched off
    assert disc.check_networks() is True
    assert _FakeZeroconf.instances[0].closed and len(_FakeZeroconf.instances) == 2
    assert disc.advertised == ["10.42.69.146"]

    addresses["now"] = []                             # no network at all: stays stopped
    assert disc.check_networks() is False
    assert disc.advertised is None
    addresses["now"] = ["10.42.69.146"]               # and comes back
    assert disc.check_networks() is True
    disc.stop()
    assert disc.advertised is None


def test_client_keeps_checking_and_survives_errors():
    import asyncio
    from outpost.client import OutpostClient
    from outpost.config import OutpostConfig, Channel
    from outpost.crypto import generate_channel_key

    client = OutpostClient(OutpostConfig(sender_id=1, listen_port=0, mesh_key=generate_channel_key(),
                                         channels={1: Channel(id=1, name="ops", key=generate_channel_key())},
                                         enable_mdns=False, enable_pex=False), "ops")
    calls = []

    def check():
        calls.append(1)
        if len(calls) == 2:
            raise OSError("interface vanished mid-restart")
        return False
    client.discovery = SimpleNamespace(check_networks=check)

    async def run():
        task = asyncio.create_task(client._network_watch_loop(interval_s=0.01))
        await asyncio.sleep(0.15)
        task.cancel()
    asyncio.run(run())
    assert len(calls) >= 4  # kept going after the error
