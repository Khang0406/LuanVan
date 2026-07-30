"""Permission-restricted secret storage for application secrets.

Secrets are stored outside application/deployment/job/audit payloads under
the ``instance`` directory with restricted file permissions (0o600).

Secrets are NEVER returned in:
- UI API responses
- Deployment manifests (public copies)
- Audit logs, job logs, or pipeline stage messages
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from app.config import BASE_DIR
from app.json_store import write_json

DATA_DIR = Path(os.getenv("PLATFORM_DATA_DIR", BASE_DIR / "instance"))
SECRETS_FILE = DATA_DIR / "application_secrets.json"


def _load_secrets() -> dict[str, dict[str, str]]:
    if not SECRETS_FILE.exists():
        return {}
    try:
        payload = json.loads(SECRETS_FILE.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_secrets(records: dict[str, dict[str, str]]) -> None:
    write_json(SECRETS_FILE, records)
    os.chmod(SECRETS_FILE, 0o600)
    backup = SECRETS_FILE.with_suffix(SECRETS_FILE.suffix + ".bak")
    if backup.exists():
        os.chmod(backup, 0o600)


def save_secret(application_id: str, key: str, value: str) -> None:
    if not application_id or not key or not value or value == "***" or len(value) < 1:
        raise ValueError("Secret requires application_id, key, and non-empty value.")
    records = _load_secrets()
    app_key = f"{application_id}/{key}"
    records[app_key] = {"key": key, "value": value}
    _save_secrets(records)


def get_secret_value(application_id: str, key: str) -> str:
    records = _load_secrets()
    return records.get(f"{application_id}/{key}", {}).get("value", "")


def list_secret_keys(application_id: str) -> list[str]:
    records = _load_secrets()
    prefix = f"{application_id}/"
    return [
        records[k]["key"]
        for k in records
        if k.startswith(prefix)
    ]


def delete_secret(application_id: str, key: str) -> bool:
    records = _load_secrets()
    app_key = f"{application_id}/{key}"
    if app_key not in records:
        return False
    del records[app_key]
    _save_secrets(records)
    return True


def get_secret_checksum(application_id: str) -> str:
    import hashlib
    records = _load_secrets()
    prefix = f"{application_id}/"
    relevant = {k: v for k, v in records.items() if k.startswith(prefix)}
    if not relevant:
        return ""
    payload = json.dumps(relevant, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def get_secret_map(application_id: str) -> dict[str, str]:
    records = _load_secrets()
    prefix = f"{application_id}/"
    return {
        records[k]["key"]: records[k]["value"]
        for k in records
        if k.startswith(prefix)
    }
