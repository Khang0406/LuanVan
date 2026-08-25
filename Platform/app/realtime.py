"""Realtime pipeline event publishing over Redis Pub/Sub.

Message envelope (see docs/phase-a-postgresql-redis-design.md §8):

    {
      "event": "pipeline.stage",
      "pipeline_run_id": "run-map-...",
      "application_id": "map",
      "stage": "DEPLOY",
      "status": "Running",
      "message": "...",
      "ts": "2026-08-25T09:00:00Z"
    }

Every function is a safe no-op when Redis is unavailable, so existing SQLite
dev/test flows are unaffected.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.redis_client import publish


def _channel(run_id: str) -> str:
    return f"platform:pipeline:{run_id}"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _emit(run_id: str, application_id: str, event: str, extra: dict[str, Any]) -> None:
    payload: dict[str, Any] = {
        "event": event,
        "pipeline_run_id": run_id,
        "application_id": application_id,
        "ts": _now(),
    }
    payload.update(extra)
    publish(_channel(run_id), payload)


def pipeline_stage(
    run_id: str,
    application_id: str,
    stage: str,
    status: str,
    message: str = "",
) -> None:
    _emit(
        run_id,
        application_id,
        "pipeline.stage",
        {"stage": stage, "status": status, "message": message},
    )


def pipeline_status(run_id: str, application_id: str, status: str) -> None:
    _emit(run_id, application_id, "pipeline.status", {"status": status})


def pipeline_log(run_id: str, application_id: str, line: str) -> None:
    _emit(run_id, application_id, "pipeline.log", {"line": line})
