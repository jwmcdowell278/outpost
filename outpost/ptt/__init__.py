"""Push-to-talk input abstraction.

A PTTSource notifies a callback with True/False when the talk key/button is
pressed/released. Concrete sources live in sibling modules and are imported
lazily by callers so that, e.g., a desktop dev machine without GPIO/HID
hardware never needs those optional dependencies installed:

  - keyboard.py    software PTT (hold a key) - for development/testing
  - hid.py         USB HID footswitch/button
  - gpio.py        GPIO button on embedded boards (Raspberry Pi etc.)
  - serial_ptt.py  Serial/RS-232 PTT relay (radio interface boxes)
"""
from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Callable

PTTCallback = Callable[[bool], None]


class PTTSource(ABC):
    @abstractmethod
    def start(self) -> None:
        """Begin listening for PTT press/release events."""

    @abstractmethod
    def stop(self) -> None:
        """Stop listening and release any underlying resources."""


class NullPTT(PTTSource):
    """No-op fallback used when the configured hardware source can't be
    built (e.g. no X11 display for keyboard PTT on a headless box) - PTT
    stays available via the web UI, which calls set_ptt directly."""

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass
