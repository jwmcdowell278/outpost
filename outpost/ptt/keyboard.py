"""Keyboard-based PTT (hold a key) - for desktops with a display (pynput).
The key is picked on the web UI's Buttons page (OutpostConfig.ptt_key); a
change applies without a restart. No key is set by default."""
from __future__ import annotations
import logging
from typing import Callable

from pynput import keyboard

from . import PTTSource, PTTCallback
from ..keys import pynput_spec

log = logging.getLogger(__name__)


def resolve_key(name: str | None):
    """pynput key for a Linux input key name, or None if no key is set or
    pynput can't listen for that key."""
    if not name:
        return None
    spec = pynput_spec(name)
    if spec is None:
        return None
    kind, _, value = spec.partition(":")
    if kind == "key":
        return getattr(keyboard.Key, value, None)
    return keyboard.KeyCode.from_char(value)


def _same_key(pressed, wanted) -> bool:
    if wanted is None or pressed is None:
        return False
    if isinstance(wanted, keyboard.KeyCode) and isinstance(pressed, keyboard.KeyCode):
        # With Shift held a letter arrives upper case.
        return (pressed.char or "").lower() == (wanted.char or "").lower() and bool(wanted.char)
    return pressed == wanted


class KeyboardPTT(PTTSource):
    def __init__(self, on_change: PTTCallback, key=None,
                 key_name: Callable[[], str | None] | None = None):
        """key_name, if given, returns the current ptt_key (looked up on
        every key event, so a key picked in the web UI takes over live)."""
        self._on_change = on_change
        self._fixed_key = key
        self._key_name = key_name
        self._resolved: tuple[str | None, object] | None = None
        self._listener: keyboard.Listener | None = None
        self._pressed = False

    @property
    def key(self):
        if self._key_name is None:
            return self._fixed_key
        name = self._key_name()
        if self._resolved is None or self._resolved[0] != name:
            key = resolve_key(name)
            if key is None and name:
                log.warning("keyboard PTT: %s can't be used for desktop keyboard PTT - pick another key", name)
            self._resolved = (name, key)
            if self._pressed:  # the old key was held - let go of PTT
                self._pressed = False
                self._on_change(False)
        return self._resolved[1]

    def start(self) -> None:
        self._listener = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)
        self._listener.start()

    def stop(self) -> None:
        if self._listener:
            self._listener.stop()
            self._listener = None

    def _on_press(self, key) -> None:
        if _same_key(key, self.key) and not self._pressed:
            self._pressed = True
            self._on_change(True)

    def _on_release(self, key) -> None:
        if _same_key(key, self.key) and self._pressed:
            self._pressed = False
            self._on_change(False)
