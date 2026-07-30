#!/usr/bin/env python3
"""Restore the Platform SQLite database from a validated backup directory."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path


def restore(backup_dir: Path, destination_dir: Path) -> Path:
    backup_dir = backup_dir.resolve()
    destination_dir = destination_dir.resolve()
    manifest_file = backup_dir / "backup-manifest.json"
    metadata_file = backup_dir / "secret-metadata.json"
    database = backup_dir / "app.db"
    if (
        not manifest_file.is_file()
        or not metadata_file.is_file()
        or not database.is_file()
    ):
        raise FileNotFoundError(
            "Backup manifest, secret metadata or SQLite database is missing"
        )
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    if manifest.get("format") != 1 or manifest.get("secret_values_included") is not False:
        raise ValueError("Unsupported or unsafe backup format")
    metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
    if metadata.get("format") != 1 or not isinstance(metadata.get("stores"), dict):
        raise ValueError("Invalid secret metadata")
    allowed_metadata = {"reference", "key", "registry", "username"}
    for entries in metadata["stores"].values():
        if not isinstance(entries, list) or any(
            not isinstance(entry, dict) or not set(entry).issubset(allowed_metadata)
            for entry in entries
        ):
            raise ValueError("Secret metadata contains unsupported fields")
    with sqlite3.connect(database) as source:
        result = source.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise RuntimeError("Backup database integrity check failed")

    destination_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    target_path = destination_dir / "app.db"
    temporary_path = destination_dir / ".app.db.restore"
    temporary_path.unlink(missing_ok=True)
    with sqlite3.connect(database) as source:
        with sqlite3.connect(temporary_path) as target:
            source.backup(target)
    temporary_path.chmod(0o600)
    os.replace(temporary_path, target_path)
    target_path.chmod(0o600)
    for suffix in ("-wal", "-shm"):
        (destination_dir / f"app.db{suffix}").unlink(missing_ok=True)
    return target_path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backup", required=True, type=Path)
    parser.add_argument(
        "--destination",
        type=Path,
        default=Path(os.getenv("PLATFORM_DATA_DIR", "instance")),
    )
    arguments = parser.parse_args()
    restored = restore(arguments.backup, arguments.destination)
    print(f"Database restored: {restored}")


if __name__ == "__main__":
    main()
