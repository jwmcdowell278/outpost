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
import glob
import logging
import select
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


class ButtonController:
    """Turns key events into client actions according to the configured
    bindings, and records presses for the web UI's "press a button" setup."""

    def __init__(self, client, bindings: list[ButtonBinding], input_factory=None):
        self.client = client
        self.bindings = list(bindings)
        self._input_factory = input_factory
        self._input = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self.unavailable_reason: str | None = None
        # (device, key, monotonic time) of the most recent press.
        self.last_press: tuple[str, str, float] | None = None
        self._learn_future: asyncio.Future | None = None
        # Keys whose press was captured by learn(): their release (and any
        # auto-repeat) mustn't trigger the binding just made for them.
        self._captured: set[tuple[str, str]] = set()

    @property
    def running(self) -> bool:
        return self._input is not None

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        factory = self._input_factory
        if factory is None:
            evdev, reason = _load_evdev()
            if evdev is None:
                self.unavailable_reason = reason
                if self.bindings:
                    log.warning("buttons: %s - button bindings won't work", reason)
                return
            factory = lambda on_event: EvdevInput(on_event, evdev)  # noqa: E731
        self._input = factory(self._on_event_threadsafe)
        self._input.start()

    def stop(self) -> None:
        if self._input is not None:
            self._input.stop()
            self._input = None

    def devices(self) -> list[str]:
        return self._input.devices() if self._input is not None else []

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
