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



# --- not cutting off the start (release_audio_when_idle + Bluetooth headsets) ----

import asyncio
import queue
import time
from types import SimpleNamespace

from outpost import client as client_mod
from outpost.announce import ChannelAnnouncer


def _bare_announcer(spoken):
    """A ChannelAnnouncer without a speech engine or thread."""
    a = ChannelAnnouncer.__new__(ChannelAnnouncer)
    a._queue = queue.Queue()
    a._unavailable = False
    a.before_speak = a.after_speak = None
    a._speak = lambda text: spoken.append(("speak", text))
    return a


def test_only_the_newest_queued_announcement_is_spoken():
    spoken = []
    a = _bare_announcer(spoken)
    for name in ("tac", "logistics", "command"):
        a.announce(f"Channel {name}")
    assert a._next_text() == "Channel command"
    assert a._queue.empty()


def test_speaker_is_readied_before_and_kept_after_speaking():
    spoken = []
    a = _bare_announcer(spoken)
    a.before_speak = lambda: spoken.append("ready")
    a.after_speak = lambda: spoken.append("done")
    a._announce_once("Channel tac")
    assert spoken == ["ready", ("speak", "Channel tac"), "done"]

    def broken(text):
        raise RuntimeError("no espeak")
    spoken.clear()
    a._speak = broken
    a._announce_once("Channel tac")
    assert spoken == ["ready", "done"]


def test_client_hooks_its_announcer_up():
    a = _bare_announcer([])
    client = _make_client(enable_channel_announce=True, channel_announcer=a)
    assert a.before_speak == client.prepare_local_speech
    assert a.after_speak == client.local_speech_done


def test_closed_speaker_is_opened_and_given_time_before_speaking(monkeypatch):
    monkeypatch.setattr(client_mod, "_SPEECH_LINK_WAIT_S", 0.2)

    async def run():
        client = _make_client(release_audio_when_idle=True)
        client._loop = asyncio.get_running_loop()
        speaker = SimpleNamespace(wanted=False, last_callback=0.0)
        client.speaker = speaker
        demanded = []

        def demand(kind):
            demanded.append(kind)
            speaker.wanted = True
        client._demand_audio_threadsafe = demand

        def callbacks():  # the opened stream starts running
            end = time.monotonic() + 1.0
            while time.monotonic() < end:
                if speaker.wanted:
                    speaker.last_callback = time.monotonic()
                time.sleep(0.01)
        feeder = client._loop.run_in_executor(None, callbacks)
        started = time.monotonic()
        await client._loop.run_in_executor(None, client.prepare_local_speech)
        waited = time.monotonic() - started
        assert demanded == ["output"] and waited >= 0.2
        # Already running now: no waiting.
        started = time.monotonic()
        await client._loop.run_in_executor(None, client.prepare_local_speech)
        assert time.monotonic() - started < 0.1 and demanded == ["output"]
        await feeder
    asyncio.run(run())


def test_no_waiting_without_release_audio_when_idle():
    client = _make_client()
    started = time.monotonic()
    client.prepare_local_speech()
    assert time.monotonic() - started < 0.05
