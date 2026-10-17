"""Opus audio codec wrapper tuned for low-bitrate voice.

Defaults: 16 kHz mono, 20ms frames, ~16kbps VOIP-tuned bitrate. This keeps
per-packet throughput minimal while remaining clearly intelligible for voice;
raise `bitrate` (and/or sample_rate) for higher fidelity at the cost of
bandwidth.
"""
from __future__ import annotations
import logging
import opuslib

log = logging.getLogger(__name__)

DEFAULT_SAMPLE_RATE = 16000
DEFAULT_CHANNELS = 1
DEFAULT_FRAME_MS = 20
DEFAULT_BITRATE = 16000  # bits/sec


def frame_size(sample_rate: int = DEFAULT_SAMPLE_RATE, frame_ms: int = DEFAULT_FRAME_MS) -> int:
    return sample_rate * frame_ms // 1000


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
        except opuslib.OpusError:
            # Some ctypes/libopus builds (observed on arm64 Macs) can't marshal
            # the variadic opus_encoder_ctl() call used for this - audio still
            # encodes/decodes fine, it just uses libopus's own defaults
            # instead of our explicit low-throughput/FEC targets.
            log.warning(
                "Opus encoder does not support ctl configuration on this platform; "
                "falling back to library defaults (bitrate, FEC)."
            )
        self._decoder = opuslib.Decoder(sample_rate, channels)

    def encode(self, pcm_bytes: bytes) -> bytes:
        return self._encoder.encode(pcm_bytes, self.frame_size)

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
