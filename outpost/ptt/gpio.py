"""GPIO push-to-talk button for embedded boards (e.g. Raspberry Pi), via gpiozero.

Requires the optional `gpiozero` dependency (see requirements-hardware.txt),
which in turn needs a GPIO-capable board (or its pin-factory mock backend).
"""
from __future__ import annotations
from gpiozero import Button

from . import PTTSource, PTTCallback


class GPIOPTT(PTTSource):
    def __init__(self, on_change: PTTCallback, pin: int, pull_up: bool = True):
        self._on_change = on_change
        self._pin = pin
        self._pull_up = pull_up
        self._button: Button | None = None

    def start(self) -> None:
        self._button = Button(self._pin, pull_up=self._pull_up)
        self._button.when_pressed = lambda: self._on_change(True)
        self._button.when_released = lambda: self._on_change(False)

    def stop(self) -> None:
        if self._button:
            self._button.close()
            self._button = None
