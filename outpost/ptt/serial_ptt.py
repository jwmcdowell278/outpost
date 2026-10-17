"""Serial/RS-232 PTT input - common on radio interface boxes that assert a
control line (e.g. CTS or DSR) while a hardware PTT button is held.

Requires the optional `pyserial` dependency (see requirements-hardware.txt).
"""
from __future__ import annotations
import threading
import time
import serial

from . import PTTSource, PTTCallback


class SerialPTT(PTTSource):
    def __init__(
        self,
        on_change: PTTCallback,
        port: str,
        baudrate: int = 9600,
        line: str = "cts",
        poll_interval: float = 0.02,
        active_low: bool = False,
    ):
        self._on_change = on_change
        self._port = port
        self._baudrate = baudrate
        self._line = line
        self._poll_interval = poll_interval
        self._active_low = active_low
        self._conn: serial.Serial | None = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._pressed = False

    def start(self) -> None:
        self._conn = serial.Serial(self._port, self._baudrate, timeout=0)
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=1)
            self._thread = None
        if self._conn:
            self._conn.close()
            self._conn = None

    def _read_line_state(self) -> bool:
        raw = getattr(self._conn, self._line)
        return (not raw) if self._active_low else raw

    def _poll_loop(self) -> None:
        while self._running:
            pressed = self._read_line_state()
            if pressed != self._pressed:
                self._pressed = pressed
                self._on_change(pressed)
            time.sleep(self._poll_interval)
