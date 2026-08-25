"""PostgreSQL backend integration tests.

These tests run only when ``DATABASE_URL`` points at a live PostgreSQL server
(e.g. the ``docker-compose.yml`` service). They are skipped by default so the
normal SQLite test run stays green without external dependencies.

Run::

    DATABASE_URL=postgresql+psycopg2://platform:platform@localhost:5432/platform \
        python -m unittest tests.test_postgres_backend -v
"""

import os
import threading
import unittest
import uuid

from app.delivery_store import _use_postgres


def _require_postgres() -> bool:
    return _use_postgres(None)


@unittest.skipUnless(_require_postgres(), "PostgreSQL not configured; skipping")
class PostgresBackendTests(unittest.TestCase):
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
        self.assertIsNone(claim_next_pipeline_run("worker-y"))

        # Per-application limit: a second queued run is rejected.
        duplicate = dict(run, id=f"run-{self.suffix}-2")
        with self.assertRaises(ValueError):
            reserve_pipeline_run(duplicate)

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
