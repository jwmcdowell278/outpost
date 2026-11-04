"""Desktop keyboard PTT (outpost/ptt/keyboard.py) following the key picked
on the web UI's Buttons page. Key events are fed in directly - no real
keyboard listener is started."""
from __future__ import annotations
import pytest

keyboard = pytest.importorskip("pynput.keyboard")

from outpost.ptt.keyboard import KeyboardPTT, resolve_key


def test_resolve_key():
    assert resolve_key(None) is None
    assert resolve_key("KEY_SPACE") == keyboard.Key.space
    assert resolve_key("KEY_F8") == keyboard.Key.f8
    assert resolve_key("KEY_A") == keyboard.KeyCode.from_char("a")
    assert resolve_key("KEY_KP0") is None


def test_follows_the_picked_key_live():
    events = []
    current = {"key": None}
    ptt = KeyboardPTT(events.append, key_name=lambda: current["key"])
    ptt._on_press(keyboard.Key.space)
    ptt._on_release(keyboard.Key.space)
    assert events == []  # no key picked: Space does nothing

    current["key"] = "KEY_A"
    ptt._on_press(keyboard.Key.space)
    assert events == []
    ptt._on_press(keyboard.KeyCode.from_char("A"))  # Shift held
    ptt._on_release(keyboard.KeyCode.from_char("a"))
    assert events == [True, False]


def test_changing_key_while_held_releases_ptt():
    events = []
    current = {"key": "KEY_F8"}
    ptt = KeyboardPTT(events.append, key_name=lambda: current["key"])
    ptt._on_press(keyboard.Key.f8)
    current["key"] = "KEY_F9"
    ptt._on_release(keyboard.Key.f8)
    assert events == [True, False]
