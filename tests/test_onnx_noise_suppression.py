"""Tests for OnnxNoiseSuppressor against tiny synthetic ONNX models built
locally with the `onnx` package (no external/pretrained model is fetched -
these models have random/trivial logic, purely to prove the onnxruntime
wiring - shape resolution, float<->int16 conversion, and recurrent-state
passing - works correctly end to end)."""
from __future__ import annotations
import numpy as np
import pytest

onnx = pytest.importorskip("onnx")  # no prebuilt wheel on some platforms (e.g. armv7l); building from source needs CMake>=3.26
from onnx import helper, TensorProto

from outpost import noise_suppression as ns_module
from outpost.noise_suppression import OnnxNoiseSuppressor


def _save(model: onnx.ModelProto, tmp_path, name: str) -> str:
    path = str(tmp_path / name)
    onnx.save(model, path)
    return path


def _make_identity_model(tmp_path) -> str:
    """One dynamic-length float32 audio input straight through to output."""
    audio_in = helper.make_tensor_value_info("audio_in", TensorProto.FLOAT, [1, "frame_len"])
    audio_out = helper.make_tensor_value_info("audio_out", TensorProto.FLOAT, [1, "frame_len"])
    node = helper.make_node("Identity", ["audio_in"], ["audio_out"])
    graph = helper.make_graph([node], "identity", [audio_in], [audio_out])
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)])
    model.ir_version = 10  # cap for compatibility with the installed onnxruntime
    onnx.checker.check_model(model)
    return _save(model, tmp_path, "identity.onnx")


def _make_stateful_model(tmp_path) -> str:
    """audio_out = audio_in + state_in (old state); state_out = state_in +
    0.01. Proves state from a previous process() call measurably affects
    the next call's output, and that the updated state is what gets carried
    forward (small increment avoids saturating the int16 output range)."""
    audio_in = helper.make_tensor_value_info("audio_in", TensorProto.FLOAT, [1, "frame_len"])
    state_in = helper.make_tensor_value_info("state_in", TensorProto.FLOAT, [1, 1])
    audio_out = helper.make_tensor_value_info("audio_out", TensorProto.FLOAT, [1, "frame_len"])
    state_out = helper.make_tensor_value_info("state_out", TensorProto.FLOAT, [1, 1])

    one = helper.make_tensor("one", TensorProto.FLOAT, [1, 1], [0.01])
    add_audio = helper.make_node("Add", ["audio_in", "state_in"], ["audio_out"])
    add_state = helper.make_node("Add", ["state_in", "one"], ["state_out"])
    graph = helper.make_graph(
        [add_audio, add_state], "stateful",
        [audio_in, state_in], [audio_out, state_out],
        initializer=[one],
    )
    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 17)])
    model.ir_version = 10  # cap for compatibility with the installed onnxruntime
    onnx.checker.check_model(model)
    return _save(model, tmp_path, "stateful.onnx")


def _pcm(amplitude: float, n: int = 160) -> bytes:
    return np.full(n, amplitude, dtype=np.int16).tobytes()


def test_identity_model_roundtrips_audio_unchanged(tmp_path):
    suppressor = OnnxNoiseSuppressor(_make_identity_model(tmp_path))
    frame = _pcm(1000.0)

    result = suppressor.process(frame)

    original = np.frombuffer(frame, dtype=np.int16)
    processed = np.frombuffer(result, dtype=np.int16)
    # float32/int16 round-trip isn't bit-exact, but should be very close.
    assert np.allclose(original, processed, atol=2)


def test_stateful_model_carries_state_across_calls(tmp_path):
    suppressor = OnnxNoiseSuppressor(_make_stateful_model(tmp_path))
    silence = _pcm(0.0)

    first = np.frombuffer(suppressor.process(silence), dtype=np.int16)
    second = np.frombuffer(suppressor.process(silence), dtype=np.int16)
    third = np.frombuffer(suppressor.process(silence), dtype=np.int16)

    # state starts at 0, so audio_out = audio_in + 0 on the first call...
    assert np.allclose(first, 0, atol=2)
    # ...then state grows each call, so later outputs should be strictly larger.
    assert np.all(second > first)
    assert np.all(third > second)


def test_missing_onnxruntime_raises_clear_actionable_error(tmp_path, monkeypatch):
    monkeypatch.setattr(ns_module, "ort", None)
    with pytest.raises(RuntimeError, match="pip install -e '.\\[nn\\]'|nn.*extra"):
        OnnxNoiseSuppressor(_make_identity_model(tmp_path))
