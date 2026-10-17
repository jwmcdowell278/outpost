"""USB HID push-to-talk (e.g. a generic USB footswitch/button).

HID report layouts vary a lot by device. This polls raw input reports and
treats a masked byte going non-zero as "pressed". The defaults work for many
simple single-button HID devices, but you may need to tune `button_byte` /
`button_mask` for yours - run `python -m outpost.scripts.dump_hid_reports`
(or inspect with `hid.enumerate()`) to see your device's raw report bytes.

Requires the optional `hidapi` dependency (see requirements-hardware.txt) and
the system hidapi library installed.
"""
from __future__ import annotations
import threading
import time
import hid

from . import PTTSource, PTTCallback


class HIDPTT(PTTSource):
    def __init__(
        self,
        on_change: PTTCallback,
        vendor_id: int,
        product_id: int,
        button_byte: int = 1,
        button_mask: int = 0xFF,
        poll_interval: float = 0.01,
    ):
        self._on_change = on_change
        self._vendor_id = vendor_id
        self._product_id = product_id
        self._button_byte = button_byte
        self._button_mask = button_mask
        self._poll_interval = poll_interval
        self._device: hid.device | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._pressed = False

    def start(self) -> None:
        self._device = hid.device()
        self._device.open(self._vendor_id, self._product_id)
        self._device.set_nonblocking(True)
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=1)
            self._thread = None
        if self._device:
            self._device.close()
            self._device = None

    def _poll_loop(self) -> None:
        while self._running:
            report = self._device.read(64)
            if report and len(report) > self._button_byte:
                pressed = bool(report[self._button_byte] & self._button_mask)
                if pressed != self._pressed:
                    self._pressed = pressed
                    self._on_change(pressed)
            time.sleep(self._poll_interval)
