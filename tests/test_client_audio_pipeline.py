"""Verifies OutpostClient wires AGC into the tx path: on by default per
config, overridable via injection, applied before the noise suppressor and
Opus encoding."""
from __future__ import annotations
import asyncio

from outpost.agc import AutomaticGainControl
from outpost.client import OutpostClient
from outpost.config import OutpostConfig, Channel
from outpost.crypto import generate_channel_key


class _SpyProcessor:
    def __init__(self):
        self.calls: list[bytes] = []

    def process(self, pcm_bytes: bytes) -> bytes:
        self.calls.append(pcm_bytes)
        return pcm_bytes


def test_agc_enabled_by_default_from_config():
    client = OutpostClient(_make_config(), "ops")
    assert isinstance(client.agc, AutomaticGainControl)


def test_agc_disabled_when_config_turns_it_off():
    # The AGC object is always constructed (so the web UI can toggle it live
    # without a restart) - config.enable_agc=False just starts it gated off.
    client = OutpostClient(_make_config(enable_agc=False), "ops")
    assert isinstance(client.agc, AutomaticGainControl)
    assert client.agc_enabled is False


def test_web_ui_disabled_by_default_from_config():
    client = OutpostClient(_make_config(), "ops")
    assert client.web_ui is None


def test_web_ui_enabled_from_config():
    from outpost.webui import WebUI
    client = OutpostClient(_make_config(enable_web_ui=True), "ops")
    assert isinstance(client.web_ui, WebUI)


def test_injected_agc_overrides_config():
    spy = _SpyProcessor()
    client = OutpostClient(_make_config(), "ops", agc=spy)
    assert client.agc is spy


def test_tx_loop_runs_agc_before_noise_suppressor():
    asyncio.run(_tx_loop_runs_agc_before_noise_suppressor())


async def _tx_loop_runs_agc_before_noise_suppressor():
    config = _make_config()
    order: list[str] = []

    class _TaggedProcessor:
        def __init__(self, tag: str):
            self.tag = tag

        def process(self, pcm_bytes: bytes) -> bytes:
            order.append(self.tag)
            return pcm_bytes

    client = OutpostClient(
        config, "ops",
        noise_suppressor=_TaggedProcessor("noise_suppressor"),
        agc=_TaggedProcessor("agc"),
    )
    await client.transport.start(config.listen_port)
    client._ptt_active = True

    silence = b"\x00" * (client.codec.frame_size * 2)
    client.mic.read_frame = lambda timeout=None: silence

    try:
        tx_task = asyncio.create_task(client._tx_loop())
        await asyncio.sleep(0.1)
    finally:
        tx_task.cancel()
        client.transport.stop()

    assert len(order) > 0
    assert order[0] == "agc"
    assert order[1] == "noise_suppressor"


def test_client_builds_onnx_suppressor_from_config_model_path(tmp_path):
    import onnx
    from onnx import helper, TensorProto

    audio_in = helper.make_tensor_value_info("audio_in", TensorProto.FLOAT, [1, "frame_len"])
    audio_out = helper.make_tensor_value_info("audio_out", TensorProto.FLOAT, [1, "frame_len"])
    node = helper.make_node("Identity", ["audio_in"], ["audio_out"])
    graph = helper.make_graph([node], "identity", [audio_in], [audio_out])
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)])
    model.ir_version = 10
    model_path = str(tmp_path / "identity.onnx")
    onnx.save(model, model_path)

    from outpost.noise_suppression import OnnxNoiseSuppressor
    config = _make_config(noise_suppressor_model_path=model_path)
    client = OutpostClient(config, "ops")

    assert isinstance(client.noise_suppressor, OnnxNoiseSuppressor)


def _make_config(**overrides) -> OutpostConfig:
    ops_key = generate_channel_key()
    defaults = dict(
        sender_id=1,
        listen_port=0,
        mesh_key=generate_channel_key(),
        channels={1: Channel(id=1, name="ops", key=ops_key)},
        enable_mdns=False,
        enable_pex=False,
    )
    defaults.update(overrides)
    return OutpostConfig(**defaults)
