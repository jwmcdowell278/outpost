"""Hardware buttons bound to actions: push-to-talk, volume, channel up/down.

Works with anything Linux sees as an input device with keys - Bluetooth PTT
buttons (e.g. Inrico), USB footswitches, media remotes, keyboards. Bindings
are set up from the web UI's Buttons page: pick an action, press the button,
and the key it sends is recorded (see ButtonController.learn).

Linux only: reads /dev/input/event* through the `evdev` package. The user
running Outpost needs read access to those files - on Raspberry Pi OS and
most distros that means being in the `input` group.
"""
from __future__ import annotations
import asyncio
import ctypes
import glob
import logging
import re
import select
import socket
import struct
import subprocess
import sys
import threading
import time
from typing import Callable

from .config import ButtonBinding, save_config_fields

log = logging.getLogger(__name__)

# evdev key event values.
KEY_UP, KEY_DOWN, KEY_REPEAT = 0, 1, 2

# How often to look for input devices that appeared since the last look,
# e.g. a Bluetooth button reconnecting after going out of range.
_RESCAN_S = 3.0
# Volume change per press of a volume button (volumes run 0.0-1.0).
VOLUME_STEP = 0.1

# (device name, key name, value) - called on the reader thread.
EventCallback = Callable[[str, str, int], None]


def _load_evdev():
    """(evdev module, None), or (None, why buttons are unavailable)."""
    if not sys.platform.startswith("linux"):
        return None, "hardware buttons are only supported on Linux (e.g. a Raspberry Pi)"
    try:
        import evdev
    except ImportError:
        return None, "the evdev package isn't installed - run: pip install evdev"
    return evdev, None


def _key_name(ecodes, code: int) -> str:
    name = ecodes.bytype[ecodes.EV_KEY].get(code)
    if isinstance(name, (list, tuple)):  # codes with several aliases
        name = name[0]
    return name or f"KEY_{code}"


class EvdevInput:
    """Reads key events from every readable input device that has keys, on
    one background thread. Devices that appear later (a Bluetooth button
    reconnecting) are picked up, and ones that disappear are dropped."""

    def __init__(self, on_event: EventCallback, evdev_module):
        self._on_event = on_event
        self._evdev = evdev_module
        self._open: dict[int, object] = {}   # fd -> InputDevice
        self._skipped: set[str] = set()      # paths without keys
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.unreadable = 0                  # devices present but not readable

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="buttons", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
        with self._lock:
            for dev in self._open.values():
                self._close(dev)
            self._open.clear()

    def devices(self) -> list[str]:
        with self._lock:
            return sorted({dev.name for dev in self._open.values()})

    @staticmethod
    def _close(dev) -> None:
        try:
            dev.close()
        except Exception:
            pass

    def _rescan(self) -> None:
        readable = set(self._evdev.list_devices())  # only lists readable ones
        self.unreadable = len(set(glob.glob("/dev/input/event*")) - readable)
        self._skipped &= readable  # a path can be reused by a new device
        with self._lock:
            open_paths = {dev.path for dev in self._open.values()}
        for path in sorted(readable - open_paths - self._skipped):
            try:
                dev = self._evdev.InputDevice(path)
            except OSError:
                continue
            if self._evdev.ecodes.EV_KEY not in dev.capabilities():
                self._skipped.add(path)
                self._close(dev)
                continue
            log.info("buttons: listening to %s (%s)", dev.name, path)
            with self._lock:
                self._open[dev.fd] = dev

    def _run(self) -> None:
        ecodes = self._evdev.ecodes
        next_rescan = 0.0
        while not self._stop.is_set():
            if time.monotonic() >= next_rescan:
                try:
                    self._rescan()
                except Exception as exc:
                    log.debug("buttons: device scan failed: %s", exc)
                next_rescan = time.monotonic() + _RESCAN_S
            with self._lock:
                fds = list(self._open)
            if not fds:
                self._stop.wait(0.5)
                continue
            try:
                ready, _, _ = select.select(fds, [], [], 0.5)
            except (OSError, ValueError):
                ready = fds  # a device went away - the reads below find which
            for fd in ready:
                with self._lock:
                    dev = self._open.get(fd)
                if dev is None:
                    continue
                try:
                    for event in dev.read():
                        if event.type == ecodes.EV_KEY:
                            self._on_event(dev.name, _key_name(ecodes, event.code), event.value)
                except BlockingIOError:
                    pass
                except OSError:
                    log.info("buttons: %s disconnected", dev.name)
                    with self._lock:
                        self._open.pop(fd, None)
                    self._close(dev)


class HandsFreeInput:
    """Buttons on Bluetooth headsets/speaker mics that arrive as hands-free
    (HFP/HSP) AT commands instead of key presses - e.g. the Inrico B02PTT's
    PTT sends AT+XEVENT=TALK,1 on press and AT+XEVENT=TALK,0 on release,
    and a plain headset button sends AT+CKPD=200.

    The audio system (PipeWire) owns that connection and rejects commands
    it doesn't know, so they're read passively from the kernel's Bluetooth
    monitor, like `btmon` - which needs the CAP_NET_RAW capability (see
    README "Bluetooth headset buttons")."""

    DEVICE = "Bluetooth headset"
    # (bytes in the command, key name, values to report)
    COMMANDS = [
        (b"AT+XEVENT=TALK,1", "HFP_TALK", (KEY_DOWN,)),
        (b"AT+XEVENT=TALK,0", "HFP_TALK", (KEY_UP,)),
        (b"AT+CKPD=200", "HFP_BUTTON", (KEY_DOWN, KEY_UP)),  # one message per press
    ]

    _AF_BLUETOOTH, _BTPROTO_HCI = 31, 1
    _HCI_DEV_NONE, _HCI_CHANNEL_MONITOR = 0xFFFF, 2
    _OP_ACL_RX = 5  # monitor opcode for incoming data packets

    def __init__(self, on_event: EventCallback):
        self._on_event = on_event
        self._sock: socket.socket | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @classmethod
    def open_monitor(cls) -> socket.socket:
        """Raises PermissionError without CAP_NET_RAW. Python's own bind()
        can't select the monitor channel, so it goes through libc."""
        sock = socket.socket(cls._AF_BLUETOOTH, socket.SOCK_RAW, cls._BTPROTO_HCI)
        addr = struct.pack("<HHH", cls._AF_BLUETOOTH, cls._HCI_DEV_NONE, cls._HCI_CHANNEL_MONITOR)
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.bind(sock.fileno(), addr, len(addr)) != 0:
            err = ctypes.get_errno()
            sock.close()
            raise OSError(err, "can't open the Bluetooth monitor")
        sock.settimeout(0.5)
        return sock

    def start(self) -> None:
        self._sock = self.open_monitor()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="hfp-buttons", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None
        if self._sock:
            self._sock.close()
            self._sock = None

    def devices(self) -> list[str]:
        return [self.DEVICE]

    def handle_frame(self, frame: bytes) -> None:
        if len(frame) < 6 or struct.unpack_from("<H", frame)[0] != self._OP_ACL_RX:
            return
        if b"AT+" not in frame:
            return
        for pattern, key, values in self.COMMANDS:
            if pattern in frame:
                for value in values:
                    self._on_event(self.DEVICE, key, value)
                return

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                frame = self._sock.recv(4096)
            except socket.timeout:
                continue
            except OSError as exc:
                if not self._stop.is_set():
                    log.warning("buttons: Bluetooth monitor stopped: %s", exc)
                return
            try:
                self.handle_frame(frame)
            except Exception:
                log.exception("buttons: bad Bluetooth monitor frame")


class SerialHeadsetInput:
    """Buttons that a Bluetooth headset reports over its serial port (SPP)
    channel - e.g. the Inrico B02PTT's group up/down buttons send
    `C:VP*` and `C:VM*` there (PTT apps listen on it), and nothing over the
    hands-free link or as key presses.

    Opens the serial channel of every connected, paired device that has
    one, and reports each `C:<code>*` message as key SPP_<code> (one press:
    down then up) on device "Bluetooth headset". Devices that connect later
    are picked up; dropped ones are retried."""

    DEVICE = "Bluetooth headset"
    SERIAL_PORT_UUID = "00001101-0000-1000-8000-00805f9b34fb"
    _MESSAGE = re.compile(rb"C:([A-Za-z0-9_]{1,16})\*?")
    _RESCAN_S = 10.0
    _CHANNELS = range(1, 31)

    def __init__(self, on_event: EventCallback, list_devices=None, connect=None):
        self._on_event = on_event
        # list_devices() -> addresses; connect(address) -> socket or None.
        # Both swapped in tests.
        self._list_devices = list_devices or self._serial_devices
        self._connect = connect or self._open_rfcomm
        self._channels: dict[str, int] = {}   # address -> working channel
        self._open: dict[str, object] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @staticmethod
    def available() -> bool:
        return hasattr(socket, "BTPROTO_RFCOMM") and sys.platform.startswith("linux")

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="spp-buttons", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            socks = list(self._open.values())
            self._open.clear()
        for sock in socks:
            try:
                sock.close()
            except OSError:
                pass
        if self._thread:
            self._thread.join(timeout=2)
            self._thread = None

    def devices(self) -> list[str]:
        with self._lock:
            return [self.DEVICE] if self._open else []

    @classmethod
    def _serial_devices(cls) -> list[str]:
        """Connected, paired devices offering a serial port, via bluetoothctl."""
        from .bluetooth import parse_devices

        def run(*args):
            return subprocess.run(["bluetoothctl", *args], capture_output=True, text=True, timeout=10).stdout
        try:
            connected = parse_devices(run("devices", "Connected"))
            found = []
            for address, _ in connected:
                info = run("info", address)
                if cls.SERIAL_PORT_UUID in info.lower() and "Paired: yes" in info:
                    found.append(address)
            return found
        except (OSError, subprocess.SubprocessError):
            return []

    def _rfcomm(self, address: str, channel: int, timeout: float):
        sock = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
        sock.settimeout(timeout)
        try:
            sock.connect((address, channel))
        except OSError:
            sock.close()
            return None
        return sock

    def _open_rfcomm(self, address: str):
        """The device's serial channel. Its number isn't looked up (Python
        has no SDP client): the last one that worked, else 1, else the first
        of 2-30 that accepts a connection."""
        known = self._channels.get(address)
        for channel in ([known] if known else []) + [1]:
            sock = self._rfcomm(address, channel, 6.0)
            if sock is not None:
                self._channels[address] = channel
                return sock
        found: dict[int, object] = {}

        def probe(ch):
            sock = self._rfcomm(address, ch, 6.0)
            if sock is not None:
                found[ch] = sock
        threads = [threading.Thread(target=probe, args=(ch,)) for ch in self._CHANNELS if ch != 1]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        if not found:
            return None
        channel = min(found)
        for ch, sock in found.items():
            if ch != channel:
                sock.close()
        self._channels[address] = channel
        return found[channel]

    def handle_data(self, data: bytes) -> None:
        for m in self._MESSAGE.finditer(data):
            key = "SPP_" + m.group(1).decode().upper()
            self._on_event(self.DEVICE, key, KEY_DOWN)
            self._on_event(self.DEVICE, key, KEY_UP)

    def _read(self, address: str, sock) -> None:
        sock.settimeout(1.0)
        buffer = b""
        while not self._stop.is_set():
            try:
                data = sock.recv(1024)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                break
            buffer += data
            # Messages end in "*" and NUL; keep a partial one for the next read.
            *complete, buffer = buffer.split(b"\x00")
            if buffer.endswith(b"*"):
                complete.append(buffer)
                buffer = b""
            for message in complete:
                try:
                    self.handle_data(message)
                except Exception:
                    log.exception("buttons: bad serial message %r", message)
            if len(buffer) > 256:
                buffer = b""
        with self._lock:
            if self._open.get(address) is sock:
                del self._open[address]
        try:
            sock.close()
        except OSError:
            pass
        if not self._stop.is_set():
            log.info("buttons: %s serial buttons disconnected", address)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                addresses = self._list_devices()
            except Exception as exc:
                log.debug("buttons: serial device scan failed: %s", exc)
                addresses = []
            for address in addresses:
                with self._lock:
                    if address in self._open:
                        continue
                sock = self._connect(address)
                if sock is None or self._stop.is_set():
                    continue
                with self._lock:
                    self._open[address] = sock
                log.info("buttons: listening to %s serial buttons", address)
                threading.Thread(target=self._read, args=(address, sock), name="spp-read", daemon=True).start()
            self._stop.wait(self._RESCAN_S)


class ButtonController:
    """Turns key events into client actions according to the configured
    bindings, and records presses for the web UI's "press a button" setup."""

    def __init__(self, client, bindings: list[ButtonBinding], input_factory=None, handsfree_factory=None,
                 ptt_key: str | None = None, serial_factory=None):
        self.client = client
        self.bindings = list(bindings)
        # Keyboard key that's push-to-talk (hold) on any device - see
        # OutpostConfig.ptt_key.
        self.ptt_key = ptt_key
        self._input_factory = input_factory
        # None = the real HandsFreeInput (Linux only); False = don't use one.
        self._handsfree_factory = handsfree_factory
        # None = the real SerialHeadsetInput (Linux only); False = don't use one.
        self._serial_factory = serial_factory
        self._serial = None
        self._input = None
        self._handsfree = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.unavailable_reason: str | None = None
        # Why Bluetooth headset (hands-free) buttons aren't being read, if not.
        self.handsfree_note: str | None = None
        # (device, key, monotonic time) of the most recent press.
        self.last_press: tuple[str, str, float] | None = None
        self._learn_future: asyncio.Future | None = None
        # Keys whose press was captured by learn(): their release (and any
        # auto-repeat) mustn't trigger the binding just made for them.
        self._captured: set[tuple[str, str]] = set()

    @property
    def running(self) -> bool:
        return self._input is not None or self._handsfree is not None or self._serial is not None

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._start_handsfree()
        self._start_serial()
        factory = self._input_factory
        if factory is None:
            evdev, reason = _load_evdev()
            if evdev is None:
                if self._handsfree is None:
                    self.unavailable_reason = reason
                    if self.bindings or self.ptt_key:
                        log.warning("buttons: %s - button bindings won't work", reason)
                return
            factory = lambda on_event: EvdevInput(on_event, evdev)  # noqa: E731
        self._input = factory(self._on_event_threadsafe)
        self._input.start()

    def _start_serial(self) -> None:
        factory = self._serial_factory
        if factory is False:
            return
        if factory is None:
            if not SerialHeadsetInput.available():
                return
            factory = SerialHeadsetInput
        try:
            serial = factory(self._on_event_threadsafe)
            serial.start()
        except OSError as exc:
            log.info("buttons: headset serial buttons unavailable: %s", exc)
            return
        self._serial = serial

    def _start_handsfree(self) -> None:
        factory = self._handsfree_factory
        if factory is False:
            return
        if factory is None:
            if not sys.platform.startswith("linux"):
                return
            factory = HandsFreeInput
        try:
            handsfree = factory(self._on_event_threadsafe)
            handsfree.start()
        except PermissionError:
            self.handsfree_note = (
                "Bluetooth headset buttons (e.g. an Inrico PTT) need permission to read Bluetooth "
                "traffic - see README \"Bluetooth headset buttons\"")
            log.info("buttons: %s", self.handsfree_note)
            return
        except OSError as exc:
            self.handsfree_note = f"Bluetooth headset buttons unavailable: {exc}"
            log.info("buttons: %s", self.handsfree_note)
            return
        self._handsfree = handsfree
        log.info("buttons: listening for Bluetooth headset buttons")

    def stop(self) -> None:
        if self._input is not None:
            self._input.stop()
            self._input = None
        if self._handsfree is not None:
            self._handsfree.stop()
            self._handsfree = None
        if self._serial is not None:
            self._serial.stop()
            self._serial = None

    def devices(self) -> list[str]:
        devices = self._input.devices() if self._input is not None else []
        if self._handsfree is not None:
            devices = devices + self._handsfree.devices()
        if self._serial is not None:
            devices = devices + [d for d in self._serial.devices() if d not in devices]
        return devices

    def unreadable_devices(self) -> int:
        return getattr(self._input, "unreadable", 0) if self._input is not None else 0

    def _on_event_threadsafe(self, device: str, key: str, value: int) -> None:
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self.handle_event, device, key, value)

    def handle_event(self, device: str, key: str, value: int) -> None:
        """Runs on the event loop thread."""
        source = (device, key)
        if value == KEY_DOWN:
            self.last_press = (device, key, time.monotonic())
            if self._learn_future is not None and not self._learn_future.done():
                self._learn_future.set_result({"device": device, "key": key})
                self._captured.add(source)
                return
        if source in self._captured:
            if value == KEY_UP:
                self._captured.discard(source)
            return
        if self.ptt_key is not None and key == self.ptt_key:
            self._run("ptt", value)
        for binding in self.bindings:
            if binding.key == key and binding.device in (None, device):
                try:
                    self._run(binding.action, value)
                except Exception:
                    log.exception("buttons: %s failed", binding.action)

    def _run(self, action: str, value: int) -> None:
        client = self.client
        if action == "ptt":
            if value in (KEY_DOWN, KEY_UP):
                client.set_ptt(value == KEY_DOWN)
        elif action == "ptt_toggle":
            if value == KEY_DOWN:
                client.set_ptt(not client._ptt_active)
        elif action in ("volume_up", "volume_down"):
            if value in (KEY_DOWN, KEY_REPEAT):
                client.adjust_volume(VOLUME_STEP if action == "volume_up" else -VOLUME_STEP)
                self._save({"channel_volumes": client.config.channel_volumes})
        elif action in ("channel_up", "channel_down"):
            if value == KEY_DOWN:
                client.step_channel(1 if action == "channel_up" else -1)

    def _save(self, fields: dict) -> None:
        if self.client.config_path is None:
            return
        try:
            save_config_fields(self.client.config_path, fields)
        except OSError as exc:
            log.warning("buttons: couldn't save %s: %s", ", ".join(fields), exc)

    async def learn(self, timeout: float) -> dict | None:
        """Waits for the next button press on any device and returns
        {"device", "key"} - or None if nothing was pressed in time. That
        press triggers no action. A newer call replaces an older one."""
        if self._learn_future is not None and not self._learn_future.done():
            self._learn_future.set_result(None)
        future = asyncio.get_running_loop().create_future()
        self._learn_future = future
        try:
            return await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            if self._learn_future is future:
                self._learn_future = None
