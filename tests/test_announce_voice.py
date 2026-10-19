"""Verifies voice selection for channel announcements (`channel_announce_voice`):
matches a configured substring against installed voices' id/name/language,
case-insensitively, and returns None (system default) when nothing matches."""
from __future__ import annotations

from dataclasses import dataclass, field

from outpost.announce import _find_voice_id


@dataclass
class _FakeVoice:
    id: str
    name: str
    languages: list[str] = field(default_factory=list)


class _FakeEngine:
    def __init__(self, voices: list[_FakeVoice]) -> None:
        self._voices = voices

    def getProperty(self, name: str):
        assert name == "voices"
        return self._voices


_VOICES = [
    _FakeVoice(id="com.apple.speech.synthesis.voice.Albert", name="Albert", languages=["en_US"]),
    _FakeVoice(id="com.apple.voice.compact.en-IE.Moira", name="Moira", languages=["en_IE"]),
    _FakeVoice(id="com.apple.voice.compact.en-GB.Daniel", name="Daniel", languages=["en_GB"]),
]


def test_matches_by_name_case_insensitively():
    engine = _FakeEngine(_VOICES)
    assert _find_voice_id(engine, "moira") == "com.apple.voice.compact.en-IE.Moira"


def test_matches_by_language_code():
    engine = _FakeEngine(_VOICES)
    assert _find_voice_id(engine, "en_GB") == "com.apple.voice.compact.en-GB.Daniel"


def test_no_match_returns_none():
    engine = _FakeEngine(_VOICES)
    assert _find_voice_id(engine, "nonexistent-voice") is None
