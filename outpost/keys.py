"""Keyboard keys that can be picked as the push-to-talk key from the web UI.

One table links the three names a key has:
  - the Linux input name (e.g. "KEY_F8") - what's saved as `ptt_key` in the
    config, and what the Buttons reader (evdev) reports on Linux;
  - the browser's KeyboardEvent.code (e.g. "F8") - so the Buttons page can
    record a key pressed in the page itself;
  - pynput's name - for keyboard PTT on a desktop (scripts/run_client.py
    --ptt keyboard): "key:<Key attribute>" or "char:<character>".
"""
from __future__ import annotations

# Linux input name -> (browser code, label, pynput spec or None)
_SPECIAL: list[tuple[str, str, str, str | None]] = [
    ("KEY_SPACE", "Space", "Space", "key:space"),
    ("KEY_ENTER", "Enter", "Enter", "key:enter"),
    ("KEY_TAB", "Tab", "Tab", "key:tab"),
    ("KEY_BACKSPACE", "Backspace", "Backspace", "key:backspace"),
    ("KEY_CAPSLOCK", "CapsLock", "Caps Lock", "key:caps_lock"),
    ("KEY_LEFTSHIFT", "ShiftLeft", "Left Shift", "key:shift_l"),
    ("KEY_RIGHTSHIFT", "ShiftRight", "Right Shift", "key:shift_r"),
    ("KEY_LEFTCTRL", "ControlLeft", "Left Ctrl", "key:ctrl_l"),
    ("KEY_RIGHTCTRL", "ControlRight", "Right Ctrl", "key:ctrl_r"),
    ("KEY_LEFTALT", "AltLeft", "Left Alt", "key:alt_l"),
    ("KEY_RIGHTALT", "AltRight", "Right Alt", "key:alt_r"),
    ("KEY_LEFTMETA", "MetaLeft", "Left Windows/Command", "key:cmd_l"),
    ("KEY_RIGHTMETA", "MetaRight", "Right Windows/Command", "key:cmd_r"),
    ("KEY_COMPOSE", "ContextMenu", "Menu", "key:menu"),
    ("KEY_UP", "ArrowUp", "Up arrow", "key:up"),
    ("KEY_DOWN", "ArrowDown", "Down arrow", "key:down"),
    ("KEY_LEFT", "ArrowLeft", "Left arrow", "key:left"),
    ("KEY_RIGHT", "ArrowRight", "Right arrow", "key:right"),
    ("KEY_INSERT", "Insert", "Insert", "key:insert"),
    ("KEY_DELETE", "Delete", "Delete", "key:delete"),
    ("KEY_HOME", "Home", "Home", "key:home"),
    ("KEY_END", "End", "End", "key:end"),
    ("KEY_PAGEUP", "PageUp", "Page Up", "key:page_up"),
    ("KEY_PAGEDOWN", "PageDown", "Page Down", "key:page_down"),
    ("KEY_SCROLLLOCK", "ScrollLock", "Scroll Lock", "key:scroll_lock"),
    ("KEY_PAUSE", "Pause", "Pause", "key:pause"),
    ("KEY_GRAVE", "Backquote", "`", "char:`"),
    ("KEY_MINUS", "Minus", "-", "char:-"),
    ("KEY_EQUAL", "Equal", "=", "char:="),
    ("KEY_LEFTBRACE", "BracketLeft", "[", "char:["),
    ("KEY_RIGHTBRACE", "BracketRight", "]", "char:]"),
    ("KEY_BACKSLASH", "Backslash", "\\", "char:\\"),
    ("KEY_SEMICOLON", "Semicolon", ";", "char:;"),
    ("KEY_APOSTROPHE", "Quote", "'", "char:'"),
    ("KEY_COMMA", "Comma", ",", "char:,"),
    ("KEY_DOT", "Period", ".", "char:."),
    ("KEY_SLASH", "Slash", "/", "char:/"),
    # Number pad keys: Linux only (pynput can't tell them from the main keys).
    ("KEY_KPENTER", "NumpadEnter", "Keypad Enter", None),
    ("KEY_KPPLUS", "NumpadAdd", "Keypad +", None),
    ("KEY_KPMINUS", "NumpadSubtract", "Keypad -", None),
    ("KEY_KPASTERISK", "NumpadMultiply", "Keypad *", None),
    ("KEY_KPSLASH", "NumpadDivide", "Keypad /", None),
    ("KEY_KPDOT", "NumpadDecimal", "Keypad .", None),
]


def _build() -> dict[str, tuple[str, str, str | None]]:
    keys: dict[str, tuple[str, str, str | None]] = {}
    for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        keys[f"KEY_{c}"] = (f"Key{c}", c, f"char:{c.lower()}")
    for d in "0123456789":
        keys[f"KEY_{d}"] = (f"Digit{d}", d, f"char:{d}")
        keys[f"KEY_KP{d}"] = (f"Numpad{d}", f"Keypad {d}", None)
    for n in range(1, 25):
        keys[f"KEY_F{n}"] = (f"F{n}", f"F{n}", f"key:f{n}" if n <= 20 else None)
    for name, code, label, spec in _SPECIAL:
        keys[name] = (code, label, spec)
    return keys


PTT_KEYS = _build()

# Browser KeyboardEvent.code -> Linux input name, for the Buttons page.
BROWSER_CODES: dict[str, str] = {code: name for name, (code, _, _) in PTT_KEYS.items()}


def key_label(name: str | None) -> str | None:
    if name is None:
        return None
    entry = PTT_KEYS.get(name)
    return entry[1] if entry else name


def pynput_spec(name: str) -> str | None:
    entry = PTT_KEYS.get(name)
    return entry[2] if entry else None
