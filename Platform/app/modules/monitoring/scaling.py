"""Persist HPA replica transitions as compact scale events."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.config import BASE_DIR
from app.json_store import is_list_of_dicts, read_json, write_json

SCALE_EVENTS_FILE = BASE_DIR / "app" / "data" / "scale_events.json"


def load_scale_events(application_id: str = "") -> list[dict[str, Any]]:
    events = read_json(SCALE_EVENTS_FILE, [], is_list_of_dicts)
    return [event for event in events if not application_id or event.get("application_id") == application_id]


def record_scale_events(app_metrics: list[dict[str, Any]]) -> list[dict[str, Any]]:
    events = load_scale_events()
    latest: dict[tuple[str, str], int] = {}
    for event in events:
        latest.setdefault(
            (event.get("application_id", ""), event.get("hpa", "")),
            int(event.get("replicas", 0)),
        )
    changed: list[dict[str, Any]] = []
    for app in app_metrics:
        for hpa in app.get("hpas", []):
            key = (app.get("app_id", ""), hpa.get("name", ""))
            replicas = int(hpa.get("current_replicas", 0))
            if latest.get(key) == replicas:
                continue
            event = {
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "application_id": key[0],
                "namespace": app.get("namespace", ""),
                "hpa": key[1],
                "replicas": replicas,
                "max_replicas": int(hpa.get("max_replicas", 0)),
                "direction": "initial" if key not in latest else (
                    "up" if replicas > latest[key] else "down"
                ),
            }
            events.insert(0, event)
            changed.append(event)
            latest[key] = replicas
    write_json(SCALE_EVENTS_FILE, events[:500])
    return changed
