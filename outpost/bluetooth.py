"""Bluetooth management for the web UI's Bluetooth page: power on/off, scan,
pair/unpair, connect/disconnect, and device status/battery level.

Drives BlueZ through its standard `bluetoothctl` command (installed with
Raspberry Pi OS / the `bluez` package), one command per call - no extra
Python dependency. Linux only.

Pairing registers a NoInputNoOutput agent, which suits PTT buttons, speaker
mics and headsets ("just works" pairing). Devices that need a PIN typed in
or a code confirmed have to be paired with bluetoothctl directly.
"""
from __future__ import annotations
import asyncio
import logging
import re
import shutil
import sys
import time

log = logging.getLogger(__name__)

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]|\x01|\x02")
_ADDRESS = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$")
_DEVICE_LINE = re.compile(r"^Device ([0-9A-F:]{17}) ?(.*)$")
_BATTERY = re.compile(r"\((\d+)\)")
_FIELD_KEY = re.compile(r"^[A-Z][A-Za-z]*( Percentage)?$")

SCAN_SECONDS = 20
# Cap on devices shown/queried - a scan in a busy place can find dozens.
MAX_DEVICES = 40
_COMMAND_TIMEOUT_S = 10.0
# state() runs bluetoothctl once per device, so reuse a recent result when
# the page polls (or several pages are open).
_STATE_CACHE_S = 2.0
_PAIR_TIMEOUT_S = 30.0
# How often to look for paired devices that should be connected but aren't.
RECONNECT_INTERVAL_S = 15.0


def valid_address(address) -> bool:
    return isinstance(address, str) and bool(_ADDRESS.match(address.upper()))


def _clean(output: str) -> list[str]:
    return [_ANSI.sub("", line).strip() for line in output.splitlines()]


def parse_fields(output: str) -> dict[str, str]:
    """`Key: value` lines of `bluetoothctl show` / `info` output. Repeated
    keys (e.g. UUID) keep their first value."""
    fields: dict[str, str] = {}
    for line in _clean(output):
        key, sep, value = line.partition(": ")
        if sep and _FIELD_KEY.match(key):
            fields.setdefault(key, value.strip())
    return fields


def parse_devices(output: str) -> list[tuple[str, str]]:
    """(address, name) per `Device XX:XX:... name` line."""
    devices = []
    for line in _clean(output):
        m = _DEVICE_LINE.match(line)
        if m:
            devices.append((m.group(1), m.group(2).strip()))
    return devices


def parse_device_info(address: str, name: str, output: str) -> dict:
    f = parse_fields(output)
    battery = None
    if "Battery Percentage" in f:
        m = _BATTERY.search(f["Battery Percentage"])
        if m:
            battery = int(m.group(1))
    rssi = f.get("RSSI")
    return {
        "address": address,
        "name": f.get("Alias") or f.get("Name") or name or address,
        "icon": f.get("Icon"),
        "paired": f.get("Paired") == "yes",
        "trusted": f.get("Trusted") == "yes",
        "connected": f.get("Connected") == "yes",
        "battery": battery,
        "rssi": int(rssi.split()[0]) if rssi and rssi.split()[0].lstrip("-").isdigit() else None,
    }


def _failure(output: str) -> str | None:
    """The error line of a failed bluetoothctl command, if any."""
    for line in _clean(output):
        if (line.startswith("Failed") or "org.bluez.Error" in line
                or "not available" in line or line.startswith("No default controller")):
            return line
    return None


async def _run_bluetoothctl(args: list[str], timeout: float) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "bluetoothctl", *args,
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    return proc.returncode, out.decode(errors="replace")


class BluetoothError(Exception):
    pass


class BluetoothManager:
    def __init__(self, runner=None):
        # runner(args, timeout) -> (returncode, output); swapped in tests.
        self._runner = runner or _run_bluetoothctl
        self._lock = asyncio.Lock()     # one pair/remove/etc. at a time
        self._scan_task: asyncio.Task | None = None
        self.scan_until = 0.0
        self._state_cache: tuple[float, dict] | None = None
        # Devices disconnected or unpaired on purpose - auto-reconnect leaves
        # them alone until they're connected again.
        self._hold_off: set[str] = set()

    def unavailable_reason(self) -> str | None:
        if self._runner is not _run_bluetoothctl:
            return None
        if not sys.platform.startswith("linux"):
            return "Bluetooth control is only supported on Linux (e.g. a Raspberry Pi)"
        if shutil.which("bluetoothctl") is None:
            return "bluetoothctl isn't installed - run: sudo apt install bluez"
        return None

    async def _run(self, args: list[str], timeout: float = _COMMAND_TIMEOUT_S) -> str:
        try:
            _, out = await self._runner(args, timeout)
        except asyncio.TimeoutError:
            raise BluetoothError(f"bluetoothctl {args[-2] if len(args) > 1 else args[0]} timed out")
        except OSError as exc:
            raise BluetoothError(f"couldn't run bluetoothctl: {exc}")
        return out

    @property
    def scanning(self) -> bool:
        return time.monotonic() < self.scan_until

    async def state(self) -> dict:
        """Adapter power plus every known device (paired first, then
        connected, then the rest by signal strength)."""
        now = time.monotonic()
        if self._state_cache is not None and now - self._state_cache[0] < _STATE_CACHE_S:
            return {**self._state_cache[1], "scanning": self.scanning}
        result = await self._read_state()
        self._state_cache = (time.monotonic(), result)
        return result

    async def _read_state(self) -> dict:
        show = await self._run(["show"])
        if _failure(show):
            return {"adapter": None, "powered": False, "scanning": False, "devices": [],
                    "error": "no Bluetooth adapter found"}
        adapter = parse_fields(show)
        listed = parse_devices(await self._run(["devices"]))[:MAX_DEVICES]
        infos = await asyncio.gather(*(self._run(["info", addr]) for addr, _ in listed),
                                     return_exceptions=True)
        devices = [parse_device_info(addr, name, out)
                   for (addr, name), out in zip(listed, infos) if isinstance(out, str)]
        devices.sort(key=lambda d: (not d["paired"], not d["connected"],
                                    -(d["rssi"] if d["rssi"] is not None else -999), d["name"].lower()))
        return {
            "adapter": adapter.get("Alias") or adapter.get("Name"),
            "powered": adapter.get("Powered") == "yes",
            "scanning": self.scanning,
            "devices": devices,
            "error": None,
        }

    async def set_power(self, on: bool) -> None:
        self._state_cache = None
        async with self._lock:
            out = await self._run(["power", "on" if on else "off"])
        self._state_cache = None
        err = _failure(out)
        if err:
            if "Blocked" in err or "rfkill" in err.lower():
                err += " - Bluetooth is blocked; run: sudo rfkill unblock bluetooth"
            raise BluetoothError(err)

    def start_scan(self, seconds: int = SCAN_SECONDS) -> None:
        """Looks for nearby devices for `seconds` in the background; found
        devices show up in state() as they're discovered."""
        if self.scanning:
            return
        self.scan_until = time.monotonic() + seconds

        async def scan():
            try:
                await self._runner(["--timeout", str(seconds), "scan", "on"], seconds + 5)
            except Exception as exc:
                log.debug("bluetooth scan ended: %s", exc)
            finally:
                self.scan_until = 0.0
        self._scan_task = asyncio.ensure_future(scan())

    async def device_action(self, address: str, action: str) -> str:
        """pair (also trusts and connects, so it reconnects by itself),
        unpair, connect or disconnect. Returns a short result message."""
        if not valid_address(address):
            raise BluetoothError("invalid device address")
        address = address.upper()
        self._state_cache = None
        if action in ("disconnect", "unpair"):
            self._hold_off.add(address)
        elif action in ("connect", "pair"):
            self._hold_off.discard(address)
        async with self._lock:
            if action == "pair":
                # Without "pairable", BlueZ pairs but doesn't keep the keys,
                # so the pairing is lost as soon as the device disconnects.
                await self._run(["pairable", "on"])
                out = await self._run(["--agent", "NoInputNoOutput", "pair", address], _PAIR_TIMEOUT_S)
                err = _failure(out)
                if err and "AlreadyExists" not in err:
                    raise BluetoothError(err)
                await self._run(["trust", address])
                out = await self._run(["connect", address], _PAIR_TIMEOUT_S)
                self._state_cache = None
                err = _failure(out)
                return f"paired - but connecting failed: {err}" if err else "paired and connected"
            commands = {"unpair": "remove", "connect": "connect", "disconnect": "disconnect"}
            if action not in commands:
                raise BluetoothError(f"unknown action {action!r}")
            out = await self._run([commands[action], address], _PAIR_TIMEOUT_S)
        self._state_cache = None
        err = _failure(out)
        if err:
            raise BluetoothError(err)
        return {"unpair": "unpaired", "connect": "connected", "disconnect": "disconnected"}[action]

    async def reconnect_once(self) -> list[str]:
        """Connects paired, trusted devices that aren't connected (and weren't
        disconnected on purpose). Returns the names of those reconnected.

        Some devices (e.g. the Inrico B02PTT) can't reconnect by themselves
        - their incoming connection gives up - while the Pi connecting out
        to them works, so this covers a device switched back on or coming
        back into range."""
        out = await self._run(["show"])
        if _failure(out) or parse_fields(out).get("Powered") != "yes" or self.scanning:
            return []
        reconnected = []
        for address, name in parse_devices(await self._run(["devices", "Paired"])):
            if address in self._hold_off:
                continue
            info = parse_device_info(address, name, await self._run(["info", address]))
            if not info["paired"] or not info["trusted"] or info["connected"]:
                continue
            async with self._lock:
                result = await self._run(["connect", address], _PAIR_TIMEOUT_S)
            if _failure(result) is None:
                self._state_cache = None
                log.info("bluetooth: reconnected %s", info["name"])
                reconnected.append(info["name"])
            else:
                log.debug("bluetooth: %s not reachable: %s", info["name"], _failure(result))
        return reconnected

    async def reconnect_loop(self, interval: float = RECONNECT_INTERVAL_S) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                await self.reconnect_once()
            except BluetoothError as exc:
                log.debug("bluetooth: reconnect check failed: %s", exc)

    def stop(self) -> None:
        if self._scan_task is not None:
            self._scan_task.cancel()
            self._scan_task = None
