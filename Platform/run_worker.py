"""Celery pipeline worker entrypoint.

Loads ``.env`` before starting the worker so ``DATABASE_URL``/``REDIS_URL`` are
available to tasks. Run with::

    python run_worker.py
"""

from __future__ import annotations

import os

from dotenv import load_dotenv

load_dotenv()

from app.worker import celery_app


if __name__ == "__main__":
    concurrency = os.getenv("PIPELINE_MAX_CONCURRENT", "4")
    celery_app.worker_main(
        [
            "-A",
            "app.worker.celery_app",
            "worker",
            "--concurrency",
            concurrency,
            "--loglevel=info",
        ]
    )
