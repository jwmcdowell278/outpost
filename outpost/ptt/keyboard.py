"""Keyboard-based PTT (hold a key, default spacebar) - for development and
testing without dedicated hardware."""
from __future__ import annotations
from pynput import keyboard

from . import PTTSource, PTTCallback


class KeyboardPTT(PTTSource):
    def __init__(self, on_change: PTTCallback, key=keyboard.Key.space):
        self._on_change = on_change
        self._key = key
        self._listener: keyboard.Listener | None = None
        self._pressed = False

    def start(self) -> None:
        self._listener = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)
        self._listener.start()

    def stop(self) -> None:
        if self._listener:
            self._listener.stop()
            self._listener = None

    def _on_press(self, key) -> None:
        if key == self._key and not self._pressed:
            self._pressed = True
            self._on_change(True)

    def _on_release(self, key) -> None:
        if key == self._key and self._pressed:
            self._pressed = False
            self._on_change(False)
