"""Consistent local backups, verification and isolated restores.

Usage (from the ``backend`` directory, with the project virtual environment):

    python -m app.backup create
    python -m app.backup create --dest D:\\backups\\r-matrix --no-documents
    python -m app.backup verify --backup D:\\backups\\r-matrix\\r-matrix-backup-20260101T000000Z
    python -m app.backup restore --backup <backup-dir> --data-dir D:\\restore-test

A restore never targets the live data directory unless that directory is empty,
so the running application's data is not overwritten by mistake.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import settings

BACKUP_PREFIX = "r-matrix-backup-"
MANIFEST_NAME = "manifest.json"
DATABASES = ("r_matrix.sqlite3", "control_plane.sqlite3")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _integrity(database_path: Path) -> str:
    connection = sqlite3.connect(f"file:{database_path.as_posix()}?mode=ro", uri=True)
    try:
        row = connection.execute("PRAGMA integrity_check").fetchone()
        return str(row[0]) if row else "unknown"
    finally:
        connection.close()


def _copy_database(source: Path, destination: Path) -> None:
    with sqlite3.connect(source) as source_connection:
        with sqlite3.connect(destination) as target_connection:
            source_connection.backup(target_connection)


def create_backup(
    destination_root: Path | None = None,
    *,
    include_documents: bool = True,
    data_dir: Path | None = None,
) -> Path:
    root = destination_root or (settings.data_dir / "backups")
    root.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = root / f"{BACKUP_PREFIX}{timestamp}"
    if target.exists():
        raise FileExistsError(f"A backup already exists at {target}; nothing was overwritten.")
    target.mkdir(parents=True)
    active_data_dir = data_dir or settings.data_dir
    files: list[dict[str, Any]] = []
    for name in DATABASES:
        source = active_data_dir / name
        if not source.exists():
            continue
        copy = target / name
        _copy_database(source, copy)
        files.append(
            {
                "name": name,
                "bytes": copy.stat().st_size,
                "sha256": _sha256(copy),
                "integrity_check": _integrity(copy),
            }
        )
    documents_copied = 0
    documents_dir = active_data_dir / "documents"
    if include_documents and documents_dir.is_dir():
        destination = target / "documents"
        shutil.copytree(documents_dir, destination)
        documents_copied = sum(1 for item in destination.rglob("*") if item.is_file())
    manifest = {
        "format_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "application": "R Matrix Personal AI Command Center",
        "data_dir": str(active_data_dir),
        "databases": files,
        "documents": {
            "included": bool(include_documents and documents_dir.is_dir()),
            "files": documents_copied,
        },
        "excluded": [
            "backend/.env and any environment secrets",
            "temporary SQLite -wal/-shm files",
            "logs and previous backups",
        ],
    }
    (target / MANIFEST_NAME).write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    return target


def _load_manifest(backup_dir: Path) -> dict[str, Any]:
    manifest_path = backup_dir / MANIFEST_NAME
    if not manifest_path.is_file():
        raise FileNotFoundError(f"No {MANIFEST_NAME} found in {backup_dir}.")
    return json.loads(manifest_path.read_text(encoding="utf-8"))


def verify_backup(backup_dir: Path) -> dict[str, Any]:
    """Verifies checksums and opens each database copy in an isolated temp folder."""
    manifest = _load_manifest(backup_dir)
    report: dict[str, Any] = {"backup": str(backup_dir), "databases": [], "documents": manifest.get("documents", {})}
    with tempfile.TemporaryDirectory(prefix="r-matrix-verify-") as temporary:
        temporary_path = Path(temporary)
        for entry in manifest.get("databases", []):
            source = backup_dir / entry["name"]
            if not source.is_file():
                report["databases"].append(
                    {"name": entry["name"], "ok": False, "error": "file is missing"}
                )
                continue
            checksum_matches = _sha256(source) == entry.get("sha256")
            isolated = temporary_path / entry["name"]
            shutil.copy2(source, isolated)
            integrity = _integrity(isolated)
            tables = 0
            connection = sqlite3.connect(f"file:{isolated.as_posix()}?mode=ro", uri=True)
            try:
                tables = connection.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"
                ).fetchone()[0]
            finally:
                connection.close()
            report["databases"].append(
                {
                    "name": entry["name"],
                    "ok": checksum_matches and integrity == "ok",
                    "checksum_matches": checksum_matches,
                    "integrity_check": integrity,
                    "tables": tables,
                }
            )
    report["ok"] = all(item["ok"] for item in report["databases"]) and bool(report["databases"])
    return report


def restore_backup(backup_dir: Path, target_data_dir: Path) -> Path:
    """Restores into an empty directory only; never overwrites live application data."""
    manifest = _load_manifest(backup_dir)
    target = target_data_dir.expanduser().resolve()
    if target == settings.data_dir.resolve():
        raise ValueError(
            "Refusing to restore over the live data directory. Choose an empty directory, "
            "verify the restored copy, then move it into place while the backend is stopped."
        )
    if target.exists() and any(target.iterdir()):
        raise ValueError(f"{target} is not empty; choose an empty restore directory.")
    target.mkdir(parents=True, exist_ok=True)
    for entry in manifest.get("databases", []):
        source = backup_dir / entry["name"]
        if source.is_file():
            shutil.copy2(source, target / entry["name"])
    documents_source = backup_dir / "documents"
    if documents_source.is_dir():
        shutil.copytree(documents_source, target / "documents", dirs_exist_ok=True)
    return target


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Back up, verify or restore R Matrix personal data."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", help="Create a consistent backup")
    create.add_argument("--dest", type=Path, default=None, help="Backup root directory")
    create.add_argument(
        "--no-documents", action="store_true", help="Skip uploaded document files"
    )

    verify = subparsers.add_parser("verify", help="Verify a backup in an isolated location")
    verify.add_argument("--backup", type=Path, required=True)

    restore = subparsers.add_parser("restore", help="Restore into an empty directory")
    restore.add_argument("--backup", type=Path, required=True)
    restore.add_argument("--data-dir", type=Path, required=True)

    arguments = parser.parse_args(argv)
    if arguments.command == "create":
        target = create_backup(
            arguments.dest, include_documents=not arguments.no_documents
        )
        print(f"Backup created: {target}")
        print("Run: python -m app.backup verify --backup " + f'"{target}"')
        return 0
    if arguments.command == "verify":
        report = verify_backup(arguments.backup)
        print(json.dumps(report, indent=2))
        return 0 if report["ok"] else 1
    restored = restore_backup(arguments.backup, arguments.data_dir)
    print(f"Restored into {restored}")
    print(
        "To use it: stop the backend, move the databases and documents folder into your "
        "data directory, then restart the backend."
    )
    return 0


if __name__ == "__main__":
    sys.exit(_main())
