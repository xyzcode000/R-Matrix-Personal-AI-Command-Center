"""Document encryption key setup and plaintext-to-encrypted migration.

Usage (run from the ``backend`` folder with the project virtual environment):

    python -m app.document_keys init      # create DOCUMENT_ENCRYPTION_KEY in .env
    python -m app.document_keys status    # show encryption coverage, never the key
    python -m app.document_keys migrate   # encrypt existing plaintext originals

The key is written to ``backend/.env`` (ignored by git) exactly once. ``init``
refuses to run when a key is already present, so an existing key is never
silently replaced. Losing the key makes encrypted documents unreadable; keep a
copy of it wherever you keep your backups, but never inside the backup folder.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import crypto
from .config import settings


ENV_PATH = Path(__file__).resolve().parents[1] / ".env"
KEY_NAME = "DOCUMENT_ENCRYPTION_KEY"


def _read_env_lines() -> list[str]:
    if not ENV_PATH.exists():
        return []
    return ENV_PATH.read_text(encoding="utf-8").splitlines()


def _has_key(lines: list[str]) -> bool:
    return any(line.strip().startswith(f"{KEY_NAME}=") and line.split("=", 1)[1].strip() for line in lines)


def init() -> int:
    lines = _read_env_lines()
    if _has_key(lines):
        print(f"{KEY_NAME} is already configured; existing key left untouched.")
        return 1
    key = crypto.generate_key()
    payload = [line for line in lines if not line.strip().startswith(f"{KEY_NAME}=")]
    payload.append("")
    payload.append("# Document encryption key (AES-256-GCM). Back this up separately from database backups.")
    payload.append(f"{KEY_NAME}={key}")
    ENV_PATH.write_text("\n".join(payload).strip() + "\n", encoding="utf-8")
    os.environ[KEY_NAME] = key
    print(f"{KEY_NAME} generated and stored in backend/.env. The value was not printed.")
    return 0


def status() -> int:
    documents_dir = settings.documents_dir
    files = sorted(documents_dir.glob("*")) if documents_dir.exists() else []
    encrypted = plaintext = 0
    for path in files:
        if not path.is_file() or path.name.endswith(".encrypting"):
            continue
        try:
            encrypted += 1 if crypto.is_encrypted(path.read_bytes()[:16]) else 0
            plaintext += 0 if crypto.is_encrypted(path.read_bytes()[:16]) else 1
        except OSError:
            continue
    print(f"encryption_configured: {crypto.encryption_enabled()}")
    print(f"stored_files: {encrypted + plaintext}")
    print(f"encrypted_files: {encrypted}")
    print(f"plaintext_files: {plaintext}")
    return 0


def migrate() -> int:
    if not crypto.encryption_enabled():
        print("DOCUMENT_ENCRYPTION_KEY is not configured; run `python -m app.document_keys init` first.")
        return 1
    documents_dir = settings.documents_dir
    if not documents_dir.exists():
        print("No document directory exists yet; nothing to migrate.")
        return 0
    converted = skipped = failed = 0
    for path in sorted(documents_dir.glob("*")):
        if not path.is_file() or path.name.endswith(".encrypting"):
            continue
        try:
            if crypto.encrypt_file(path):
                converted += 1
            else:
                skipped += 1
        except (OSError, crypto.EncryptionUnavailable) as exc:
            failed += 1
            print(f"failed: {path.name}: {type(exc).__name__}")
    print(f"converted: {converted}")
    print(f"already_encrypted: {skipped}")
    print(f"failed: {failed}")
    return 0 if not failed else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage document encryption keys and migration.")
    parser.add_argument("command", choices=("init", "status", "migrate"))
    arguments = parser.parse_args(argv)
    return {"init": init, "status": status, "migrate": migrate}[arguments.command]()


if __name__ == "__main__":
    sys.exit(main())
