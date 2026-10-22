"""Opus audio codec wrapper tuned for low-bitrate voice.

Defaults: 16 kHz mono, 20ms frames, ~16kbps VOIP-tuned bitrate. This keeps
per-packet throughput minimal while remaining clearly intelligible for voice;
raise `bitrate` (and/or sample_rate) for higher fidelity at the cost of
bandwidth.
"""
from __future__ import annotations
import logging
import opuslib
import opuslib.api.encoder

log = logging.getLogger(__name__)

DEFAULT_SAMPLE_RATE = 16000
DEFAULT_CHANNELS = 1
DEFAULT_FRAME_MS = 20
DEFAULT_BITRATE = 16000  # bits/sec


def frame_size(sample_rate: int = DEFAULT_SAMPLE_RATE, frame_ms: int = DEFAULT_FRAME_MS) -> int:
    return sample_rate * frame_ms // 1000


class OpusDecoder:
    """Decode-only Opus state. Opus decoders carry state between frames
    (prediction, concealment), so every sender being played needs its own -
    feeding two senders' frames through one decoder corrupts both."""

    def __init__(self, sample_rate: int = DEFAULT_SAMPLE_RATE, channels: int = DEFAULT_CHANNELS,
                 frame_ms: int = DEFAULT_FRAME_MS):
        self.sample_rate = sample_rate
        self.frame_size = frame_size(sample_rate, frame_ms)
        self._decoder = opuslib.Decoder(sample_rate, channels)

    def decode(self, opus_bytes: bytes) -> bytes:
        return self._decoder.decode(opus_bytes, self.frame_size)

    def decode_loss(self) -> bytes:
        """Packet-loss-concealment audio for a single missing frame."""
        return self._decoder.decode(b"", self.frame_size)

    def decode_fec(self, opus_bytes: bytes) -> bytes:
        """Recover the PREVIOUS (missing) frame from the FEC redundancy in
        `opus_bytes` - must be followed by a normal decode() of `opus_bytes`
        itself for the current frame."""
        return self._decoder.decode(opus_bytes, self.frame_size, decode_fec=True)


class OpusCodec:
    def __init__(
        self,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        channels: int = DEFAULT_CHANNELS,
        frame_ms: int = DEFAULT_FRAME_MS,
        bitrate: int = DEFAULT_BITRATE,
        inband_fec: bool = True,
        expected_packet_loss_perc: int = 10,
    ):
        self.sample_rate = sample_rate
        self.channels = channels
        self.frame_ms = frame_ms
        self.frame_size = frame_size(sample_rate, frame_ms)

        self._encoder = opuslib.Encoder(sample_rate, channels, opuslib.APPLICATION_VOIP)
        try:
            self._encoder.bitrate = bitrate
            if inband_fec:
                # Embeds redundant data for the previous frame in each packet
                # so a single lost frame can often be recovered from the next
                # one received - valuable on lossy mesh/radio links. Costs
                # some extra bitrate, hence tied to the configured loss rate.
                self._encoder.inband_fec = True
                self._encoder.packet_loss_perc = expected_packet_loss_perc
        except (opuslib.OpusError, TypeError):
            # Some ctypes/libopus builds can't marshal the variadic
            # opus_encoder_ctl() call used for this - audio still
            # encodes/decodes fine, it just uses libopus's own defaults
            # instead of our explicit low-throughput/FEC targets. Observed as
            # OpusError on some arm64 Mac builds, and as a TypeError from
            # opuslib's own ctypes wrapper (missing positional arg) on some
            # armv7l builds - same underlying platform limitation either way.
            log.warning(
                "Opus encoder does not support ctl configuration on this platform; "
                "falling back to library defaults (bitrate, FEC)."
            )
        self._decoder = opuslib.Decoder(sample_rate, channels)

    def encode(self, pcm_bytes: bytes, max_bytes: int | None = None) -> bytes:
        """Opus-encodes one frame. `max_bytes` hard-caps the encoded size -
        libopus lowers quality to fit rather than exceeding it. Unlike the
        bitrate setting above, this is a plain argument to opus_encode()
        rather than an encoder_ctl() call, so it's honoured even on builds
        where the ctl calls fail."""
        limit = len(pcm_bytes) if max_bytes is None else max_bytes
        return opuslib.api.encoder.encode(self._encoder.encoder_state, pcm_bytes, self.frame_size, limit)

    def decode(self, opus_bytes: bytes) -> bytes:
        return self._decoder.decode(opus_bytes, self.frame_size)

    def decode_loss(self) -> bytes:
        """Generate packet-loss-concealment audio for a single missing frame."""
        return self._decoder.decode(b"", self.frame_size)

    def decode_fec(self, opus_bytes: bytes) -> bytes:
        """Recover the PREVIOUS (missing) frame using FEC redundancy embedded
        in the current packet. Must be followed by a normal decode() of
        `opus_bytes` itself to get the current frame's audio too."""
        return self._decoder.decode(opus_bytes, self.frame_size, decode_fec=True)
