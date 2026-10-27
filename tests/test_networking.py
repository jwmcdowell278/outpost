"""Network settings (outpost/networking.py) and the web UI's Network page
API. nmcli is simulated - nothing touches real network settings."""
from __future__ import annotations
import asyncio
import os
import stat

import pytest
from aiohttp.test_utils import TestClient, TestServer

from outpost import networking as net
from outpost.client import OutpostClient
from outpost.config import OutpostConfig, Channel
from outpost.crypto import generate_channel_key
from outpost.networking import (NetworkManager, NetworkError, build_wg_conf, parse_wg_conf, parse_wg_dump,
                                split_terse, generate_wg_private_key, wg_public_key, AP_CONNECTION)
from outpost.webui import WebUI


class FakeNmcli:
    """A tiny NetworkManager: connections, the Wi-Fi radio and device, and
    a log of every command."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.radio = True
        self.conns = {
            "Wired connection 1": {"uuid": "u-eth", "type": "802-3-ethernet", "auto": True, "active": True},
            "HomeNet": {"uuid": "u-home", "type": "802-11-wireless", "auto": True, "active": True,
                        "mode": "infrastructure", "ssid": "HomeNet"},
        }
        self.fail: dict[str, str] = {}
        self.wg_dump = ""

    def _by(self, ref):
        for name, c in self.conns.items():
            if ref in (name, c["uuid"]):
                return name, c
        return None, None

    async def __call__(self, args, timeout):
        self.calls.append(list(args))
        if args[0] == "sudo":
            return (0, self.wg_dump) if self.wg_dump else (1, "sudo: a password is required")
        a = args[1:]
        key = " ".join(a[:2])
        if key in self.fail:
            return 10, self.fail[key]
        if a == ["radio", "wifi"]:
            return 0, "enabled\n" if self.radio else "disabled\n"
        if a[:2] == ["radio", "wifi"]:
            self.radio = a[2] == "on"
            if not self.radio:
                for c in self.conns.values():
                    if c["type"] == "802-11-wireless":
                        c["active"] = False
            return 0, ""
        if a[:3] == ["-t", "-f", "NAME,UUID,TYPE,DEVICE,AUTOCONNECT,ACTIVE"]:
            return 0, "".join(f"{n}:{c['uuid']}:{c['type']}:{'wlan0' if c['active'] and c['type'] == '802-11-wireless' else ''}:"
                              f"{'yes' if c['auto'] else 'no'}:{'yes' if c['active'] else 'no'}\n"
                              for n, c in self.conns.items())
        if a[:3] == ["-t", "-f", "802-11-wireless.mode,802-11-wireless.ssid"]:
            _, c = self._by(a[-1])
            return 0, f"802-11-wireless.mode:{c['mode']}\n802-11-wireless.ssid:{c['ssid']}\n"
        if a[:3] == ["-t", "-f", "DEVICE,TYPE,STATE,CONNECTION"]:
            active = next((n for n, c in self.conns.items() if c["active"] and c["type"] == "802-11-wireless"), "")
            return 0, f"eth0:ethernet:connected:Wired connection 1\nwlan0:wifi:{'connected' if active else 'disconnected'}:{active}\n"
        if a[:3] == ["-t", "-f", "IP4.ADDRESS"]:
            return 0, "IP4.ADDRESS[1]:192.168.1.50/24\n"
        if a[:3] == ["-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY,CHAN"]:
            return 0, "*:HomeNet:80:WPA2:6\n :Cafe\\:Guest:40:--:11\n :HomeNet:50:WPA2:1\n ::30:WPA2:3\n"
        if a[:2] == ["connection", "modify"]:
            _, c = self._by(a[2])
            for k, v in zip(a[3::2], a[4::2]):
                if k == "connection.autoconnect":
                    c["auto"] = v == "yes"
                if k == "802-11-wireless.mode":
                    c["mode"] = v
                if k == "802-11-wireless.ssid":
                    c["ssid"] = v
            return 0, ""
        if a[:2] == ["connection", "up"]:
            name, c = self._by(a[2])
            if c["type"] == "802-11-wireless":
                for other in self.conns.values():
                    if other["type"] == "802-11-wireless":
                        other["active"] = False
            c["active"] = True
            return 0, "Connection successfully activated\n"
        if a[:2] == ["connection", "down"]:
            _, c = self._by(a[2])
            c["active"] = False
            return 0, ""
        if a[:2] == ["connection", "delete"]:
            name, _ = self._by(a[2])
            del self.conns[name]
            return 0, ""
        if a[:2] == ["connection", "add"]:
            name = a[a.index("con-name") + 1]
            self.conns[name] = {"uuid": "u-" + name, "type": "802-11-wireless", "auto": False, "active": False,
                                "mode": a[a.index("802-11-wireless.mode") + 1],
                                "ssid": a[a.index("802-11-wireless.ssid") + 1]}
            return 0, ""
        if a[:4] == ["connection", "import", "type", "wireguard"]:
            path = a[5]
            name = os.path.basename(path)[:-5]
            self.imported = open(path).read()
            self.imported_mode = stat.S_IMODE(os.stat(path).st_mode)
            self.conns[name] = {"uuid": "u-" + name, "type": "wireguard", "auto": True, "active": False}
            return 0, ""
        if a[:3] == ["device", "wifi", "connect"]:
            ssid = a[3]
            for c in self.conns.values():
                if c["type"] == "802-11-wireless":
                    c["active"] = False
            self.conns[ssid] = {"uuid": "u-" + ssid, "type": "802-11-wireless", "auto": True, "active": True,
                                "mode": "infrastructure", "ssid": ssid}
            return 0, ""
        if a[:2] == ["device", "connect"]:
            for c in self.conns.values():
                if c["type"] == "802-11-wireless" and c["auto"] and c["mode"] != "ap":
                    c["active"] = True
                    break
            return 0, ""
        if a[:3] == ["-t", "-f", "ipv4.addresses,wireguard.listen-port,wireguard.peer-routes,connection.interface-name"]:
            name, _ = self._by(a[-1])
            return 0, f"ipv4.addresses:10.99.1.2/30\nwireguard.listen-port:0\nwireguard.peer-routes:no\nconnection.interface-name:{name}\n"
        if a[:2] == ["-g", "wireguard.peer-routes"]:
            return 0, "no\n"
        raise AssertionError(f"unexpected nmcli {a}")

    def ran(self, *prefix):
        return any(c[1:1 + len(prefix)] == list(prefix) for c in self.calls if c[0] == "nmcli")


def run(coro):
    return asyncio.run(coro)


# --- parsing ---------------------------------------------------------------------

def test_split_terse_handles_escaped_colons():
    assert split_terse(r"*:Cafe\:Guest:40") == ["*", "Cafe:Guest", "40"]
    assert split_terse(r"a\\b:c") == ["a\\b", "c"]


def test_wireguard_keys_and_config_round_trip():
    priv = generate_wg_private_key()
    pub = wg_public_key(priv)
    assert len(pub) == 44 and pub != priv and wg_public_key(priv) == pub
    tunnel = {"private_key": priv, "addresses": "10.99.1.2/30, fe80::2/64", "listen_port": "",
              "peers": [{"public_key": pub, "endpoint": "hub.example.org:51821", "allowed_ips": "0.0.0.0/0, ::/0",
                         "keepalive": "25", "preshared_key": ""}]}
    conf = build_wg_conf(tunnel)
    assert "PersistentKeepalive = 25" in conf and "ListenPort" not in conf
    back = parse_wg_conf(conf)
    assert back["private_key"] == priv and back["addresses"] == "10.99.1.2/30, fe80::2/64"
    assert back["peers"][0]["endpoint"] == "hub.example.org:51821"


def test_wireguard_known_public_key():
    # A pair made with `wg genkey` / `wg pubkey` (WireGuard's own tools).
    assert wg_public_key("iEqpLv5KWxVnF3TLWPU02OZ2w6qy89Eox2mtw28mlmY=") == "yP/Xn7WawR0EWJxNsXSKF+DhDjHFxXQfl94whdrmMD0="


@pytest.mark.parametrize("field,value", [
    ("private_key", "not-a-key"),
    ("addresses", "10.99.1.2/30\nPostUp = rm -rf /"),
    ("addresses", ""),
    ("listen_port", "99999"),
])
def test_bad_tunnel_fields_are_rejected(field, value):
    priv = generate_wg_private_key()
    tunnel = {"private_key": priv, "addresses": "10.99.1.2/30", "peers": []}
    tunnel[field] = value
    with pytest.raises(NetworkError):
        build_wg_conf(tunnel)


def test_endpoint_cannot_smuggle_extra_config_lines():
    priv = generate_wg_private_key()
    with pytest.raises(NetworkError):
        build_wg_conf({"private_key": priv, "addresses": "10.0.0.2/32",
                       "peers": [{"public_key": wg_public_key(priv), "allowed_ips": "0.0.0.0/0",
                                  "endpoint": "x:1\nPostUp = id"}]})


def test_parse_wg_dump():
    out = ("wg0\tPRIV\tPUBKEY=\t51820\toff\n"
           "wg0\tPEER=\t(none)\t203.0.113.5:51821\t10.99.0.0/16\t0\t1024\t2048\t25\n")
    d = parse_wg_dump(out)
    assert d["wg0"]["public_key"] == "PUBKEY=" and d["wg0"]["listen_port"] == 51820
    peer = d["wg0"]["peers"]["PEER="]
    assert peer["endpoint"] == "203.0.113.5:51821" and peer["handshake_age_s"] is None and peer["tx_bytes"] == 2048


# --- Wi-Fi -------------------------------------------------------------------------

def test_wifi_status_and_scan():
    fake = FakeNmcli()
    m = NetworkManager(fake)
    s = run(m.wifi_status())
    assert s["mode"] == "client" and s["ssid"] == "HomeNet" and s["addresses"] == ["192.168.1.50/24"]
    nets = run(m.scan())
    assert [n["ssid"] for n in nets] == ["HomeNet", "Cafe:Guest"]       # strongest per name, hidden dropped
    assert nets[1]["security"] is None


def test_access_point_setup_and_switch(tmp_path):
    async def go():
        fake = FakeNmcli()
        m = NetworkManager(fake)
        with pytest.raises(NetworkError, match="set up the access point first"):
            await m.set_mode("ap")
        with pytest.raises(NetworkError, match="8-63"):
            await m.configure_ap("Outpost", "short")
        await m.configure_ap("Outpost", "correcthorse", channel=6)
        assert fake.ran("connection", "add") and fake.conns[AP_CONNECTION]["mode"] == "ap"
        await m.set_mode("ap")
        s = await m.wifi_status()
        assert s["mode"] == "ap" and s["ssid"] == "Outpost"
        assert not fake.conns["HomeNet"]["auto"]          # saved networks won't grab the radio back
        assert s["revert_in_s"] is not None               # pending until confirmed
        assert m.confirm() is True
        assert (await m.wifi_status())["revert_in_s"] is None
        await m.set_mode("client")
        m.confirm()
        s = await m.wifi_status()
        assert s["mode"] == "client" and s["ssid"] == "HomeNet" and not fake.conns[AP_CONNECTION]["auto"]
    run(go())


def test_unconfirmed_change_is_reverted(monkeypatch):
    monkeypatch.setattr(net, "REVERT_AFTER_S", 0.05)

    async def go():
        fake = FakeNmcli()
        m = NetworkManager(fake)
        await m.configure_ap("Outpost", "correcthorse")
        await m.set_mode("ap")
        assert fake.conns[AP_CONNECTION]["active"]
        await asyncio.sleep(0.3)                          # nobody confirmed
        assert fake.conns["HomeNet"]["active"] and fake.conns["HomeNet"]["auto"]
        assert not fake.conns[AP_CONNECTION]["active"]
    run(go())


def test_failed_change_is_reverted_straight_away():
    async def go():
        fake = FakeNmcli()
        m = NetworkManager(fake)
        fake.fail["device wifi"] = "Error: Connection activation failed: Secrets were required"
        with pytest.raises(NetworkError, match="Secrets"):
            await m.connect("Cafe", "wrongpassword")
        assert fake.conns["HomeNet"]["active"] and m.revert_at is None
    run(go())


def test_wifi_off_and_forget():
    async def go():
        fake = FakeNmcli()
        m = NetworkManager(fake)
        await m.set_mode("off")
        m.confirm()
        assert (await m.wifi_status())["mode"] == "off"
        await m.forget("HomeNet")
        assert "HomeNet" not in fake.conns
        with pytest.raises(NetworkError):
            await m.forget(AP_CONNECTION)
    run(go())


def test_permission_errors_point_to_the_readme():
    async def go():
        fake = FakeNmcli()
        fake.fail["radio wifi"] = "Error: Insufficient privileges."
        with pytest.raises(NetworkError, match="Network settings"):
            await NetworkManager(fake).set_mode("off")
    run(go())


# --- WireGuard ---------------------------------------------------------------------

def test_save_edit_and_delete_tunnel(tmp_path):
    async def go():
        fake = FakeNmcli()
        m = NetworkManager(fake, tunnel_dir=str(tmp_path / "wg"))
        priv = generate_wg_private_key()
        peer_pub = wg_public_key(generate_wg_private_key())
        tunnel = {"private_key": priv, "addresses": "10.99.1.2/30", "peer_routes": False,
                  "peers": [{"public_key": peer_pub, "endpoint": "hub:51821", "allowed_ips": "0.0.0.0/0"}]}
        r = await m.save_tunnel("wg-hub", tunnel)
        assert r["public_key"] == wg_public_key(priv)
        assert "Endpoint = hub:51821" in fake.imported and fake.imported_mode == 0o600
        assert fake.ran("connection", "modify", "wg-hub", "connection.autoconnect", "yes",
                        "wireguard.peer-routes", "no")
        assert fake.ran("connection", "up", "wg-hub")
        saved = tmp_path / "wg" / "wg-hub.conf"
        assert stat.S_IMODE(saved.stat().st_mode) == 0o600
        back = await m.tunnel_config("wg-hub")
        assert back["private_key"] == priv and back["peer_routes"] is False

        await m.save_tunnel("wg-hub", dict(tunnel, addresses="10.99.1.6/30"))     # edit replaces it
        assert fake.ran("connection", "delete", "u-wg-hub") and "10.99.1.6/30" in fake.imported

        status = await m.wireguard_status()
        assert status["tunnels"][0]["name"] == "wg-hub" and status["tunnels"][0]["editable"]
        await m.tunnel_action("wg-hub", "delete")
        assert not saved.exists() and "wg-hub" not in fake.conns
    run(go())


def test_bad_tunnel_name_rejected(tmp_path):
    async def go():
        m = NetworkManager(FakeNmcli(), tunnel_dir=str(tmp_path))
        with pytest.raises(NetworkError, match="tunnel name"):
            await m.save_tunnel("../etc/x", {"private_key": generate_wg_private_key(), "addresses": "10.0.0.1/32"})
    run(go())


def test_wireguard_status_includes_live_handshakes_and_unmanaged_tunnels(tmp_path):
    async def go():
        fake = FakeNmcli()
        fake.conns["wg-hub"] = {"uuid": "u-wg-hub", "type": "wireguard", "auto": True, "active": True}
        fake.wg_dump = ("wg-hub\tP\tHUBPUB=\t0\toff\n"
                        "wg-hub\tPEER=\t(none)\t1.2.3.4:51821\t0.0.0.0/0\t1\t10\t20\t25\n"
                        "wg-other\tP\tOTHER=\t51820\toff\n")
        s = await NetworkManager(fake, tunnel_dir=str(tmp_path)).wireguard_status()
        t = s["tunnels"][0]
        assert t["up"] and t["public_key"] == "HUBPUB=" and t["peers"][0]["endpoint"] == "1.2.3.4:51821"
        assert not t["editable"]                                   # not saved from Outpost
        assert [u["interface"] for u in s["unmanaged"]] == ["wg-other"] and s["live_status"]
    run(go())


# --- web UI -------------------------------------------------------------------------

def _webui(fake, tmp_path, token=None, host="127.0.0.1") -> WebUI:
    config = OutpostConfig(sender_id=1, listen_port=0, mesh_key=generate_channel_key(),
                           channels={1: Channel(id=1, name="ops", key=generate_channel_key())},
                           enable_mdns=False, enable_pex=False)
    ui = WebUI(OutpostClient(config, "ops"), host=host, token=token)
    ui.network = NetworkManager(fake, tunnel_dir=str(tmp_path))
    return ui


def test_web_ui_network_flow(tmp_path):
    async def go():
        fake = FakeNmcli()
        async with TestClient(TestServer(_webui(fake, tmp_path).app)) as tc:
            s = await (await tc.get("/api/network")).json()
            assert s["available"] and s["wifi"]["ssid"] == "HomeNet" and not s["needs_token"]
            assert (await tc.post("/api/network/wifi/ap", json={"ssid": "Outpost", "password": "correcthorse"})).status == 200
            assert (await tc.post("/api/network/wifi/mode", json={"mode": "ap"})).status == 200
            assert (await (await tc.post("/api/network/confirm")).json())["confirmed"] is True
            bad = await tc.post("/api/network/wifi/mode", json={"mode": "monitor"})
            assert bad.status == 400
            keys = await (await tc.post("/api/network/wireguard/genkey")).json()
            r = await tc.post("/api/network/wireguard/save", json={"name": "wg0", "up": False, "tunnel": {
                "private_key": keys["private_key"], "addresses": "10.99.1.2/30", "peers": []}})
            assert (await r.json())["public_key"] == keys["public_key"]
            assert (await (await tc.get("/api/network/wireguard/wg0")).json())["private_key"] == keys["private_key"]
            assert (await tc.get("/network")).status == 200
    run(go())


def test_network_changes_need_a_token_when_exposed(tmp_path):
    async def go():
        fake = FakeNmcli()
        async with TestClient(TestServer(_webui(fake, tmp_path, host="0.0.0.0").app)) as tc:
            s = await (await tc.get("/api/network")).json()
            assert s["needs_token"] and s["wifi"]["ssid"] == "HomeNet"     # status still visible
            assert (await tc.post("/api/network/wifi/mode", json={"mode": "off"})).status == 403
            assert (await tc.post("/api/network/wireguard/genkey")).status == 403
            assert fake.radio                                             # nothing changed
        async with TestClient(TestServer(_webui(fake, tmp_path, host="0.0.0.0", token="s3cret").app)) as tc:
            ok = await tc.post("/api/network/wifi/mode", json={"mode": "off"},
                               headers={"Authorization": "Bearer s3cret"})
            assert ok.status == 200
    run(go())
