import json
import os
import re
import tempfile
import threading
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable, TypeVar


_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, threading.RLock] = {}
T = TypeVar("T")

_SECRET_KEY = re.compile(
    r"(password|passwd|pwd|token|secret|api[_-]?key|authorization)", re.IGNORECASE
)
_SECRET_TEXT_PATTERNS = (
    re.compile(r"(?i)(--password(?:-stdin)?(?:=|\s+))(\S+)"),
    re.compile(r"(?i)\b(password|passwd|pwd|token|secret|api[_-]?key)\s*([=:])\s*([^\s,;]+)"),
    re.compile(r"(?i)(authorization\s*:\s*(?:bearer|basic)\s+)(\S+)"),
    re.compile(
        r"(?im)^(\s*[A-Za-z0-9_./-]*(?:password|passwd|pwd|token|secret|api[_-]?key|authorization)"
        r"[A-Za-z0-9_./-]*\s*:\s*)(\S+)"
    ),
)

STATUS_ALIASES = {
    "complete": "Success", "completed": "Success", "done": "Success",
    "ok": "Success", "succeeded": "Success", "success": "Success",
    "error": "Failed", "failure": "Failed", "failed": "Failed",
    "in progress": "Running", "in_progress": "Running",
    "pending": "Waiting", "queued": "Waiting", "waiting": "Waiting",
    "cancelled": "Cancelled", "canceled": "Cancelled",
    "interrupted": "Interrupted", "skipped": "Skipped",
}


def _lock_for(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.RLock())


def read_json(
    path: Path,
    default: T,
    validator: Callable[[Any], bool] | None = None,
) -> T:
    """Read JSON without exposing callers to a partially written file."""
    lock = _lock_for(path)
    with lock:
        if not path.exists():
            return deepcopy(default)
        try:
            with path.open("r", encoding="utf-8") as file:
                payload = json.load(file)
                if validator is not None and not validator(payload):
                    raise ValueError("JSON payload failed validation")
                return payload
        except (json.JSONDecodeError, OSError, ValueError):
            backup_path = path.with_suffix(path.suffix + ".bak")
            if backup_path.exists():
                try:
                    with backup_path.open("r", encoding="utf-8") as file:
                        payload = json.load(file)
                        if validator is not None and not validator(payload):
                            raise ValueError("Backup JSON payload failed validation")
                        return payload
                except (json.JSONDecodeError, OSError, ValueError):
                    pass
            return deepcopy(default)


def write_json(path: Path, payload: Any) -> None:
    """Atomically replace a JSON file and retain the previous valid snapshot."""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = _lock_for(path)
    with lock:
        if path.exists():
            backup_path = path.with_suffix(path.suffix + ".bak")
            try:
                backup_path.write_bytes(path.read_bytes())
            except OSError:
                pass

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
        )
        temporary_path = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as file:
                json.dump(payload, file, ensure_ascii=False, indent=2)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary_path, path)
            _fsync_directory(path.parent)
        finally:
            temporary_path.unlink(missing_ok=True)


def update_json(
    path: Path,
    default: T,
    updater: Callable[[T], T | None],
    validator: Callable[[Any], bool] | None = None,
) -> T:
    """Atomically perform a read-modify-write operation under one path lock."""
    lock = _lock_for(path)
    with lock:
        current = read_json(path, default, validator)
        updated = updater(deepcopy(current))
        if updated is None:
            return current
        write_json(path, updated)
        return updated


def is_list_of_dicts(payload: Any) -> bool:
    return isinstance(payload, list) and all(isinstance(item, dict) for item in payload)


def normalize_status(status: Any, default: str = "Waiting") -> str:
    if not isinstance(status, str) or not status.strip():
        return default
    value = status.strip()
    return STATUS_ALIASES.get(value.lower(), value)


def mask_secrets(value: Any) -> Any:
    """Return a copy safe for logs/audits while preserving non-secret data."""
    if isinstance(value, dict):
        return {
            key: ("***" if _SECRET_KEY.search(str(key)) else mask_secrets(item))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [mask_secrets(item) for item in value]
    if isinstance(value, tuple):
        return tuple(mask_secrets(item) for item in value)
    if not isinstance(value, str):
        return value

    masked = value
    masked = _SECRET_TEXT_PATTERNS[0].sub(r"\1***", masked)
    masked = _SECRET_TEXT_PATTERNS[1].sub(r"\1\2***", masked)
    masked = _SECRET_TEXT_PATTERNS[2].sub(r"\1***", masked)
    masked = _SECRET_TEXT_PATTERNS[3].sub(r"\1***", masked)
    return masked


def _fsync_directory(directory: Path) -> None:
    """Best-effort durability for the atomic rename itself."""
    try:
        descriptor = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)
