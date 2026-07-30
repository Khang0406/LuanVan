"""Durable SQLite-backed pipeline worker process."""

from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import threading
import uuid

from app.delivery_store import claim_next_pipeline_run, migrate_default_json_state
from app.modules.pipeline.engine import (
    _run_pipeline_safely,
    recover_interrupted_pipeline_runs,
)


LOGGER = logging.getLogger("platform.pipeline-worker")


class PipelineWorker:
    def __init__(self, *, poll_interval: float = 2.0, worker_id: str = "") -> None:
        self.poll_interval = max(0.1, poll_interval)
        self.worker_id = worker_id or (
            f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
        )
        self.stop_event = threading.Event()

    def stop(self, _signum: int | None = None, _frame: object | None = None) -> None:
        LOGGER.info("Shutdown requested; worker will stop after the current task.")
        self.stop_event.set()

    def prepare(self) -> int:
        migrate_default_json_state()
        interrupted = recover_interrupted_pipeline_runs()
        if interrupted:
            LOGGER.warning(
                "Marked %d run(s) Interrupted after an earlier worker stopped.",
                interrupted,
            )
        return interrupted

    def run_once(self) -> bool:
        run = claim_next_pipeline_run(self.worker_id)
        if not run:
            return False
        LOGGER.info("Claimed pipeline %s.", run["id"])
        _run_pipeline_safely(run)
        LOGGER.info("Finished pipeline %s.", run["id"])
        return True

    def run_forever(self) -> None:
        self.prepare()
        while not self.stop_event.is_set():
            if not self.run_once():
                self.stop_event.wait(self.poll_interval)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Platform pipeline worker")
    parser.add_argument("--once", action="store_true", help="Process at most one task")
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=float(os.getenv("PIPELINE_POLL_INTERVAL", "2")),
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    worker = PipelineWorker(poll_interval=args.poll_interval)
    signal.signal(signal.SIGTERM, worker.stop)
    signal.signal(signal.SIGINT, worker.stop)
    if args.once:
        worker.prepare()
        worker.run_once()
    else:
        worker.run_forever()


if __name__ == "__main__":
    main()
