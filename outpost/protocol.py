"""Wire format for Outpost voice packets.

Layout: cleartext header + encrypted payload (NaCl secretbox ciphertext,
which includes a 16-byte Poly1305 auth tag).

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

The header is sent in the clear (channel routing/sequencing must be readable
without the key) but the audio payload itself is always encrypted - see
crypto.py. The encryption nonce is derived from (sender_id, seq), so it never
needs to be transmitted on the wire.
"""
from __future__ import annotations
import struct

VERSION = 1

TYPE_AUDIO = 1
TYPE_STOP = 2
TYPE_PING = 3
TYPE_PEX = 4
TYPE_CHANNEL_ANNOUNCE = 5

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


def encode_packet(header: Header, ciphertext: bytes) -> bytes:
    return header.pack() + ciphertext


def decode_packet(data: bytes) -> tuple[Header, bytes]:
    if len(data) < HEADER_SIZE:
        raise ValueError("packet shorter than header size")
    header = Header.unpack(data)
    if header.version != VERSION:
        raise ValueError(f"unsupported protocol version {header.version}")
    return header, data[HEADER_SIZE:]
