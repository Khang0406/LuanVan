"""Celery pipeline worker — Redis-backed replacement for the SQLite claim loop.

Run with::

    python run_worker.py                      # loads .env first (recommended)
    celery -A app.worker.celery_app worker    # when env vars are already exported

The old ``python -m app.pipeline_worker`` polling loop remains available as a
fallback when ``REDIS_URL`` is unset. This module only needs to be launched when
Redis is configured (``REDIS_URL`` or ``CELERY_BROKER_URL``).
"""

from __future__ import annotations

import os
import threading

from celery import Celery
from celery.signals import worker_ready

BROKER_URL = os.getenv(
    "REDIS_URL", os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/0")
)
RESULT_BACKEND = os.getenv("CELERY_RESULT_BACKEND", BROKER_URL)

celery_app = Celery(
    "platform",
    broker=BROKER_URL,
    backend=RESULT_BACKEND,
    include=["app.worker"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_track_started=True,
    # Keep the global concurrency semantics from PIPELINE_MAX_CONCURRENT when
    # the operator does not pass an explicit --concurrency.
    worker_concurrency=max(1, int(os.getenv("PIPELINE_MAX_CONCURRENT", "4"))),
    broker_connection_retry_on_startup=True,
    broker_connection_max_retries=None,
    broker_transport_options={
        "visibility_timeout": max(
            300, int(os.getenv("CELERY_VISIBILITY_TIMEOUT", "7200"))
        ),
    },
    task_soft_time_limit=max(
        60, int(os.getenv("PIPELINE_TASK_SOFT_TIME_LIMIT", "3300"))
    ),
    task_time_limit=max(90, int(os.getenv("PIPELINE_TASK_TIME_LIMIT", "3600"))),
    result_expires=max(300, int(os.getenv("CELERY_RESULT_EXPIRES", "86400"))),
)


@celery_app.task(name="platform.run_pipeline", bind=True, acks_late=True)
def run_pipeline(self, pipeline_run_id: str) -> dict:
    """Claim and execute one pipeline; duplicate Celery deliveries are no-ops."""
    from app.delivery_store import mark_pipeline_running, renew_pipeline_lease
    from app.modules.pipeline.engine import _run_pipeline_safely

    worker_id = self.request.id
    lease_seconds = max(30, int(os.getenv("PIPELINE_LEASE_SECONDS", "300")))
    run = mark_pipeline_running(
        pipeline_run_id, worker_id=worker_id, lease_seconds=lease_seconds
    )
    if not run:
        return {"pipeline_run_id": pipeline_run_id, "status": "not_claimed"}

    stop_heartbeat = threading.Event()
    heartbeat_interval = max(
        5,
        min(
            lease_seconds // 3,
            int(os.getenv("PIPELINE_HEARTBEAT_SECONDS", "30")),
        ),
    )

    def heartbeat() -> None:
        while not stop_heartbeat.wait(heartbeat_interval):
            if not renew_pipeline_lease(
                pipeline_run_id, worker_id, lease_seconds=lease_seconds
            ):
                break

    heartbeat_thread = threading.Thread(
        target=heartbeat,
        name=f"pipeline-heartbeat-{pipeline_run_id}",
        daemon=True,
    )
    heartbeat_thread.start()
    try:
        _run_pipeline_safely(run)
        return {"pipeline_run_id": pipeline_run_id, "status": run.get("status")}
    finally:
        stop_heartbeat.set()
        heartbeat_thread.join(timeout=heartbeat_interval + 1)


@worker_ready.connect
def _on_worker_ready(sender, **kwargs) -> None:
    """Recover only tasks whose owner stopped renewing an expired lease."""
    try:
        from app.delivery_store import (
            migrate_default_json_state,
            recover_expired_pipeline_runs,
        )

        migrate_default_json_state()
        recovered = recover_expired_pipeline_runs()
        if recovered:
            sender.app.log.info(
                "Marked %d run(s) Interrupted after an earlier worker stopped.",
                recovered,
            )
    except Exception as exc:  # pragma: no cover - startup is best-effort
        sender.app.log.warning("Worker startup recovery failed: %s", exc)


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint only
    celery_app.worker_main(["-A", "app.worker.celery_app", "worker", "--loglevel=info"])
