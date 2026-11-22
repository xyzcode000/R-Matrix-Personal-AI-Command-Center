"""Authenticated encryption for stored document files.

Original uploads are written with AES-256-GCM using a dedicated key from
``DOCUMENT_ENCRYPTION_KEY`` (base64, 32 random bytes). The key is deliberately
separate from ``AUTH_JWT_SECRET``.

Encrypted files start with :data:`MAGIC`, so documents stored before encryption
was enabled stay readable and can be migrated one at a time. Anything that
cannot be decrypted fails closed instead of returning partial bytes.
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAGIC = b"RMXENC1\x00"
NONCE_BYTES = 12
KEY_BYTES = 32
_ASSOCIATED_DATA = b"r-matrix-document-v1"


class EncryptionUnavailable(RuntimeError):
    """Raised when encrypted content cannot be handled safely."""


def decode_key(raw: str) -> bytes:
    value = (raw or "").strip()
    if not value:
        raise EncryptionUnavailable("DOCUMENT_ENCRYPTION_KEY is not configured.")
    try:
        key = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise EncryptionUnavailable("DOCUMENT_ENCRYPTION_KEY is not valid base64.") from exc
    if len(key) != KEY_BYTES:
        raise EncryptionUnavailable("DOCUMENT_ENCRYPTION_KEY must decode to exactly 32 bytes.")
    return key


def current_key() -> bytes | None:
    """Returns the configured key, or None when encryption is not set up yet."""
    raw = os.getenv("DOCUMENT_ENCRYPTION_KEY", "").strip()
    if not raw:
        return None
    return decode_key(raw)


def encryption_enabled() -> bool:
    return bool(os.getenv("DOCUMENT_ENCRYPTION_KEY", "").strip())


def is_encrypted(data: bytes) -> bool:
    return data.startswith(MAGIC)


def encrypt_bytes(data: bytes) -> bytes:
    key = current_key()
    if key is None:
        raise EncryptionUnavailable("Document encryption is not configured.")
    nonce = os.urandom(NONCE_BYTES)
    sealed = AESGCM(key).encrypt(nonce, data, _ASSOCIATED_DATA)
    return MAGIC + nonce + sealed


def decrypt_bytes(data: bytes) -> bytes:
    """Returns plaintext for stored document bytes.

    Plaintext (legacy) files pass through unchanged. Encrypted files require the
    matching key; a missing or wrong key raises instead of guessing.
    """
    if not is_encrypted(data):
        return data
    key = current_key()
    if key is None:
        raise EncryptionUnavailable(
            "This document is encrypted but DOCUMENT_ENCRYPTION_KEY is not configured. "
            "Restore the original key to open it; the key is never regenerated automatically."
        )
    body = data[len(MAGIC):]
    if len(body) <= NONCE_BYTES:
        raise EncryptionUnavailable("The encrypted document is truncated and cannot be opened.")
    nonce, sealed = body[:NONCE_BYTES], body[NONCE_BYTES:]
    try:
        return AESGCM(key).decrypt(nonce, sealed, _ASSOCIATED_DATA)
    except InvalidTag as exc:
        raise EncryptionUnavailable(
            "The stored document could not be decrypted with the configured key. "
            "The key may have changed or the file may be corrupted."
        ) from exc


def generate_key() -> str:
    return base64.b64encode(os.urandom(KEY_BYTES)).decode("ascii")


def encrypt_file(path: Path) -> bool:
    """Encrypts an existing plaintext document file in place.

    Returns True when the file was converted, False when it was already
    encrypted. The plaintext is only replaced after the encrypted payload has
    been fully written and re-read successfully.
    """
    data = path.read_bytes()
    if is_encrypted(data):
        return False
    sealed = encrypt_bytes(data)
    temporary = path.with_name(path.name + ".encrypting")
    try:
        temporary.write_bytes(sealed)
        if temporary.read_bytes() != sealed:
            raise EncryptionUnavailable("The encrypted copy could not be verified.")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return True
