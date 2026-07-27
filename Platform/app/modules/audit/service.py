import json
from datetime import datetime
from typing import Any

from flask import has_request_context, request
from flask_login import current_user

from app.config import BASE_DIR

DATA_DIR = BASE_DIR / "app" / "data"
AUDIT_FILE = DATA_DIR / "audit_logs.json"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _ensure_audit_file() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not AUDIT_FILE.exists():
        AUDIT_FILE.write_text("[]", encoding="utf-8")


def load_audit_logs(limit: int | None = 200) -> list[dict[str, Any]]:
    _ensure_audit_file()
    with AUDIT_FILE.open("r", encoding="utf-8") as file:
        logs = json.load(file)

    logs = sorted(logs, key=lambda item: item.get("created_at", ""), reverse=True)
    return logs[:limit] if limit else logs


def save_audit_logs(logs: list[dict[str, Any]]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with AUDIT_FILE.open("w", encoding="utf-8") as file:
        json.dump(logs, file, ensure_ascii=False, indent=2)


def record_audit(
    action: str,
    target: str,
    result: str,
    message: str = "",
    user: Any | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    _ensure_audit_file()
    logs = load_audit_logs(limit=None)

    actor = user if user is not None else (current_user if has_request_context() else None)
    username = "anonymous"
    user_id = None
    role = ""
    if getattr(actor, "is_authenticated", False):
        username = getattr(actor, "username", "unknown")
        user_id = getattr(actor, "id", None)
        role = getattr(actor, "role", "")
    elif isinstance(actor, str) and actor:
        username = actor

    entry = {
        "id": f"audit-{datetime.now().strftime('%Y%m%d%H%M%S%f')}",
        "created_at": _now(),
        "time": _now(),
        "user": username,
        "user_id": user_id,
        "role": role,
        "action": action,
        "target": target,
        "result": result.upper(),
        "message": message,
        "ip": request.headers.get("X-Forwarded-For", request.remote_addr or "") if has_request_context() else "",
        "method": request.method if has_request_context() else "",
        "path": request.path if has_request_context() else "",
        "metadata": metadata or {},
    }
    logs.append(entry)
    save_audit_logs(logs)
    return entry
