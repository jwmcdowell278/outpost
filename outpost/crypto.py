"""Per-channel symmetric encryption using XSalsa20-Poly1305 (NaCl secretbox).

Nonces are derived deterministically from (sender_id, seq) rather than being
transmitted on the wire, saving 24 bytes on every packet. This is safe for
secretbox because its only requirement is that a (key, nonce) pair is never
reused - each sender's strictly increasing seq counter guarantees that for
the lifetime of a channel key.
"""
from __future__ import annotations
import struct
from nacl import secret, utils, exceptions

NONCE_SIZE = secret.SecretBox.NONCE_SIZE  # 24 bytes
KEY_SIZE = secret.SecretBox.KEY_SIZE  # 32 bytes


def generate_channel_key() -> bytes:
    return utils.random(KEY_SIZE)


def derive_nonce(sender_id: int, seq: int) -> bytes:
    # 4 bytes sender_id + 8 bytes seq, zero-padded to the required 24 bytes.
    prefix = struct.pack("!IQ", sender_id, seq)
    return prefix + b"\x00" * (NONCE_SIZE - len(prefix))


class ChannelCipher:
    """Encrypts/decrypts Opus frames for a single channel's pre-shared key."""

    def __init__(self, key: bytes):
        if len(key) != KEY_SIZE:
            raise ValueError(f"channel key must be {KEY_SIZE} bytes, got {len(key)}")
        self._box = secret.SecretBox(key)

    def encrypt(self, sender_id: int, seq: int, plaintext: bytes) -> bytes:
        nonce = derive_nonce(sender_id, seq)
        encrypted = self._box.encrypt(plaintext, nonce)
        return encrypted.ciphertext  # nonce is implied by the packet header, not resent

    def decrypt(self, sender_id: int, seq: int, ciphertext: bytes) -> bytes:
        nonce = derive_nonce(sender_id, seq)
        try:
            return self._box.decrypt(ciphertext, nonce)
        except exceptions.CryptoError as exc:
            raise ValueError("decryption/authentication failed") from exc
