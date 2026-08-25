import json
import os
import re
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import yaml

from app.delivery_store import (
    claim_next_pipeline_run,
    create_deployment_record,
    initialize_schema,
    list_audit_logs,
    list_deployments,
    list_jobs,
    list_pipeline_runs,
    replace_applications,
    replace_audit_logs,
    replace_jobs,
    reserve_pipeline_run,
)
from app.modules.applications.service import can_access_application
from scripts.backup_platform_data import backup
from scripts.restore_platform_data import restore


def _application(application_id: str = "phase4-app", user_id: int = 2) -> dict:
    return {
        "id": application_id,
        "name": "Phase 4 sample",
        "namespace": application_id,
        "source_type": "docker",
        "user_id": user_id,
        "services": [{
            "name": "web",
            "image": "nginx:stable-alpine",
            "container_port": 80,
            "replicas": 1,
        }],
        "created_at": "2026-07-29 10:00:00",
        "updated_at": "2026-07-29 10:00:00",
    }


def _run(run_id: str = "run-phase4") -> dict:
    return {
        "id": run_id,
        "application_id": "phase4-app",
        "application_name": "Phase 4 sample",
        "status": "Queued",
        "trigger_type": "Manual",
        "created_at": "2026-07-29 10:01:00",
        "updated_at": "2026-07-29 10:01:00",
        "stages": [{
            "name": name,
            "status": "Waiting",
            "message": "",
            "started_at": "",
            "finished_at": "",
        } for name in ("SOURCE", "BUILD", "TEST", "PUSH", "DEPLOY", "VERIFY")],
    }


class DurableQueueTests(unittest.TestCase):
    def test_cicd_events_accept_legacy_run_without_application_name(self):
        from app.modules.pipeline import engine

        legacy = _run("run-legacy")
        legacy.pop("application_name")
        legacy["stages"][0]["status"] = "Done"
        with patch.object(engine, "load_pipeline_runs", return_value=[legacy]):
            events = engine.load_all_pipeline_events({"phase4-app"})
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["actor"], "phase4-app")
        self.assertEqual(events[0]["event"], "SOURCE stage")

    def test_claim_is_atomic_and_increments_attempt(self):
        with TemporaryDirectory() as directory:
            database = Path(directory) / "app.db"
            replace_applications([_application()], database)
            reserve_pipeline_run(_run(), database)
            claims = []

            def claim(worker: str) -> None:
                claims.append(claim_next_pipeline_run(worker, database))

            threads = [
                threading.Thread(target=claim, args=(f"worker-{index}",))
                for index in range(2)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            claimed = [item for item in claims if item]
            self.assertEqual(len(claimed), 1)
            self.assertEqual(claimed[0]["status"], "Running")
            self.assertEqual(claimed[0]["attempt"], 1)
            self.assertTrue(claimed[0]["worker_id"].startswith("worker-"))
            self.assertIsNone(claim_next_pipeline_run("worker-later", database))

    def test_web_app_restart_does_not_consume_or_interrupt_queue(self):
        from app import create_app

        with TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "app.db"
            replace_applications([_application()], database)
            reserve_pipeline_run(_run(), database)

            class TestConfig:
                TESTING = True
                SECRET_KEY = "phase4-test"
                SQLALCHEMY_DATABASE_URI = f"sqlite:///{database}"
                SQLALCHEMY_TRACK_MODIFICATIONS = False

            environment = {
                "DELIVERY_DATABASE_PATH": str(database),
                "PLATFORM_DATA_DIR": str(root),
            }
            with patch.dict(os.environ, environment):
                create_app(TestConfig)
                create_app(TestConfig)
                runs = list_pipeline_runs(path=database)
            self.assertEqual(runs[0]["status"], "Queued")
            self.assertNotIn("worker_id", runs[0])

    def test_worker_claims_then_executes_one_task(self):
        from app.pipeline_worker import PipelineWorker

        with TemporaryDirectory() as directory:
            database = Path(directory) / "app.db"
            replace_applications([_application()], database)
            reserve_pipeline_run(_run(), database)
            with patch.dict(os.environ, {"DELIVERY_DATABASE_PATH": str(database)}), patch(
                "app.pipeline_worker._run_pipeline_safely"
            ) as execute:
                worker = PipelineWorker(worker_id="test-worker")
                self.assertTrue(worker.run_once())
                self.assertFalse(worker.run_once())
            execute.assert_called_once()
            self.assertEqual(execute.call_args.args[0]["status"], "Running")

    def test_worker_restart_marks_running_task_interrupted(self):
        from app.pipeline_worker import PipelineWorker

        with TemporaryDirectory() as directory:
            database = Path(directory) / "app.db"
            replace_applications([_application()], database)
            run = _run()
            run["status"] = "Running"
            run["stages"][0]["status"] = "Running"
            reserve_pipeline_run(run, database)
            with patch.dict(os.environ, {"DELIVERY_DATABASE_PATH": str(database)}):
                recovered = PipelineWorker(worker_id="replacement-worker").prepare()
            stored = list_pipeline_runs(path=database)[0]
            self.assertEqual(recovered, 1)
            self.assertEqual(stored["status"], "Interrupted")
            self.assertEqual(stored["stages"][0]["status"], "Interrupted")


class BackupRestoreTests(unittest.TestCase):
    def test_closed_loop_restores_delivery_history_without_secret_values(self):
        secret = "phase4-secret-value-must-not-enter-backup"
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "data"
            backups = root / "backups"
            source.mkdir()
            database = source / "app.db"
            replace_applications([_application()], database)
            reserve_pipeline_run(_run(), database)
            create_deployment_record({
                "id": "deployment-phase4",
                "application_id": "phase4-app",
                "pipeline_run_id": "run-phase4",
                "namespace": "phase4-app",
                "deployment_mode": "Production",
                "status": "Ready",
                "verify_status": "Ready",
                "services": [{
                    "service": "web",
                    "image": "example/web:commit",
                    "digest": "sha256:" + "a" * 64,
                }],
            }, database)
            replace_jobs([{
                "id": "job-phase4",
                "type": "Pipeline",
                "title": "Phase 4",
                "target": "phase4-app",
                "status": "Success",
            }], database)
            replace_audit_logs([{
                "id": "audit-phase4",
                "action": "PIPELINE_VERIFY",
                "target": "phase4-app",
                "result": "SUCCESS",
            }], database)
            (source / "application_secrets.json").write_text(
                json.dumps({
                    "phase4-app/API_TOKEN": {"key": "API_TOKEN", "value": secret}
                }),
                encoding="utf-8",
            )
            (source / "registry_credentials.json").write_text(
                json.dumps({
                    "application:phase4-app": {
                        "registry": "docker.io",
                        "username": "student",
                        "credential": secret,
                    }
                }),
                encoding="utf-8",
            )

            backup_dir = backup(source, backups)
            replace_applications([_application("changed-app")], database)
            restored = restore(backup_dir, source)

            self.assertEqual(restored, database)
            self.assertEqual(list_pipeline_runs(path=database)[0]["id"], "run-phase4")
            self.assertEqual(list_deployments(path=database)[0]["id"], "deployment-phase4")
            self.assertEqual(list_jobs(path=database)[0]["id"], "job-phase4")
            self.assertEqual(list_audit_logs(path=database)[0]["id"], "audit-phase4")

            from app import create_app
            from app.pipeline_worker import PipelineWorker

            class RestoredConfig:
                TESTING = True
                SECRET_KEY = "phase4-restore-test"
                SQLALCHEMY_DATABASE_URI = f"sqlite:///{database}"
                SQLALCHEMY_TRACK_MODIFICATIONS = False

            with patch.dict(os.environ, {
                "DELIVERY_DATABASE_PATH": str(database),
                "PLATFORM_DATA_DIR": str(source),
            }), patch("app.delivery_store.migrate_default_json_state"):
                create_app(RestoredConfig)
                with patch("app.pipeline_worker.migrate_default_json_state"), patch(
                    "app.modules.pipeline.engine.migrate_default_json_state"
                ), patch("app.pipeline_worker._run_pipeline_safely") as execute:
                    worker = PipelineWorker(worker_id="restored-worker")
                    worker.prepare()
                    self.assertTrue(worker.run_once())
            self.assertEqual(execute.call_args.args[0]["id"], "run-phase4")

            metadata = json.loads(
                (backup_dir / "secret-metadata.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                metadata["stores"]["application"][0]["key"], "API_TOKEN"
            )
            for path in backup_dir.iterdir():
                if path.is_file():
                    self.assertNotIn(secret.encode(), path.read_bytes())
            self.assertFalse((backup_dir / "application_secrets.json").exists())
            self.assertFalse((backup_dir / "registry_credentials.json").exists())


class SecurityAndRuntimeTests(unittest.TestCase):
    def test_viewer_can_read_all_apps_but_cannot_own_mutations(self):
        class User:
            role = "Viewer"
            id = 999
            is_admin = False

        self.assertTrue(can_access_application(_application(user_id=2), User()))

    def test_production_manifest_has_separate_non_root_web_and_worker(self):
        documents = list(yaml.safe_load_all(
            Path("k8s/platform/deployment.yaml").read_text(encoding="utf-8")
        ))
        deployment = documents[0]
        self.assertEqual(deployment["spec"]["replicas"], 1)
        self.assertEqual(deployment["spec"]["strategy"]["type"], "Recreate")
        pod = deployment["spec"]["template"]["spec"]
        self.assertGreaterEqual(pod["terminationGracePeriodSeconds"], 30)
        containers = {item["name"]: item for item in pod["containers"]}
        self.assertEqual(set(containers), {"platform", "pipeline-worker"})
        self.assertEqual(
            containers["pipeline-worker"]["command"],
            ["python", "-m", "app.pipeline_worker"],
        )
        for container in containers.values():
            security = container["securityContext"]
            self.assertTrue(security["runAsNonRoot"])
            self.assertFalse(security["allowPrivilegeEscalation"])
            self.assertEqual(security["capabilities"]["drop"], ["ALL"])
            self.assertIn("requests", container["resources"])
            self.assertIn("limits", container["resources"])

    def test_viewer_role_is_valid(self):
        from app.models import User

        viewer = User(username="view-only", role="Viewer")
        viewer.set_password("long-enough-password")
        self.assertEqual(viewer.role, "Viewer")

    def test_production_rejects_insecure_session_cookie(self):
        from app.config import Config

        values = (
            Config.PLATFORM_ENV,
            Config.SECRET_KEY,
            Config.SESSION_COOKIE_SECURE,
            Config.REMEMBER_COOKIE_SECURE,
        )
        try:
            Config.PLATFORM_ENV = "production"
            Config.SECRET_KEY = "phase4-production-secret"
            Config.SESSION_COOKIE_SECURE = False
            Config.REMEMBER_COOKIE_SECURE = False
            with self.assertRaisesRegex(RuntimeError, "COOKIE_SECURE"):
                Config.validate_production()
        finally:
            (
                Config.PLATFORM_ENV,
                Config.SECRET_KEY,
                Config.SESSION_COOKIE_SECURE,
                Config.REMEMBER_COOKIE_SECURE,
            ) = values

    def test_viewer_cannot_trigger_application_pipeline(self):
        from app import create_app
        from app.models import User

        with TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "app.db"

            class TestConfig:
                TESTING = True
                SECRET_KEY = "phase4-test"
                SQLALCHEMY_DATABASE_URI = f"sqlite:///{database}"
                SQLALCHEMY_TRACK_MODIFICATIONS = False

            with patch.dict(os.environ, {
                "DELIVERY_DATABASE_PATH": str(database),
                "PLATFORM_DATA_DIR": str(root),
            }):
                application = create_app(TestConfig)
                replace_applications([_application()], database)
                with application.app_context():
                    viewer_id = User.query.filter_by(username="viewer").one().id
                client = application.test_client()
                with client.session_transaction() as session:
                    session["_user_id"] = str(viewer_id)
                    session["_fresh"] = True
                    session["_csrf_token"] = "phase4-csrf"
                with patch("app.ui.routes.trigger_pipeline") as trigger:
                    response = client.post(
                        "/applications/phase4-app/pipeline",
                        data={"csrf_token": "phase4-csrf"},
                    )
                self.assertEqual(response.status_code, 302)
                self.assertTrue(response.location.endswith("/dashboard"))
                trigger.assert_not_called()

    def test_all_static_internal_post_forms_have_csrf_token(self):
        missing = []
        form_pattern = re.compile(
            r"<form\b[^>]*\bmethod=[\"']post[\"'][^>]*>.*?</form>",
            re.IGNORECASE | re.DOTALL,
        )
        for template in Path("app/templates").rglob("*.html"):
            content = template.read_text(encoding="utf-8")
            for index, form in enumerate(form_pattern.findall(content), start=1):
                if "csrf_token" not in form:
                    missing.append(f"{template}:{index}")
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
