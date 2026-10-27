"""Network settings for the web UI's Wi-Fi and WireGuard pages, through
NetworkManager (`nmcli`), the standard on Raspberry Pi OS Bookworm.

Wi-Fi can be a client (join a network), an access point (other devices
join the Pi; NetworkManager's "shared" mode gives them addresses and
internet through the Pi's other connections), or off. WireGuard tunnels are
kept as NetworkManager connections, imported from wg-quick style config
text that Outpost builds from the page's fields.

Changing network settings needs permission: the user Outpost runs as must be
allowed by polkit to modify NetworkManager's system connections, and to see
tunnel handshakes it needs `sudo wg show` - see README "Network settings".

A Wi-Fi change that cuts the browser's connection to the Pi is undone after
REVERT_AFTER_S unless confirmed (the page confirms automatically when it can
still reach the Pi) - see NetworkManager.confirm.
"""
from __future__ import annotations
import asyncio
import base64
import ipaddress
import logging
import os
import re
import shutil
import sys
import tempfile
import time

log = logging.getLogger(__name__)

AP_CONNECTION = "outpost-ap"
REVERT_AFTER_S = 120.0
_TIMEOUT_S = 20.0
_CONNECT_TIMEOUT_S = 45.0
_WG_NAME = re.compile(r"^[A-Za-z0-9_=+.-]{1,15}$")
_KEY = re.compile(r"^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw480]=$")


class NetworkError(Exception):
    pass


def split_terse(line: str) -> list[str]:
    """Fields of one line of `nmcli -t` output: ':'-separated, with ':' and
    '\\' inside values escaped by a backslash."""
    fields, cur, i = [], [], 0
    while i < len(line):
        c = line[i]
        if c == "\\" and i + 1 < len(line):
            cur.append(line[i + 1])
            i += 2
            continue
        if c == ":":
            fields.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    fields.append("".join(cur))
    return fields


def _rows(output: str) -> list[list[str]]:
    return [split_terse(line) for line in output.splitlines() if line.strip()]


# --- WireGuard keys and config text ------------------------------------------------

def generate_wg_private_key() -> str:
    from nacl.public import PrivateKey
    return base64.b64encode(bytes(PrivateKey.generate())).decode()


def wg_public_key(private_key: str) -> str:
    from nacl.public import PrivateKey
    raw = base64.b64decode(private_key)
    if len(raw) != 32:
        raise NetworkError("private key must be 32 bytes, base64")
    # WireGuard clamps the scalar; Curve25519 scalar multiplication does too.
    return base64.b64encode(bytes(PrivateKey(raw).public_key)).decode()


def _check_key(value: str, what: str) -> str:
    value = (value or "").strip()
    if not _KEY.match(value):
        raise NetworkError(f"{what} must be a WireGuard key (44 characters of base64)")
    return value


def _check_prefixes(value: str, what: str) -> str:
    items = [v.strip() for v in (value or "").split(",") if v.strip()]
    if not items:
        raise NetworkError(f"{what} is required")
    for item in items:
        try:
            ipaddress.ip_interface(item)
        except ValueError:
            raise NetworkError(f"{what}: {item!r} isn't an address like 10.99.1.2/30")
    return ", ".join(items)


def _check_endpoint(value: str) -> str:
    value = (value or "").strip()
    if not value:
        return ""
    host, sep, port = value.rpartition(":")
    if not sep or not host or not port.isdigit() or not 0 < int(port) < 65536:
        raise NetworkError(f"endpoint {value!r} must be host:port")
    if re.search(r"[\s\n]", value):
        raise NetworkError("endpoint can't contain spaces")
    return value


def build_wg_conf(tunnel: dict) -> str:
    """wg-quick style config text from the page's tunnel fields (validated
    here - everything ends up in a config file)."""
    lines = ["[Interface]", f"PrivateKey = {_check_key(tunnel.get('private_key'), 'private key')}",
             f"Address = {_check_prefixes(tunnel.get('addresses'), 'addresses')}"]
    port = tunnel.get("listen_port")
    if port not in (None, ""):
        if not str(port).isdigit() or not 0 < int(port) < 65536:
            raise NetworkError("listen port must be 1-65535")
        lines.append(f"ListenPort = {int(port)}")
    peers = tunnel.get("peers") or []
    if not isinstance(peers, list):
        raise NetworkError("peers must be a list")
    for peer in peers:
        lines += ["", "[Peer]", f"PublicKey = {_check_key(peer.get('public_key'), 'peer public key')}",
                  f"AllowedIPs = {_check_prefixes(peer.get('allowed_ips'), 'allowed IPs')}"]
        if peer.get("preshared_key"):
            lines.append(f"PresharedKey = {_check_key(peer['preshared_key'], 'preshared key')}")
        endpoint = _check_endpoint(peer.get("endpoint", ""))
        if endpoint:
            lines.append(f"Endpoint = {endpoint}")
        keepalive = peer.get("keepalive")
        if keepalive not in (None, "", 0, "0"):
            if not str(keepalive).isdigit() or not 0 < int(keepalive) < 65536:
                raise NetworkError("keepalive must be a number of seconds")
            lines.append(f"PersistentKeepalive = {int(keepalive)}")
    return "\n".join(lines) + "\n"


def parse_wg_conf(text: str) -> dict:
    """The page's tunnel fields from wg-quick config text (for importing a
    .conf someone sent you). Unknown keys are ignored."""
    tunnel = {"private_key": "", "addresses": "", "listen_port": "", "peers": []}
    section = None
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith("["):
            section = line.strip("[]").strip().lower()
            if section == "peer":
                tunnel["peers"].append({"public_key": "", "allowed_ips": "", "endpoint": "",
                                        "keepalive": "", "preshared_key": ""})
            continue
        key, _, value = line.partition("=")
        key, value = key.strip().lower(), value.strip()
        if section == "interface":
            if key == "privatekey":
                tunnel["private_key"] = value
            elif key == "address":
                tunnel["addresses"] = ", ".join(filter(None, [tunnel["addresses"], value]))
            elif key == "listenport":
                tunnel["listen_port"] = value
        elif section == "peer" and tunnel["peers"]:
            peer = tunnel["peers"][-1]
            mapping = {"publickey": "public_key", "allowedips": "allowed_ips", "endpoint": "endpoint",
                       "persistentkeepalive": "keepalive", "presharedkey": "preshared_key"}
            if key in mapping:
                peer[mapping[key]] = value
    return tunnel


def parse_wg_dump(output: str) -> dict[str, dict]:
    """`wg show all dump` -> {interface: {"public_key", "listen_port",
    "peers": {public_key: {...}}}}."""
    result: dict[str, dict] = {}
    now = time.time()
    for line in output.splitlines():
        f = line.split("\t")
        if len(f) == 5:      # interface line
            result[f[0]] = {"public_key": f[2], "listen_port": int(f[3]) if f[3].isdigit() else None,
                            "peers": {}}
        elif len(f) == 9 and f[0] in result:
            handshake = int(f[5]) if f[5].isdigit() else 0
            result[f[0]]["peers"][f[1]] = {
                "endpoint": None if f[3] == "(none)" else f[3],
                "allowed_ips": f[4],
                "handshake_age_s": round(now - handshake) if handshake else None,
                "rx_bytes": int(f[6]) if f[6].isdigit() else 0,
                "tx_bytes": int(f[7]) if f[7].isdigit() else 0,
            }
    return result


# --- running commands --------------------------------------------------------------

async def _run_command(args: list[str], timeout: float) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode, out.decode(errors="replace")


class NetworkManager:
    def __init__(self, runner=None, wifi_device: str = "wlan0", tunnel_dir: str | None = None):
        # runner(args, timeout) -> (returncode, output); swapped in tests.
        self._runner = runner or _run_command
        self.wifi_device = wifi_device
        # Outpost's own copy of each tunnel's settings (<name>.conf, wg-quick
        # format, owner-only) - NetworkManager can't export WireGuard
        # connections back, so this is what the page edits.
        self.tunnel_dir = tunnel_dir or os.path.expanduser("~/.config/outpost/wireguard")
        self._lock = asyncio.Lock()
        self._revert_task: asyncio.Task | None = None
        self.revert_at: float | None = None

    def unavailable_reason(self) -> str | None:
        if self._runner is not _run_command:
            return None
        if not sys.platform.startswith("linux"):
            return "network settings are only supported on Linux (e.g. a Raspberry Pi)"
        if shutil.which("nmcli") is None:
            return "NetworkManager (nmcli) isn't installed - this page needs it"
        return None

    async def _nmcli(self, *args: str, timeout: float = _TIMEOUT_S, check: bool = True) -> str:
        try:
            code, out = await self._runner(["nmcli", *args], timeout)
        except asyncio.TimeoutError:
            raise NetworkError(f"nmcli {' '.join(args[:2])} timed out")
        except OSError as exc:
            raise NetworkError(f"couldn't run nmcli: {exc}")
        if check and code != 0:
            msg = out.strip().splitlines()[-1] if out.strip() else f"nmcli failed ({code})"
            if "Insufficient privileges" in out or "not authorized" in out.lower():
                msg += " - Outpost isn't allowed to change network settings; see README \"Network settings\""
            raise NetworkError(msg)
        return out

    # --- status -------------------------------------------------------------------

    async def _connections(self) -> list[dict]:
        rows = _rows(await self._nmcli("-t", "-f", "NAME,UUID,TYPE,DEVICE,AUTOCONNECT,ACTIVE",
                                       "connection", "show"))
        return [{"name": r[0], "uuid": r[1], "type": r[2], "device": r[3] or None,
                 "autoconnect": r[4] == "yes", "active": r[5] == "yes"} for r in rows if len(r) >= 6]

    async def _wifi_connections(self) -> list[dict]:
        conns = [c for c in await self._connections() if c["type"] == "802-11-wireless"]
        for c in conns:
            f = _rows(await self._nmcli("-t", "-f", "802-11-wireless.mode,802-11-wireless.ssid",
                                        "connection", "show", c["uuid"]))
            values = {r[0]: r[1] for r in f if len(r) >= 2}
            c["mode"] = "ap" if values.get("802-11-wireless.mode") == "ap" else "client"
            c["ssid"] = values.get("802-11-wireless.ssid", c["name"])
        return conns

    async def wifi_status(self) -> dict:
        radio = (await self._nmcli("radio", "wifi")).strip()
        devices = {r[0]: r for r in _rows(await self._nmcli("-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device"))}
        dev = devices.get(self.wifi_device)
        conns = await self._wifi_connections()
        active = next((c for c in conns if c["active"]), None)
        if radio != "enabled":
            mode = "off"
        elif active is not None:
            mode = active["mode"]
        else:
            mode = "client"
        addresses = []
        if dev is not None and active is not None:
            show = _rows(await self._nmcli("-t", "-f", "IP4.ADDRESS", "device", "show", self.wifi_device,
                                           check=False))
            addresses = [r[1] for r in show if len(r) >= 2 and r[1]]
        return {
            "device": self.wifi_device if dev is not None else None,
            "radio": radio == "enabled",
            "mode": mode,
            "state": dev[2] if dev is not None else "missing",
            "active": active["name"] if active else None,
            "ssid": active["ssid"] if active else None,
            "addresses": addresses,
            "saved": [{"name": c["name"], "ssid": c["ssid"], "autoconnect": c["autoconnect"],
                       "active": c["active"]} for c in conns if c["mode"] == "client"],
            "ap": next(({"ssid": c["ssid"], "active": c["active"]} for c in conns if c["name"] == AP_CONNECTION),
                       None),
            "revert_in_s": round(self.revert_at - time.monotonic()) if self.revert_at else None,
        }

    async def scan(self) -> list[dict]:
        out = await self._nmcli("-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY,CHAN", "device", "wifi", "list",
                                "ifname", self.wifi_device, "--rescan", "yes", timeout=30)
        best: dict[str, dict] = {}
        for r in _rows(out):
            if len(r) < 5 or not r[1]:
                continue
            net = {"ssid": r[1], "signal": int(r[2]) if r[2].isdigit() else 0,
                   "security": r[3] if r[3] not in ("", "--") else None,
                   "channel": r[4], "in_use": r[0] == "*"}
            if r[1] not in best or net["signal"] > best[r[1]]["signal"]:
                best[r[1]] = net
        return sorted(best.values(), key=lambda n: (-n["in_use"], -n["signal"]))

    # --- Wi-Fi changes (revertible) ---------------------------------------------------

    async def _snapshot(self) -> dict:
        conns = await self._wifi_connections()
        return {"radio": (await self._nmcli("radio", "wifi")).strip() == "enabled",
                "autoconnect": {c["uuid"]: c["autoconnect"] for c in conns},
                "active": next((c["uuid"] for c in conns if c["active"]), None)}

    async def _restore(self, snap: dict) -> None:
        log.warning("network: Wi-Fi change wasn't confirmed - reverting it")
        try:
            await self._nmcli("radio", "wifi", "on" if snap["radio"] else "off", check=False)
            current = {c["uuid"]: c for c in await self._wifi_connections()}
            for uuid, auto in snap["autoconnect"].items():
                if uuid in current:
                    await self._nmcli("connection", "modify", uuid, "connection.autoconnect",
                                      "yes" if auto else "no", check=False)
            for uuid, c in current.items():
                if c["active"] and uuid != snap["active"]:
                    await self._nmcli("connection", "down", uuid, check=False)
            if snap["active"] and snap["active"] in current and snap["radio"]:
                await self._nmcli("connection", "up", snap["active"], timeout=_CONNECT_TIMEOUT_S, check=False)
        except NetworkError as exc:
            log.error("network: reverting failed: %s", exc)

    async def _revertible(self, change) -> None:
        """Apply `change` (a coroutine function), then revert it after
        REVERT_AFTER_S unless confirm() is called. A failed change is
        reverted straight away."""
        async with self._lock:
            snap = await self._snapshot()
            self.confirm()  # a newer change replaces any pending revert
            try:
                await change()
            except NetworkError:
                await self._restore(snap)
                raise

            async def revert_later():
                try:
                    await asyncio.sleep(REVERT_AFTER_S)
                    async with self._lock:
                        await self._restore(snap)
                finally:
                    self.revert_at = None
            self.revert_at = time.monotonic() + REVERT_AFTER_S
            self._revert_task = asyncio.ensure_future(revert_later())

    def confirm(self) -> bool:
        """Keep the latest Wi-Fi change. True if one was pending."""
        task, self._revert_task = self._revert_task, None
        self.revert_at = None
        if task is not None and not task.done():
            task.cancel()
            return True
        return False

    async def _set_client_autoconnect(self, on: bool) -> None:
        for c in await self._wifi_connections():
            if c["mode"] == "client" and c["autoconnect"] != on:
                await self._nmcli("connection", "modify", c["uuid"], "connection.autoconnect", "yes" if on else "no")

    async def set_mode(self, mode: str) -> None:
        """"client": saved networks connect automatically, the access point
        is off. "ap": the access point is on (it must be set up first).
        "off": Wi-Fi radio off."""
        if mode not in ("client", "ap", "off"):
            raise NetworkError("mode must be client, ap or off")

        async def change():
            conns = {c["name"]: c for c in await self._wifi_connections()}
            if mode == "off":
                await self._nmcli("radio", "wifi", "off")
                return
            await self._nmcli("radio", "wifi", "on")
            if mode == "ap":
                if AP_CONNECTION not in conns:
                    raise NetworkError("set up the access point first")
                await self._set_client_autoconnect(False)
                await self._nmcli("connection", "modify", AP_CONNECTION, "connection.autoconnect", "yes")
                await self._nmcli("connection", "up", AP_CONNECTION, timeout=_CONNECT_TIMEOUT_S)
            else:
                if AP_CONNECTION in conns:
                    await self._nmcli("connection", "modify", AP_CONNECTION, "connection.autoconnect", "no")
                    if conns[AP_CONNECTION]["active"]:
                        await self._nmcli("connection", "down", AP_CONNECTION)
                await self._set_client_autoconnect(True)
                await self._nmcli("device", "connect", self.wifi_device, timeout=_CONNECT_TIMEOUT_S, check=False)
        await self._revertible(change)

    async def connect(self, ssid: str, password: str | None) -> None:
        """Join a network as a client (and save it)."""
        ssid = (ssid or "").strip()
        if not ssid or len(ssid) > 32:
            raise NetworkError("network name must be 1-32 characters")
        if password is not None and password != "" and not 8 <= len(password) <= 63:
            raise NetworkError("Wi-Fi passwords are 8-63 characters")

        async def change():
            await self._nmcli("radio", "wifi", "on")
            conns = {c["name"]: c for c in await self._wifi_connections()}
            if AP_CONNECTION in conns:
                await self._nmcli("connection", "modify", AP_CONNECTION, "connection.autoconnect", "no")
            args = ["device", "wifi", "connect", ssid, "ifname", self.wifi_device]
            if password:
                args += ["password", password]
            await self._nmcli(*args, timeout=_CONNECT_TIMEOUT_S)
        await self._revertible(change)

    async def forget(self, name: str) -> None:
        if name == AP_CONNECTION:
            raise NetworkError("that's the access point - turn it off with Client mode instead")
        conns = {c["name"]: c for c in await self._wifi_connections()}
        if name not in conns:
            raise NetworkError(f"no saved network {name!r}")
        async with self._lock:
            await self._nmcli("connection", "delete", conns[name]["uuid"])

    async def configure_ap(self, ssid: str, password: str, channel: int | None = None) -> None:
        """Create or update the access point's settings (WPA2). Doesn't
        switch modes - see set_mode("ap")."""
        ssid = (ssid or "").strip()
        if not ssid or len(ssid) > 32:
            raise NetworkError("network name must be 1-32 characters")
        if not password or not 8 <= len(password) <= 63:
            raise NetworkError("the access point needs a password of 8-63 characters")
        if channel not in (None, "", 0) and (not str(channel).isdigit() or not 1 <= int(channel) <= 13):
            raise NetworkError("2.4 GHz channel must be 1-13")
        settings = ["802-11-wireless.mode", "ap", "802-11-wireless.band", "bg",
                    "802-11-wireless.ssid", ssid,
                    "802-11-wireless.channel", str(int(channel)) if channel else "0",
                    "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.proto", "rsn",
                    "wifi-sec.pairwise", "ccmp", "wifi-sec.group", "ccmp", "wifi-sec.psk", password,
                    "ipv4.method", "shared", "ipv6.method", "disabled"]
        async with self._lock:
            conns = {c["name"] for c in await self._connections()}
            if AP_CONNECTION in conns:
                await self._nmcli("connection", "modify", AP_CONNECTION, *settings)
            else:
                await self._nmcli("connection", "add", "type", "wifi", "ifname", self.wifi_device,
                                  "con-name", AP_CONNECTION, "autoconnect", "no", *settings)

    # --- WireGuard ------------------------------------------------------------------

    async def _wg_runtime(self) -> dict[str, dict]:
        """Live tunnel state (handshakes, traffic) - needs `sudo wg show`."""
        try:
            code, out = await self._runner(["sudo", "-n", "wg", "show", "all", "dump"], _TIMEOUT_S)
        except (OSError, asyncio.TimeoutError):
            return {}
        return parse_wg_dump(out) if code == 0 else {}

    async def wireguard_status(self) -> dict:
        conns = [c for c in await self._connections() if c["type"] == "wireguard"]
        runtime = await self._wg_runtime()
        tunnels = []
        for c in conns:
            f = {r[0]: r[1] for r in _rows(await self._nmcli(
                "-t", "-f", "ipv4.addresses,wireguard.listen-port,wireguard.peer-routes,connection.interface-name",
                "connection", "show", c["uuid"])) if len(r) >= 2}
            iface = f.get("connection.interface-name") or c["name"]
            live = runtime.get(iface)
            tunnels.append({
                "editable": os.path.exists(os.path.join(self.tunnel_dir, f"{c['name']}.conf")),
                "name": c["name"], "interface": iface, "up": c["active"], "autoconnect": c["autoconnect"],
                "addresses": f.get("ipv4.addresses", ""),
                "listen_port": f.get("wireguard.listen-port") or None,
                "peer_routes": f.get("wireguard.peer-routes", "yes") != "no",
                "public_key": live["public_key"] if live else None,
                "peers": [{"public_key": k, **v} for k, v in live["peers"].items()] if live else [],
            })
        managed = {t["interface"] for t in tunnels}
        others = [{"interface": i, "public_key": r["public_key"],
                   "peers": [{"public_key": k, **v} for k, v in r["peers"].items()]}
                  for i, r in runtime.items() if i not in managed]
        return {"tunnels": tunnels, "unmanaged": others, "live_status": bool(runtime) or not conns}

    async def save_tunnel(self, name: str, tunnel: dict, up: bool = True) -> dict:
        """Create or replace tunnel `name` (also its interface name) from the
        page's fields. Returns {"public_key"} for the other end."""
        if not _WG_NAME.match(name or ""):
            raise NetworkError("tunnel name must be 1-15 letters, digits or _=+.- (it's the interface name, e.g. wg0)")
        conf = build_wg_conf(tunnel)
        public_key = wg_public_key(tunnel["private_key"])
        async with self._lock:
            existing = {c["name"]: c for c in await self._connections()}
            if name in existing and existing[name]["type"] != "wireguard":
                raise NetworkError(f"{name!r} is already used by a non-WireGuard connection")
            os.makedirs(self.tunnel_dir, mode=0o700, exist_ok=True)
            path = os.path.join(self.tunnel_dir, f"{name}.conf")
            tmp_path = path + ".tmp"
            fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(conf)
            # NetworkManager names the imported connection after the file.
            with tempfile.TemporaryDirectory() as tmp:
                import_path = os.path.join(tmp, f"{name}.conf")
                shutil.copyfile(tmp_path, import_path)
                os.chmod(import_path, 0o600)
                if name in existing:
                    await self._nmcli("connection", "delete", existing[name]["uuid"])
                await self._nmcli("connection", "import", "type", "wireguard", "file", import_path)
            os.replace(tmp_path, path)
            await self._nmcli("connection", "modify", name,
                              "connection.autoconnect", "yes" if up else "no",
                              "wireguard.peer-routes", "yes" if tunnel.get("peer_routes", True) else "no")
            if up:
                await self._nmcli("connection", "up", name, timeout=_CONNECT_TIMEOUT_S)
            else:
                await self._nmcli("connection", "down", name, check=False)
        return {"public_key": public_key}

    def _tunnel_file(self, name: str) -> str:
        if not _WG_NAME.match(name or ""):
            raise NetworkError("invalid tunnel name")
        return os.path.join(self.tunnel_dir, f"{name}.conf")

    async def tunnel_config(self, name: str) -> dict:
        """The page's fields for a tunnel saved from Outpost, to edit it
        (includes the private key - only served to authorized web UI users)."""
        path = self._tunnel_file(name)
        if not os.path.exists(path):
            raise NetworkError(f"{name!r} wasn't set up from Outpost, so it can't be edited here - "
                               "import its config to take it over")
        with open(path) as f:
            tunnel = parse_wg_conf(f.read())
        conns = {c["name"]: c for c in await self._connections() if c["type"] == "wireguard"}
        if name in conns:
            tunnel["peer_routes"] = (await self._nmcli("-g", "wireguard.peer-routes", "connection", "show",
                                                       conns[name]["uuid"])).strip() != "no"
        return tunnel

    async def tunnel_action(self, name: str, action: str) -> None:
        conns = {c["name"]: c for c in await self._connections() if c["type"] == "wireguard"}
        if name not in conns:
            raise NetworkError(f"no tunnel {name!r}")
        uuid = conns[name]["uuid"]
        async with self._lock:
            if action == "up":
                await self._nmcli("connection", "modify", uuid, "connection.autoconnect", "yes")
                await self._nmcli("connection", "up", uuid, timeout=_CONNECT_TIMEOUT_S)
            elif action == "down":
                await self._nmcli("connection", "modify", uuid, "connection.autoconnect", "no")
                await self._nmcli("connection", "down", uuid)
            elif action == "delete":
                await self._nmcli("connection", "delete", uuid)
                try:
                    os.remove(self._tunnel_file(name))
                except FileNotFoundError:
                    pass
            else:
                raise NetworkError(f"unknown action {action!r}")

    def stop(self) -> None:
        # A pending revert is dropped on shutdown: the change stays.
        self.confirm()
