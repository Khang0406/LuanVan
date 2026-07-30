#!/usr/bin/env python3
"""Create a consistent backup without placing plaintext secrets in it."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SECRET_STORES = {
    "application_secrets.json": "application",
    "registry_credentials.json": "registry",
    "webhook_secrets.json": "webhook",
}


def _read_object(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _secret_metadata(source_dir: Path) -> dict[str, Any]:
    """Return references/keys only; never values, passwords or tokens."""
    result: dict[str, Any] = {"format": 1, "stores": {}}
    for filename, store_type in SECRET_STORES.items():
        records = _read_object(source_dir / filename)
        entries: list[dict[str, str]] = []
        for reference, record in sorted(records.items()):
            entry = {"reference": str(reference)}
            if store_type == "application" and isinstance(record, dict):
                entry["key"] = str(record.get("key", ""))
            elif store_type == "registry" and isinstance(record, dict):
                entry["registry"] = str(record.get("registry", ""))
                entry["username"] = str(record.get("username", ""))
            entries.append(entry)
        result["stores"][store_type] = entries
    return result


def backup(source_dir: Path, destination_root: Path) -> Path:
    source_dir = source_dir.resolve()
    database = source_dir / "app.db"
    if not database.is_file():
        raise FileNotFoundError(f"Platform database not found: {database}")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = destination_root.resolve() / f"platform-{timestamp}"
    destination.mkdir(parents=True, mode=0o700, exist_ok=False)
    destination.chmod(0o700)

    with sqlite3.connect(database) as source:
        with sqlite3.connect(destination / "app.db") as target:
            source.backup(target)
            result = target.execute("PRAGMA integrity_check").fetchone()
            if not result or result[0] != "ok":
                raise RuntimeError("Backup database integrity check failed")
    (destination / "app.db").chmod(0o600)

    metadata_file = destination / "secret-metadata.json"
    metadata_file.write_text(
        json.dumps(_secret_metadata(source_dir), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    metadata_file.chmod(0o600)

    manifest = {
        "format": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "database": "app.db",
        "secret_backup": "metadata-only",
        "secret_values_included": False,
    }
    manifest_file = destination / "backup-manifest.json"
    manifest_file.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    manifest_file.chmod(0o600)
    return destination


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source",
        type=Path,
        default=Path(os.getenv("PLATFORM_DATA_DIR", "instance")),
    )
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    destination = backup(arguments.source, arguments.output)
    print(f"Backup created: {destination}")


if __name__ == "__main__":
    main()
