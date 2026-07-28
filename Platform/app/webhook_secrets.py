"""Permission-restricted GitHub webhook secret storage."""

import json
import os
from pathlib import Path

from app.config import BASE_DIR
from app.json_store import write_json


DATA_DIR = Path(os.getenv("PLATFORM_DATA_DIR", BASE_DIR / "instance"))
WEBHOOK_SECRETS_FILE = DATA_DIR / "webhook_secrets.json"


def save_webhook_secret(application_id: str, secret: str) -> str:
    if not application_id or not secret:
        raise ValueError("Application ID and webhook secret are required.")
    records = {}
    if WEBHOOK_SECRETS_FILE.exists():
        try:
            records = json.loads(WEBHOOK_SECRETS_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            records = {}
    reference = f"application:{application_id}"
    records[reference] = secret
    write_json(WEBHOOK_SECRETS_FILE, records)
    os.chmod(WEBHOOK_SECRETS_FILE, 0o600)
    return reference


def get_webhook_secret(reference: str, application_id: str = "") -> str:
    env_name = f"GITHUB_WEBHOOK_SECRET_{application_id.upper().replace('-', '_')}"
    configured = os.getenv(env_name) or os.getenv("GITHUB_WEBHOOK_SECRET", "")
    if configured:
        return configured
    if not reference or not WEBHOOK_SECRETS_FILE.exists():
        return ""
    try:
        records = json.loads(WEBHOOK_SECRETS_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return str(records.get(reference, ""))
