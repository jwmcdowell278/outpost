"""Verifies voice selection for channel announcements (`channel_announce_voice`):
matches a configured substring against installed voices' id/name/language,
case-insensitively, and returns None (system default) when nothing matches."""
from __future__ import annotations

from dataclasses import dataclass, field

from outpost.announce import _find_voice_id, _resolve_say_voice


@dataclass
class _FakeVoice:
    id: str
    name: str
    languages: list[str] = field(default_factory=list)
    gender: str | None = None


class _FakeEngine:
    def __init__(self, voices: list[_FakeVoice]) -> None:
        self._voices = voices

    def getProperty(self, name: str):
        assert name == "voices"
        return self._voices


_VOICES = [
    _FakeVoice(id="com.apple.speech.synthesis.voice.Albert", name="Albert", languages=["en_US"], gender="Male"),
    _FakeVoice(id="com.apple.voice.compact.en-IE.Moira", name="Moira", languages=["en_IE"], gender="Female"),
    _FakeVoice(id="com.apple.voice.compact.en-GB.Daniel", name="Daniel", languages=["en_GB"], gender="Male"),
]


def test_matches_by_name_case_insensitively():
    engine = _FakeEngine(_VOICES)
    assert _find_voice_id(engine, "moira") == "com.apple.voice.compact.en-IE.Moira"


def test_matches_by_language_code():
    engine = _FakeEngine(_VOICES)
    assert _find_voice_id(engine, "en_GB") == "com.apple.voice.compact.en-GB.Daniel"


def test_matches_by_gender():
    engine = _FakeEngine(_VOICES)
    assert _find_voice_id(engine, "female") == "com.apple.voice.compact.en-IE.Moira"


def test_no_match_returns_none():
    engine = _FakeEngine(_VOICES)
    assert _find_voice_id(engine, "nonexistent-voice") is None


_SAY_LINES = [
    "Albert              en_US    # Hello! My name is Albert.",
    "Moira               en_IE    # Top o' the morning to you!",
    "Daniel              en_GB    # Hello! My name is Daniel.",
]


def test_say_matches_by_name_case_insensitively():
    assert _resolve_say_voice(_SAY_LINES, "moira") == "Moira"


def test_say_matches_by_language_code():
    assert _resolve_say_voice(_SAY_LINES, "en_GB") == "Daniel"


def test_say_no_match_returns_none():
    assert _resolve_say_voice(_SAY_LINES, "nonexistent-voice") is None
