"""Pluggable noise-suppression stage applied to captured PCM before Opus
encoding (mirrors the `PTTSource` pattern: a narrow interface, injected
rather than hardcoded, so a real backend can be swapped in without touching
the client).

Ships with a no-op default (`PassthroughSuppressor`) and a real backend
(`OnnxNoiseSuppressor`) that runs a user-supplied ONNX speech-enhancement
model via `onnxruntime`. No specific model is bundled - picking and vetting
a pretrained model is a decision for whoever deploys this, not something
this project should fetch/trust on your behalf (see README).
"""
from __future__ import annotations
from typing import Protocol
import numpy as np

try:
    import onnxruntime as ort
except ImportError:
    ort = None


class NoiseSuppressor(Protocol):
    def process(self, pcm_bytes: bytes) -> bytes:
        """Takes one raw 16-bit PCM frame, returns a frame of the same size."""
        ...


class PassthroughSuppressor:
    """No-op default - used whenever no real suppressor is configured."""

    def process(self, pcm_bytes: bytes) -> bytes:
        return pcm_bytes


class OnnxNoiseSuppressor:
    """Runs a user-supplied ONNX speech-enhancement model frame-by-frame.

    Supports the common "streaming" export shape used by lightweight
    real-time speech-enhancement models (e.g. DTLN, Silero's denoiser): one
    float32 audio-frame input/output pair, plus zero or more recurrent-state
    tensor pairs carried forward call-to-call (matched positionally - first
    extra input <-> first extra output, etc; override via
    `audio_input_name`/`audio_output_name` if a model's audio tensors
    aren't the first declared input/output). State tensors start at zero,
    matching a fresh "cold start" the first time PTT is pressed.

    Audio is assumed float32 in [-1, 1] (the standard convention for these
    models) and is converted to/from this project's 16-bit PCM frames.
    """

    def __init__(
        self,
        model_path: str,
        audio_input_name: str | None = None,
        audio_output_name: str | None = None,
    ):
        if ort is None:
            raise RuntimeError(
                "OnnxNoiseSuppressor requires onnxruntime - install the 'nn' "
                "extra: pip install -e '.[nn]'"
            )
        self._session = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
        inputs = self._session.get_inputs()
        outputs = self._session.get_outputs()
        if not inputs or not outputs:
            raise ValueError(f"model at {model_path!r} has no declared inputs/outputs")

        self._audio_input = audio_input_name or inputs[0].name
        self._audio_output = audio_output_name or outputs[0].name

        state_inputs = [i for i in inputs if i.name != self._audio_input]
        state_outputs = [o for o in outputs if o.name != self._audio_output]
        if len(state_inputs) != len(state_outputs):
            raise ValueError(
                f"model at {model_path!r} has {len(state_inputs)} non-audio "
                f"inputs but {len(state_outputs)} non-audio outputs - can't "
                "infer state-passing pairing; pass explicit audio_input_name/"
                "audio_output_name."
            )
        self._state_pairs = list(zip(state_inputs, state_outputs))
        self._state = {inp.name: _zeros_for(inp) for inp, _ in self._state_pairs}

    def process(self, pcm_bytes: bytes) -> bytes:
        samples = np.frombuffer(pcm_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        audio_shape = _resolve_audio_shape(
            next(i for i in self._session.get_inputs() if i.name == self._audio_input).shape,
            len(samples),
        )

        feed = {self._audio_input: samples.reshape(audio_shape)}
        feed.update(self._state)

        output_values = self._session.run(None, feed)
        output_by_name = dict(zip([o.name for o in self._session.get_outputs()], output_values))

        for inp, out in self._state_pairs:
            self._state[inp.name] = output_by_name[out.name]

        processed = output_by_name[self._audio_output].reshape(-1)
        pcm = np.clip(processed * 32768.0, -32768, 32767).astype(np.int16)
        return pcm.tobytes()


def _zeros_for(value_info) -> np.ndarray:
    shape = [dim if isinstance(dim, int) and dim > 0 else 1 for dim in value_info.shape]
    return np.zeros(shape, dtype=np.float32)


def _resolve_audio_shape(declared_shape: list, frame_len: int) -> list:
    """Fills in the captured frame length for any dynamic/symbolic axis,
    assumed to be the time axis (honors a model's own static dims as-is -
    e.g. a model requiring an exact fixed block size). Any other dynamic
    axes are assumed to be batch/channel dims and filled with 1."""
    dynamic_positions = [i for i, dim in enumerate(declared_shape) if not (isinstance(dim, int) and dim > 0)]
    time_axis = dynamic_positions[-1] if dynamic_positions else len(declared_shape) - 1

    resolved = []
    for i, dim in enumerate(declared_shape):
        if isinstance(dim, int) and dim > 0:
            resolved.append(dim)
        elif i == time_axis:
            resolved.append(frame_len)
        else:
            resolved.append(1)
    return resolved
