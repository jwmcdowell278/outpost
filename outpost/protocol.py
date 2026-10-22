"""Wire format for Outpost voice packets.

Layout: cleartext header + encrypted payload (XChaCha20-Poly1305 AEAD
ciphertext, which includes a 16-byte Poly1305 auth tag). The header is
authenticated as the AEAD's associated data - see crypto.py - so it can be
read without a key but not altered without detection.

  offset  size  field
  0       1     version
  1       1     packet type (AUDIO / STOP / PING / PEX / CHANNEL_ANNOUNCE)
  2       1     channel_id (unused/0 for PEX - it is not channel-scoped)
  3       4     sender_id (uint32, big-endian)
  7       8     sequence number (uint64, big-endian)
  15      ...   ciphertext

PEX (peer exchange) packets carry a gossiped peer list encrypted with the
mesh-wide key instead of a channel key - see pex.py.

CHANNEL_ANNOUNCE packets are a tiny empty-payload beacon encrypted with a
channel's own key, flooded to all known peers at a low rate. Only a true
holder of that channel's key can produce one that decrypts, so receivers use
successful decryption to build a per-channel membership table and then
target future AUDIO/STOP sends only at known members instead of flooding
everyone - see client.py's `_channel_announce_loop`/`_channel_target_peers`.

START packets are sent once per PTT press (symmetric with STOP, sent once
per release), carrying the sender's optional alias (UTF-8, possibly empty)
as the encrypted payload - this is how receivers learn who is currently
transmitting without paying per-frame AUDIO overhead for it. See
client.py's `set_ptt`/`_on_packet`/`speaking_details`.

The header is sent in the clear (channel routing/sequencing must be readable
without the key, e.g. by relays) but is authenticated, and the payload
itself is always encrypted - see crypto.py. The encryption nonce is derived from (sender_id, seq), so it never
needs to be transmitted on the wire.
"""
from __future__ import annotations
import struct

# 2: header authenticated as AEAD associated data (XChaCha20-Poly1305).
# 1: payload-only NaCl secretbox - header could be altered undetected.
# Versions don't interoperate; every node must run the same one.
VERSION = 2

TYPE_AUDIO = 1
TYPE_STOP = 2
TYPE_PING = 3
TYPE_PEX = 4
TYPE_CHANNEL_ANNOUNCE = 5
TYPE_START = 6
# Sent by site bridges to their own site, encrypted with the mesh key, so
# several bridges on one site can share the work and fail over - see
# client.py's "redundant bridges". Payload: priority (1 byte), address
# count (1 byte), its IPv4 addresses (4 bytes each), then the sender_ids
# (4 bytes each) of silent bridges it stands in for.
TYPE_BRIDGE_HELLO = 7

_HEADER_FMT = "!BBBIQ"
HEADER_SIZE = struct.calcsize(_HEADER_FMT)  # 15 bytes


class Header:
    __slots__ = ("version", "ptype", "channel_id", "sender_id", "seq")

    def __init__(self, version: int, ptype: int, channel_id: int, sender_id: int, seq: int):
        self.version = version
        self.ptype = ptype
        self.channel_id = channel_id
        self.sender_id = sender_id
        self.seq = seq

    def pack(self) -> bytes:
        return struct.pack(_HEADER_FMT, self.version, self.ptype, self.channel_id, self.sender_id, self.seq)

    @classmethod
    def unpack(cls, data: bytes) -> "Header":
        version, ptype, channel_id, sender_id, seq = struct.unpack(_HEADER_FMT, data[:HEADER_SIZE])
        return cls(version, ptype, channel_id, sender_id, seq)

    def __repr__(self) -> str:
        return (f"Header(version={self.version}, ptype={self.ptype}, channel_id={self.channel_id}, "
                f"sender_id={self.sender_id}, seq={self.seq})")


class UnsupportedVersion(ValueError):
    """Packet from a node running a different protocol version."""

    def __init__(self, version: int):
        super().__init__(f"unsupported protocol version {version} (this node speaks {VERSION})")
        self.version = version


# --- AUDIO payload padding -----------------------------------------------
#
# Opus is variable-bitrate: frame sizes track what's being said (silence is
# a few bytes, loud consonants several times more), and published attacks
# recover words and phrases from encrypted VoIP using packet lengths alone.
# So AUDIO payloads are padded before encryption: opus bytes, then 0x80,
# then zero bytes up to a fixed size (ISO/IEC 7816-4 style - unambiguous
# whatever the opus bytes end with). The sender also caps each Opus frame
# so it always fits (see audio_pad_size and OpusCodec.encode's max_bytes),
# making every AUDIO packet on the wire the same length.


def audio_pad_size(bitrate: int, frame_ms: int) -> int:
    """Fixed AUDIO payload size for a network profile: exactly the
    configured bitrate's share of one frame, so constant-size audio costs
    no more bandwidth than the bitrate the profile already budgets for."""
    return max(16, bitrate * frame_ms // 8000)


def pad_audio(opus_bytes: bytes, size: int | None) -> bytes:
    """`size` None = no length hiding (just the 0x80 marker, so receivers
    unpad the same way either way)."""
    padded = opus_bytes + b"\x80"
    if size is not None and len(padded) < size:
        padded += b"\x00" * (size - len(padded))
    return padded


def unpad_audio(payload: bytes) -> bytes:
    stripped = payload.rstrip(b"\x00")
    if not stripped.endswith(b"\x80"):
        raise ValueError("bad audio padding")
    return stripped[:-1]


def encode_packet(header: Header, ciphertext: bytes) -> bytes:
    return header.pack() + ciphertext


def decode_packet(data: bytes) -> tuple[Header, bytes]:
    if len(data) < HEADER_SIZE:
        raise ValueError("packet shorter than header size")
    header = Header.unpack(data)
    if header.version != VERSION:
        raise UnsupportedVersion(header.version)
    return header, data[HEADER_SIZE:]
