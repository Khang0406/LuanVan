"""Phase A acceptance tests: REST API, realtime pub/sub, Celery worker helpers."""

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import fakeredis


def _application(application_id: str = "phase5-app", user_id: int = 2) -> dict:
    return {
        "id": application_id,
        "name": "Phase 5 sample",
        "namespace": application_id,
        "source_type": "docker",
        "user_id": user_id,
        "services": [{"name": "web", "image": "nginx:stable-alpine", "container_port": 80, "replicas": 1}],
        "status": "Draft",
    }


def _run(run_id: str = "run-phase5", application_id: str = "phase5-app") -> dict:
    return {
        "id": run_id,
        "application_id": application_id,
        "application_name": "Phase 5 sample",
        "status": "Success",
        "trigger_type": "Manual",
        "created_at": "2026-01-01",
        "updated_at": "2026-01-01",
        "finished_at": "2026-01-01",
        "stages": [
            {"name": name, "status": "Done", "message": "", "started_at": "", "finished_at": ""}
            for name in ("SOURCE", "BUILD", "TEST", "PUSH", "DEPLOY", "VERIFY")
        ],
    }


def _test_config(root: Path):
    class TestConfig:
        TESTING = True
        SECRET_KEY = "phase5-test"
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{root / 'users.db'}"
        SQLALCHEMY_TRACK_MODIFICATIONS = False

    return TestConfig


def _environment(root: Path) -> dict:
    return {
        "DELIVERY_DATABASE_PATH": str(root / "app.db"),
        "PLATFORM_DATA_DIR": str(root),
    }


def _login(client, user_id: int) -> None:
    with client.session_transaction() as session:
        session["_user_id"] = str(user_id)
        session["_fresh"] = True


class ApiAuthTests(unittest.TestCase):
    def test_health_endpoint_needs_no_auth(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            from app import create_app

            with patch.dict(os.environ, _environment(root)):
                client = create_app(_test_config(root)).test_client()
                response = client.get("/api/v1/health")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.get_json()["data"]["status"], "ok")

    def test_servers_requires_admin(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            from app import create_app
            from app.models import User

            with patch.dict(os.environ, _environment(root)):
                application = create_app(_test_config(root))
                with application.app_context():
                    admin_id = User.query.filter_by(username="admin").one().id
                    dev_id = User.query.filter_by(username="dev").one().id
                client = application.test_client()

                self.assertEqual(client.get("/api/v1/servers").status_code, 401)
                _login(client, dev_id)
                self.assertEqual(client.get("/api/v1/servers").status_code, 403)
                _login(client, admin_id)
                self.assertEqual(client.get("/api/v1/servers").status_code, 200)

    def test_developer_sees_only_owned_applications(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            from app import create_app
            from app.delivery_store import replace_applications
            from app.models import User

            with patch.dict(os.environ, _environment(root)):
                application = create_app(_test_config(root))
                with application.app_context():
                    dev_id = User.query.filter_by(username="dev").one().id
                replace_applications([
                    _application("mine", user_id=dev_id),
                    _application("other", user_id=999),
                ], root / "app.db")

                client = application.test_client()
                _login(client, dev_id)
                response = client.get("/api/v1/applications")
                ids = {item["id"] for item in response.get_json()["data"]}
            self.assertIn("mine", ids)
            self.assertNotIn("other", ids)

    def test_bearer_token_authenticates_as_admin(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            from app import create_app

            env = {**_environment(root), "PLATFORM_API_TOKEN": "phase5-secret-token"}
            with patch.dict(os.environ, env):
                client = create_app(_test_config(root)).test_client()
                response = client.get(
                    "/api/v1/servers",
                    headers={"Authorization": "Bearer phase5-secret-token"},
                )
            self.assertEqual(response.status_code, 200)

    def test_bearer_token_rejected_when_mismatched(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            from app import create_app

            env = {**_environment(root), "PLATFORM_API_TOKEN": "phase5-secret-token"}
            with patch.dict(os.environ, env):
                client = create_app(_test_config(root)).test_client()
                response = client.get(
                    "/api/v1/servers",
                    headers={"Authorization": "Bearer wrong-token"},
                )
            self.assertEqual(response.status_code, 401)


class PipelineApiTests(unittest.TestCase):
    def test_trigger_and_read_pipeline_run(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            from app import create_app
            from app.delivery_store import replace_applications
            from app.models import User

            with patch.dict(os.environ, _environment(root)):
                application = create_app(_test_config(root))
                replace_applications([_application()], root / "app.db")
                with application.app_context():
                    dev_id = User.query.filter_by(username="dev").one().id

                client = application.test_client()
                _login(client, dev_id)

                response = client.post("/api/v1/applications/phase5-app/pipeline")
                self.assertEqual(response.status_code, 202)
                run_id = response.get_json()["data"]["id"]

                detail = client.get(f"/api/v1/pipeline/{run_id}")
                self.assertEqual(detail.status_code, 200)
                self.assertEqual(detail.get_json()["data"]["status"], "Queued")

    def test_viewer_cannot_trigger_pipeline(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            from app import create_app
            from app.delivery_store import replace_applications
            from app.models import User

            with patch.dict(os.environ, _environment(root)):
                application = create_app(_test_config(root))
                replace_applications([_application()], root / "app.db")
                with application.app_context():
                    viewer_id = User.query.filter_by(username="viewer").one().id

                client = application.test_client()
                _login(client, viewer_id)

                response = client.post("/api/v1/applications/phase5-app/pipeline")
            self.assertEqual(response.status_code, 403)

    def test_pipeline_events_sse_streams_terminal_status(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            from app import create_app
            from app.delivery_store import replace_applications, upsert_pipeline_run
            from app.models import User

            with patch.dict(os.environ, _environment(root)):
                application = create_app(_test_config(root))
                replace_applications([_application()], root / "app.db")
                upsert_pipeline_run(_run(), root / "app.db")
                with application.app_context():
                    admin_id = User.query.filter_by(username="admin").one().id

                client = application.test_client()
                _login(client, admin_id)

                response = client.get("/api/v1/pipeline/run-phase5/events")
                self.assertEqual(response.status_code, 200)
                self.assertIn("text/event-stream", response.content_type)
                body = response.get_data(as_text=True)
            self.assertIn("data: ", body)
            self.assertIn("Success", body)


class WorkerAndRealtimeTests(unittest.TestCase):
    def test_get_and_mark_pipeline_running(self):
        from app.delivery_store import (
            get_pipeline_run,
            mark_pipeline_running,
            reserve_pipeline_run,
            replace_applications,
        )

        with TemporaryDirectory() as directory:
            database = Path(directory) / "app.db"
            replace_applications([_application()], database)
            queued = _run(run_id="run-queued")
            queued["status"] = "Queued"
            reserve_pipeline_run(queued, database)

            claimed = mark_pipeline_running("run-queued", "worker-1", database)
            self.assertEqual(claimed["status"], "Running")
            self.assertEqual(claimed["worker_id"], "worker-1")
            self.assertEqual(claimed["attempt"], 1)

            stored = get_pipeline_run("run-queued", database)
            self.assertEqual(stored["status"], "Running")
            self.assertIsNone(get_pipeline_run("missing", database))

    def test_realtime_helpers_are_safe_without_redis(self):
        from app.realtime import pipeline_log, pipeline_stage, pipeline_status

        # Must not raise when Redis is unavailable.
        pipeline_stage("run-1", "app-1", "DEPLOY", "Running", "deploying...")
        pipeline_status("run-1", "app-1", "Success")
        pipeline_log("run-1", "app-1", "line of log")

    def test_trigger_pipeline_enqueues_to_celery_when_redis_available(self):
        from app.delivery_store import replace_applications
        from app.modules.pipeline.engine import trigger_pipeline

        with TemporaryDirectory() as directory:
            database = Path(directory) / "app.db"
            with patch.dict(os.environ, {"DELIVERY_DATABASE_PATH": str(database)}):
                replace_applications([_application()], database)

                fake = fakeredis.FakeRedis(decode_responses=True)
                with patch("app.redis_client.get_redis", return_value=fake), \
                     patch("app.worker.run_pipeline.delay") as delay:
                    run = trigger_pipeline("phase5-app", actor=None)
                delay.assert_called_once_with(run["id"])


if __name__ == "__main__":
    unittest.main()
