"""Celery pipeline worker — Redis-backed replacement for the SQLite claim loop.

Run with::

    celery -A app.worker.celery_app worker --concurrency=4 --loglevel=info

The old ``python -m app.pipeline_worker`` polling loop remains available as a
fallback when ``REDIS_URL`` is unset. This module only needs to be launched when
Redis is configured (``REDIS_URL`` or ``CELERY_BROKER_URL``).
"""

from __future__ import annotations

import os

from celery import Celery
from celery.signals import worker_ready
from dotenv import load_dotenv

load_dotenv()

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
)


@celery_app.task(name="platform.run_pipeline", bind=True, acks_late=True)
def run_pipeline(self, pipeline_run_id: str) -> dict:
    """Execute one queued pipeline run (SOURCE → BUILD → TEST → PUSH → DEPLOY → VERIFY)."""
    from app.delivery_store import get_pipeline_run, mark_pipeline_running
    from app.modules.pipeline.engine import _run_pipeline_safely

    run = get_pipeline_run(pipeline_run_id)
    if not run:
        return {"pipeline_run_id": pipeline_run_id, "status": "not_found"}

    mark_pipeline_running(pipeline_run_id, worker_id=self.request.id)
    run = get_pipeline_run(pipeline_run_id) or run
    _run_pipeline_safely(run)
    return {"pipeline_run_id": pipeline_run_id, "status": run.get("status")}


@worker_ready.connect
def _on_worker_ready(sender, **kwargs) -> None:
    """Close any run left Running by a previous worker before accepting tasks.

    Mirrors ``PipelineWorker.prepare()`` from the SQLite fallback path.
    """
    try:
        from app.delivery_store import migrate_default_json_state
        from app.modules.pipeline.engine import recover_interrupted_pipeline_runs

        migrate_default_json_state()
        recovered = recover_interrupted_pipeline_runs()
        if recovered:
            sender.app.log.info(
                "Marked %d run(s) Interrupted after an earlier worker stopped.",
                recovered,
            )
    except Exception as exc:  # pragma: no cover - startup is best-effort
        sender.app.log.warning("Worker startup recovery failed: %s", exc)


if __name__ == "__main__":  # pragma: no cover - CLI entrypoint only
    celery_app.worker_main(["-A", "app.worker.celery_app", "worker", "--loglevel=info"])
