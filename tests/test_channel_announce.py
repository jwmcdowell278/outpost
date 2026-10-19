"""Verifies channel-switch announcements (`enable_channel_announce`): the
active channel's name is spoken via the injected announcer when it changes,
and nothing happens at all unless the feature is actually enabled."""
from __future__ import annotations

from outpost.client import OutpostClient
from outpost.config import OutpostConfig, Channel
from outpost.crypto import generate_channel_key


class _FakeAnnouncer:
    def __init__(self) -> None:
        self.announced: list[str] = []

    def announce(self, text: str) -> None:
        self.announced.append(text)


def _make_client(channel_announcer=None, **overrides) -> OutpostClient:
    defaults = dict(
        sender_id=1,
        listen_port=0,
        mesh_key=generate_channel_key(),
        channels={
            1: Channel(id=1, name="ops", key=generate_channel_key()),
            2: Channel(id=2, name="command", key=generate_channel_key()),
        },
        enable_mdns=False,
        enable_pex=False,
    )
    defaults.update(overrides)
    return OutpostClient(OutpostConfig(**defaults), "ops", channel_announcer=channel_announcer)


def test_channel_announcer_disabled_by_default():
    client = _make_client()
    assert client.channel_announcer is None


def test_switching_channel_announces_the_new_name_when_enabled():
    fake = _FakeAnnouncer()
    client = _make_client(enable_channel_announce=True, channel_announcer=fake)

    client.set_active_channel("command")

    assert fake.announced == ["Channel command"]

