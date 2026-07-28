#!/usr/bin/env python3
"""Create a consistent, permission-restricted Platform data backup."""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


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
    (destination / "app.db").chmod(0o600)

    for name in (
        "registry_credentials.json",
        "registry_credentials.json.bak",
        "webhook_secrets.json",
    ):
        source_file = source_dir / name
        if source_file.is_file():
            target_file = destination / name
            shutil.copy2(source_file, target_file)
            target_file.chmod(0o600)
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
