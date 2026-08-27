"""PostgreSQL backend integration tests.

These tests run only when ``POSTGRES_TEST_DATABASE_URL`` points at a disposable
PostgreSQL database whose name ends in ``_test``. They are skipped by default
so the normal SQLite test run stays green without external dependencies.

Run::

    POSTGRES_TEST_DATABASE_URL=postgresql+psycopg2://platform:platform@localhost:5432/platform_test \
        python -m unittest tests.test_postgres_backend -v
"""

import os
import threading
import unittest
import uuid
from urllib.parse import urlparse

# Integration tests must never reuse DATABASE_URL from the web/runtime process.
# They run only when an explicit, clearly named disposable database is supplied.
TEST_DATABASE_URL = os.getenv("POSTGRES_TEST_DATABASE_URL", "").strip()
if TEST_DATABASE_URL:
    parsed_test_url = TEST_DATABASE_URL.replace(
        "postgresql+psycopg2", "postgresql"
    )
    database_name = urlparse(parsed_test_url).path.lstrip("/")
    if not database_name.endswith("_test"):
        raise RuntimeError(
            "POSTGRES_TEST_DATABASE_URL must target a database ending in '_test'"
        )
    os.environ["DATABASE_URL"] = TEST_DATABASE_URL

from app.delivery_store import _use_postgres


def _require_postgres() -> bool:
    return bool(TEST_DATABASE_URL) and _use_postgres(None)


@unittest.skipUnless(_require_postgres(), "PostgreSQL not configured; skipping")
class PostgresBackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # The disposable PostgreSQL test database follows the same migration
        # path as production; store operations no longer create tables.
        from alembic import command
        from alembic.config import Config

        config = Config("alembic.ini")
        command.upgrade(config, "head")

    def setUp(self):
        from app.delivery_store import initialize_schema

        initialize_schema()
        self.suffix = uuid.uuid4().hex[:8]
        self.application_id = f"pg-{self.suffix}"

    def _application(self, application_id: str | None = None) -> dict:
        application_id = application_id or self.application_id
        return {
            "id": application_id,
            "name": application_id,
            "namespace": application_id,
            "source_type": "docker",
            "user_id": 2,
            "services": [{"name": "web", "image": "nginx:stable-alpine", "container_port": 80, "replicas": 1}],
            "status": "Draft",
        }

    def test_application_crud_round_trip(self):
        from app.delivery_store import list_applications, replace_applications

        replace_applications([self._application()])
        apps = list_applications()
        self.assertEqual([a["id"] for a in apps if a["id"] == self.application_id], [self.application_id])

    def test_pipeline_claim_and_reserve_limits(self):
        from app.delivery_store import (
            claim_next_pipeline_run,
            get_pipeline_run,
            list_pipeline_runs,
            mark_pipeline_running,
            replace_applications,
            reserve_pipeline_run,
        )

        replace_applications([self._application()])
        run = {
            "id": f"run-{self.suffix}",
            "application_id": self.application_id,
            "application_name": self.application_id,
            "status": "Queued",
            "trigger_type": "Manual",
            "created_at": "2026-01-01",
            "updated_at": "2026-01-01",
            "stages": [
                {"name": name, "status": "Waiting", "message": "", "started_at": "", "finished_at": ""}
                for name in ("SOURCE", "BUILD", "TEST", "PUSH", "DEPLOY", "VERIFY")
            ],
        }
        reserve_pipeline_run(run)
        self.assertEqual(get_pipeline_run(run["id"])["status"], "Queued")
        self.assertEqual(len(list_pipeline_runs(self.application_id)), 1)

        claimed = mark_pipeline_running(run["id"], "worker-x")
        self.assertEqual(claimed["status"], "Running")
        self.assertIsNone(mark_pipeline_running(run["id"], "worker-y"))
        from app.delivery_store import renew_pipeline_lease
        self.assertFalse(renew_pipeline_lease(run["id"], "worker-y"))
        self.assertTrue(renew_pipeline_lease(run["id"], "worker-x"))
        self.assertIsNone(claim_next_pipeline_run("worker-y"))

        # Per-application limit: a second queued run is rejected.
        duplicate = dict(run, id=f"run-{self.suffix}-2")
        with self.assertRaises(ValueError):
            reserve_pipeline_run(duplicate)

    def test_duplicate_delivery_claims_exactly_once_under_concurrency(self):
        from app.delivery_store import (
            mark_pipeline_running,
            replace_applications,
            reserve_pipeline_run,
        )

        replace_applications([self._application()])
        run = {
            "id": f"redelivery-{self.suffix}",
            "application_id": self.application_id,
            "status": "Queued",
            "trigger_type": "Manual",
            "created_at": "2026-01-01",
            "updated_at": "2026-01-01",
            "stages": [],
        }
        reserve_pipeline_run(run)
        barrier = threading.Barrier(2)
        claims = []
        lock = threading.Lock()

        def claim(worker_id: str) -> None:
            barrier.wait()
            claimed = mark_pipeline_running(run["id"], worker_id, lease_seconds=60)
            with lock:
                claims.append(claimed)

        threads = [threading.Thread(target=claim, args=(f"worker-{index}",)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(sum(claimed is not None for claimed in claims), 1)

    def test_expired_lease_is_recovered(self):
        from app.delivery_store import (
            get_pipeline_run,
            mark_pipeline_running,
            recover_expired_pipeline_runs,
            replace_applications,
            reserve_pipeline_run,
            upsert_pipeline_run,
        )

        replace_applications([self._application()])
        run = {
            "id": f"expired-{self.suffix}",
            "application_id": self.application_id,
            "status": "Queued",
            "trigger_type": "Manual",
            "created_at": "2026-01-01",
            "updated_at": "2026-01-01",
            "stages": [{"name": "BUILD", "status": "Running", "message": ""}],
        }
        reserve_pipeline_run(run)
        mark_pipeline_running(run["id"], "dead-worker", lease_seconds=60)
        expired = get_pipeline_run(run["id"])
        expired["lease_expires_at"] = "2000-01-01T00:00:00Z"
        upsert_pipeline_run(expired)

        self.assertEqual(recover_expired_pipeline_runs(), 1)
        self.assertEqual(get_pipeline_run(run["id"])["status"], "Interrupted")

    def test_deployment_versions_are_unique_under_concurrency(self):
        from app.delivery_store import create_deployment_record, list_deployments, replace_applications

        replace_applications([self._application()])
        versions: list[int] = []
        lock = threading.Lock()

        def make_deployment(index: int) -> None:
            record = create_deployment_record({
                "application_id": self.application_id,
                "pipeline_run_id": None,
                "previous_deployment_id": None,
                "repository": "https://example.com/repo.git",
                "branch": "main",
                "commit_sha": "a" * 40,
                "services": [{"service": "web", "image": f"img:{index}", "digest": "sha256:" + "1" * 64}],
                "manifest": "apiVersion: v1\nkind: Service\nmetadata:\n  name: x\n",
                "deployment_mode": "Production",
                "namespace": self.application_id,
                "url": "http://node:30082",
                "verify_status": "Ready",
                "status": "Ready",
                "started_at": "2026-01-01",
                "finished_at": "2026-01-01",
                "actor": "khang",
            })
            with lock:
                versions.append(record["version"])

        threads = [threading.Thread(target=make_deployment, args=(i,)) for i in range(5)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(sorted(versions), [1, 2, 3, 4, 5])
        self.assertEqual(len(list_deployments(self.application_id)), 5)

    def test_webhook_delivery_is_idempotent(self):
        from app.delivery_store import claim_webhook_delivery, replace_applications

        replace_applications([self._application()])
        delivery_id = f"delivery-{self.suffix}"
        self.assertTrue(claim_webhook_delivery(delivery_id, self.application_id, "push", "a" * 40, {"x": 1}))
        self.assertFalse(claim_webhook_delivery(delivery_id, self.application_id, "push", "a" * 40, {"x": 1}))


if __name__ == "__main__":
    unittest.main()
