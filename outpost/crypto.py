"""Per-channel symmetric encryption using XChaCha20-Poly1305 (libsodium's
IETF AEAD construction).

Every packet's cleartext header (version, type, channel id, sender id, seq -
see protocol.py) is passed as associated data, so it's authenticated along
with the payload: changing any header byte - e.g. relabelling a captured
AUDIO packet as a STOP to cut someone off - makes decryption fail. The
header itself stays readable without a key, since relays and routing need
it.

Nonces are derived deterministically from (sender_id, seq) rather than being
transmitted on the wire, saving 24 bytes on every packet. This is safe
because the AEAD's only requirement is that a (key, nonce) pair is never
reused - each sender's strictly increasing seq counter guarantees that for
the lifetime of a channel key.

The configured channel/mesh keys are not used directly: each is run through
keyed BLAKE2b with a protocol-version label first. That keeps this format's
key separate from the one protocol version 1 (XSalsa20-Poly1305 secretbox)
used with the same configured key, so the same keys can be reused across
the upgrade without one key ever serving two ciphers.
"""
from __future__ import annotations
import struct
from typing import TYPE_CHECKING

from nacl import bindings, exceptions, hash as nacl_hash, encoding, utils

if TYPE_CHECKING:
    from .protocol import Header

KEY_SIZE = bindings.crypto_aead_xchacha20poly1305_ietf_KEYBYTES  # 32 bytes
NONCE_SIZE = bindings.crypto_aead_xchacha20poly1305_ietf_NPUBBYTES  # 24 bytes
TAG_SIZE = bindings.crypto_aead_xchacha20poly1305_ietf_ABYTES  # 16 bytes

# BLAKE2b personalization (max 16 bytes) - bump alongside any future change
# to how packets are encrypted.
_KDF_PERSON = b"outpost-v2-aead"


def generate_channel_key() -> bytes:
    return utils.random(KEY_SIZE)


def derive_nonce(sender_id: int, seq: int) -> bytes:
    # 4 bytes sender_id + 8 bytes seq, zero-padded to the required 24 bytes.
    prefix = struct.pack("!IQ", sender_id, seq)
    return prefix + b"\x00" * (NONCE_SIZE - len(prefix))


def _derive_packet_key(key: bytes) -> bytes:
    return nacl_hash.blake2b(
        b"", digest_size=KEY_SIZE, key=key, person=_KDF_PERSON, encoder=encoding.RawEncoder,
    )


class ChannelCipher:
    """Encrypts/decrypts packet payloads for a single channel's (or the
    mesh's) pre-shared key, authenticating the packet header with them."""

    def __init__(self, key: bytes):
        if len(key) != KEY_SIZE:
            raise ValueError(f"channel key must be {KEY_SIZE} bytes, got {len(key)}")
        self._key = _derive_packet_key(key)

    def encrypt(self, header: "Header", plaintext: bytes) -> bytes:
        """Ciphertext + 16-byte tag for `plaintext`, bound to `header` -
        which must be exactly the header the packet is sent with (see
        MeshTransport.send_encrypted). The nonce is implied by the header,
        not included."""
        nonce = derive_nonce(header.sender_id, header.seq)
        return bindings.crypto_aead_xchacha20poly1305_ietf_encrypt(plaintext, header.pack(), nonce, self._key)

    def decrypt(self, header: "Header", ciphertext: bytes) -> bytes:
        nonce = derive_nonce(header.sender_id, header.seq)
        try:
            return bindings.crypto_aead_xchacha20poly1305_ietf_decrypt(ciphertext, header.pack(), nonce, self._key)
        except exceptions.CryptoError as exc:
            raise ValueError("decryption/authentication failed") from exc
