"""Permission-restricted registry credential storage.

Credentials are intentionally kept outside application, pipeline, job and
audit payloads.  The file lives under the ignored ``instance`` directory and
is readable/writable only by the platform OS user.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from app.config import BASE_DIR
from app.json_store import write_json


DATA_DIR = Path(os.getenv("PLATFORM_DATA_DIR", BASE_DIR / "instance"))
CREDENTIALS_FILE = DATA_DIR / "registry_credentials.json"
PLATFORM_DEFAULT_REFERENCE = "platform:default"
LEGACY_PLATFORM_REFERENCE = "platform"


def _load() -> dict[str, dict[str, str]]:
    if not CREDENTIALS_FILE.exists():
        return {}
    try:
        payload = json.loads(CREDENTIALS_FILE.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_registry_credential(
    reference: str, username: str, credential: str, registry: str = "docker.io"
) -> str:
    if not reference or not username or not credential or credential == "***":
        raise ValueError("Registry credential reference, username and credential are required.")
    records = _load()
    records[reference] = {
        "registry": registry or "docker.io",
        "username": username,
        "credential": credential,
    }
    write_json(CREDENTIALS_FILE, records)
    os.chmod(CREDENTIALS_FILE, 0o600)
    backup = CREDENTIALS_FILE.with_suffix(CREDENTIALS_FILE.suffix + ".bak")
    if backup.exists():
        os.chmod(backup, 0o600)
    return reference


def get_registry_credential(reference: str) -> dict[str, str]:
    record = _load().get(reference, {})
    return dict(record) if isinstance(record, dict) else {}


def get_platform_registry_credential() -> dict[str, str]:
    """Return the shared credential, including legacy key compatibility."""
    return (
        get_registry_credential(PLATFORM_DEFAULT_REFERENCE)
        or get_registry_credential(LEGACY_PLATFORM_REFERENCE)
    )


def resolve_registry_credential(
    application: dict[str, Any],
) -> tuple[dict[str, str], str]:
    """Resolve effective credential without ever embedding it in application data.

    All applications inherit the Platform credential by default. A legacy or
    application-specific credential is used only when ``inherit_platform`` is
    explicitly false.
    """
    registry = application.get("registry") or {}
    inherit_platform = registry.get("inherit_platform", True) is not False
    if inherit_platform:
        platform = get_platform_registry_credential()
        if platform:
            return platform, "platform"
        return {}, "platform_missing"

    reference = str(registry.get("credential_ref", "")).strip()
    stored = get_registry_credential(reference) if reference else {}
    if not stored:
        stored = get_registry_credential(
            f"application:{application.get('id', '')}"
        )
    return (stored, "application") if stored else ({}, "application_missing")


def registry_credential_status(application: dict[str, Any]) -> dict[str, Any]:
    """Expose non-sensitive metadata suitable for templates and APIs."""
    stored, source = resolve_registry_credential(application)
    return {
        "configured": bool(stored.get("username") and stored.get("credential")),
        "source": source,
        "registry": stored.get("registry", "docker.io"),
        "username": stored.get("username", ""),
    }


def remove_registry_credential(reference: str) -> None:
    records = _load()
    if reference not in records:
        return
    del records[reference]
    write_json(CREDENTIALS_FILE, records)
    os.chmod(CREDENTIALS_FILE, 0o600)


def import_legacy_application_credentials(applications: list[dict[str, Any]]) -> int:
    imported = 0
    for application in applications:
        registry = application.get("registry") or {}
        username = str(registry.get("username", "")).strip()
        credential = str(registry.get("token") or registry.get("password") or "").strip()
        if not username or not credential or credential == "***":
            continue
        reference = f"application:{application['id']}"
        if get_registry_credential(reference):
            continue
        save_registry_credential(
            reference, username, credential, str(registry.get("url") or "docker.io")
        )
        imported += 1
    return imported
