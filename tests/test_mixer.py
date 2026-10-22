"""Jitter buffer, mixing and multi-channel monitoring (outpost/mixer.py and
the client wiring around it). A fake decoder makes each frame's content
show how it was produced: normal decode, FEC recovery, or concealment."""
from __future__ import annotations
import numpy as np
import pytest

from outpost import mixer as mixer_mod
from outpost.client import OutpostClient
from outpost.config import OutpostConfig, Channel
from outpost.crypto import ChannelCipher, generate_channel_key
from outpost.mixer import Mixer, JitterStream
from outpost.protocol import Header, VERSION, TYPE_AUDIO, TYPE_STOP, TYPE_CHANNEL_ANNOUNCE, pad_audio
from outpost.transport import MeshTransport

FS = 320  # frame size
FEC, PLC = 30000, 31000


class FakeDecoder:
    """decode(b'\\x05') -> frame of 5s; FEC/PLC -> marker values."""

    def decode(self, opus):
        return np.full(FS, opus[0], dtype=np.int16).tobytes()

    def decode_fec(self, opus):
        return np.full(FS, FEC, dtype=np.int16).tobytes()

    def decode_loss(self):
        return np.full(FS, PLC, dtype=np.int16).tobytes()


def _values(pcm):
    return int(np.frombuffer(pcm, dtype=np.int16)[0]) if pcm is not None else None


class Clock:
    def __init__(self):
        self.t = 1000.0


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(mixer_mod.time, "monotonic", lambda: c.t)
    return c


def _stream():
    return JitterStream(FakeDecoder(), 0.02)


def _play(stream, clock, n):
    out = []
    for _ in range(n):
        out.append(_values(stream.pull(clock.t)))
        clock.t += 0.02
    return out


# --- JitterStream --------------------------------------------------------

def test_waits_for_target_delay_then_plays_in_order(clock):
    s = _stream()
    s.push(100, b"\x01", clock.t)
    assert _values(s.pull(clock.t)) is None  # buffering, 1 of 2 frames
    s.push(101, b"\x02", clock.t)
    assert _play(s, clock, 2) == [1, 2]


def test_reorders_out_of_order_arrivals(clock):
    s = _stream()
    for seq, v in [(100, 1), (102, 3), (101, 2), (103, 4)]:
        s.push(seq, bytes([v]), clock.t)
    assert _play(s, clock, 4) == [1, 2, 3, 4]


def test_single_loss_recovered_with_fec_when_next_frame_is_here(clock):
    s = _stream()
    for seq in (100, 102, 103):  # 101 lost
        s.push(seq, bytes([seq - 99]), clock.t)
    assert _play(s, clock, 4) == [1, FEC, 3, 4]
    assert s.stats["fec"] == 1


def test_loss_with_nothing_buffered_is_concealed(clock):
    s = _stream()
    s.push(100, b"\x01", clock.t)
    s.push(101, b"\x02", clock.t)
    assert _play(s, clock, 3) == [1, 2, PLC]  # 102 hasn't arrived


def test_frame_arriving_after_its_slot_is_dropped_not_played_late(clock):
    s = _stream()
    s.push(100, b"\x01", clock.t)
    s.push(101, b"\x02", clock.t)
    _play(s, clock, 3)            # 102's slot concealed
    s.push(102, b"\x03", clock.t)  # too late
    assert s.stats["late"] == 1


def test_stop_ends_stream_after_buffer_drains_without_concealment_tail(clock):
    s = _stream()
    s.push(100, b"\x01", clock.t)
    s.push(101, b"\x02", clock.t)
    s.stop()
    assert _play(s, clock, 4) == [1, 2, None, None]
    assert s.state == "idle"


def test_lost_stop_ends_stream_after_bounded_concealment(clock):
    s = _stream()
    s.push(100, b"\x01", clock.t)
    s.push(101, b"\x02", clock.t)
    out = _play(s, clock, 20)
    assert out[:2] == [1, 2]
    assert out.count(PLC) == mixer_mod.MAX_CONCEALED_FRAMES
    assert s.state == "idle"


def test_burst_after_stall_is_trimmed_to_target_delay(clock):
    s = _stream()
    s.push(100, b"\x01", clock.t)
    s.push(101, b"\x02", clock.t)
    _play(s, clock, 2)
    for seq in range(102, 120):  # 18 frames arrive at once
        s.push(seq, bytes([seq - 99]), clock.t)
    _play(s, clock, 1)
    assert s.stats["skipped"] > 0
    assert max(s.buf) - s.next_seq + 1 <= s.delay + mixer_mod.SKIP_SLACK_FRAMES


def test_delay_grows_with_measured_jitter(clock):
    s = _stream()
    # Arrivals alternating 0ms / 60ms late -> high jitter estimate.
    for i in range(60):
        s.push(1000 + i, b"\x01", clock.t + i * 0.02 + (0.06 if i % 2 else 0.0))
    s.stop()
    _play(s, clock, 100)
    s.push(2000, b"\x01", clock.t)  # next talk spurt
    assert s.delay > mixer_mod.MIN_DELAY_FRAMES


def test_new_talk_spurt_after_idle_resyncs(clock):
    s = _stream()
    s.push(100, b"\x01", clock.t)
    s.stop()
    _play(s, clock, 3)
    s.push(500, b"\x07", clock.t)  # seq jumped (control traffic while idle)
    s.push(501, b"\x08", clock.t)
    assert _play(s, clock, 2) == [7, 8]


# --- Mixer ---------------------------------------------------------------

def _mixer():
    return Mixer(FS, 20, decoder_factory=FakeDecoder)


def test_two_simultaneous_senders_are_summed_not_interleaved(clock):
    m = _mixer()
    for seq in (10, 11):
        m.push(1, 7, seq, b"\x05")
        m.push(1, 8, seq, b"\x03")
    assert _values(m.next_frame()) == 8
    assert _values(m.next_frame()) == 8


def test_channel_volume_scales_audio(clock):
    m = _mixer()
    m.volumes[2] = 0.5
    m.push(2, 7, 10, b"\x64")
    m.push(2, 7, 11, b"\x64")
    assert _values(m.next_frame()) == 50


def test_monitored_channel_is_ducked_while_active_channel_talks(clock):
    m = _mixer()
    m.active_channel_id = 1
    for seq in (10, 11):
        m.push(1, 7, seq, b"\x00")    # active channel (silent content)
        m.push(2, 8, seq, b"\x64")    # monitored channel, value 100
    assert _values(m.next_frame()) == int(100 * mixer_mod.DUCK_GAIN)
    m.duck_enabled = False
    assert _values(m.next_frame()) == 100


def test_limiter_prevents_clipping_when_loud_talkers_sum(clock):
    m = _mixer()
    loud = np.full(FS, 25000, dtype=np.int16).tobytes()

    class Loud(FakeDecoder):
        def decode(self, opus):
            return loud
    m._decoder_factory = Loud
    for seq in (10, 11):
        m.push(1, 7, seq, b"x")
        m.push(1, 8, seq, b"x")
    frame = np.frombuffer(m.next_frame(), dtype=np.int16)
    assert frame.max() <= 32000


def test_local_sounds_mix_over_received_audio(clock):
    m = _mixer()
    m.play_local([np.full(FS, 4, dtype=np.int16).tobytes()])
    m.push(1, 7, 10, b"\x05")
    m.push(1, 7, 11, b"\x05")
    assert _values(m.next_frame()) == 9


def test_silence_when_nothing_to_play(clock):
    m = _mixer()
    assert _values(m.next_frame()) == 0
    assert m.last_active == 0.0  # silence doesn't count for echo guard


# --- client: monitoring ---------------------------------------------------

def _client(**overrides):
    keys = {1: generate_channel_key(), 2: generate_channel_key(), 3: generate_channel_key()}
    client = OutpostClient(OutpostConfig(
        sender_id=1, listen_port=0, mesh_key=generate_channel_key(),
        channels={1: Channel(1, "team", keys[1]), 2: Channel(2, "main", keys[2]), 3: Channel(3, "other", keys[3])},
        enable_mdns=False, enable_pex=False, **overrides,
    ), "team")
    client.transport.confirm_peer = lambda *a: True
    pushed = []
    client.mixer.push = lambda ch, sid, seq, opus: pushed.append(ch)
    return client, keys, pushed


def _audio(client, key, channel, sender=9, seq=100):
    header = Header(VERSION, TYPE_AUDIO, channel, sender, seq)
    opus = client.codec.encode(b"\x00\x00" * client.codec.frame_size)
    client._on_packet(header, ChannelCipher(key).encrypt(header, pad_audio(opus, None)), ("10.0.0.2", 5555))


def test_monitored_channel_audio_is_played_alongside_active():
    client, keys, pushed = _client(monitor_channels=["main"])
    _audio(client, keys[1], 1, seq=100)
    _audio(client, keys[2], 2, seq=101)
    _audio(client, keys[3], 3, seq=102)  # neither active nor monitored
    assert pushed == [1, 2]


def test_monitoring_can_be_toggled_live():
    client, keys, pushed = _client()
    _audio(client, keys[2], 2, seq=100)
    client.set_monitor("main", True)
    _audio(client, keys[2], 2, seq=101)
    client.set_monitor("main", False)
    _audio(client, keys[2], 2, seq=102)
    assert pushed == [2]
    assert client.config.monitor_channels == []


def test_switching_active_channel_keeps_monitored_channel():
    client, keys, pushed = _client(monitor_channels=["main"])
    client.set_active_channel("other")
    _audio(client, keys[2], 2, seq=100)
    _audio(client, keys[1], 1, seq=101)  # old active, not monitored -> dropped
    assert pushed == [2]


def test_channel_volume_set_live_and_saved_in_config():
    client, _, _ = _client()
    client.set_channel_volume("main", 0.4)
    assert client.channel_volume(2) == 0.4
    assert client.config.channel_volumes == {"main": 0.4}


# --- sender: contiguous audio seqs ---------------------------------------

def test_control_packets_are_held_while_transmitting():
    t = MeshTransport(sender_id=1, peers=[], on_packet=lambda *a: None)
    sent = []
    t.send = lambda ch, seq, ct, ptype=None, targets=None: sent.append((ptype, seq))
    cipher = ChannelCipher(generate_channel_key())

    t.hold_control(True)
    t.send_encrypted(cipher, 1, TYPE_AUDIO, b"a")
    t.send_encrypted(cipher, 1, TYPE_CHANNEL_ANNOUNCE, b"")  # held
    t.send_encrypted(cipher, 1, TYPE_AUDIO, b"b")
    t.send_encrypted(cipher, 1, TYPE_STOP, b"")
    assert [p for p, _ in sent] == [TYPE_AUDIO, TYPE_AUDIO, TYPE_STOP]
    audio_seqs = [s for p, s in sent if p == TYPE_AUDIO]
    assert audio_seqs[1] == audio_seqs[0] + 1  # no gap

    t.hold_control(False)
    assert sent[-1][0] == TYPE_CHANNEL_ANNOUNCE


def test_ptt_press_and_release_drive_the_hold():
    client, _, _ = _client()
    calls = []
    client.transport.hold_control = calls.append
    client.transport.send_encrypted = lambda *a, **k: None
    client.set_ptt(True)
    client.set_ptt(False)
    assert calls == [True, False]


# --- web UI ----------------------------------------------------------------

def test_webui_monitor_endpoint_applies_live_and_saves(tmp_path):
    import asyncio
    import base64
    import yaml
    from aiohttp.test_utils import TestClient, TestServer
    from outpost.config import load_config
    from outpost.webui import WebUI

    path = tmp_path / "outpost.yaml"
    path.write_text(yaml.safe_dump({
        "mesh_key": base64.b64encode(generate_channel_key()).decode(),
        "channels": [{"id": i, "name": n, "key": base64.b64encode(generate_channel_key()).decode()}
                     for i, n in ((1, "team"), (2, "main"))],
    }))
    client = OutpostClient(load_config(str(path)), "team", config_path=str(path))

    async def run():
        async with TestClient(TestServer(WebUI(client).app)) as tc:
            r = await tc.post("/api/monitor", json={"name": "main", "listen": True, "volume": 0.5})
            assert r.status == 200
            status = await (await tc.get("/api/status")).json()
            main = next(c for c in status["channel_list"] if c["name"] == "main")
            assert main["monitored"] is True and main["volume"] == 0.5
            assert (await tc.post("/api/monitor", json={"name": "nope", "listen": True})).status == 404
            assert (await tc.post("/api/monitor", json={"name": "main", "volume": 2})).status == 400
    asyncio.run(run())

    saved = yaml.safe_load(path.read_text())
    assert saved["monitor_channels"] == ["main"]
    assert saved["channel_volumes"] == {"main": 0.5}
    # And it survives a restart.
    reloaded = OutpostClient(load_config(str(path)), "team")
    assert reloaded.monitored_channel_ids == {2}
    assert reloaded.channel_volume(2) == 0.5
