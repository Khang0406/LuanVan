import hashlib
import hmac
import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from flask import Flask

from app.modules.applications.service import add_activity
from app.modules.pipeline import engine
from app.modules.pipeline.webhook import github_webhook_bp, verify_github_signature


class WebhookTests(unittest.TestCase):
    def setUp(self):
        self.secret = "webhook-test-secret"
        self.payload = {
            "ref": "refs/heads/main",
            "after": "a" * 40,
            "head_commit": {
                "message": "delivery update",
                "author": {"name": "Khang"},
            },
        }
        self.body = json.dumps(self.payload).encode()
        self.signature = "sha256=" + hmac.new(
            self.secret.encode(), self.body, hashlib.sha256
        ).hexdigest()
        app = Flask(__name__)
        app.register_blueprint(github_webhook_bp)
        self.client = app.test_client()
        self.application = {
            "id": "map", "name": "map", "source_type": "github",
            "default_branch": "main", "webhook_secret_ref": "application:map",
            "activity_logs": [],
        }

    def _headers(self, signature=None):
        return {
            "Content-Type": "application/json",
            "X-GitHub-Event": "push",
            "X-GitHub-Delivery": "delivery-1",
            "X-Hub-Signature-256": signature or self.signature,
        }

    def test_signature_verification(self):
        self.assertTrue(verify_github_signature(self.body, self.signature, self.secret))
        self.assertFalse(verify_github_signature(self.body, "sha256=bad", self.secret))

    def test_valid_push_triggers_gitpush_pipeline_with_commit(self):
        with patch("app.modules.pipeline.webhook.find_application", return_value=self.application), \
                patch("app.modules.pipeline.webhook.get_webhook_secret", return_value=self.secret), \
                patch("app.modules.pipeline.webhook.claim_webhook_delivery", return_value=True), \
                patch("app.modules.pipeline.webhook.trigger_pipeline", return_value={"id": "run-1"}) as trigger, \
                patch("app.modules.pipeline.webhook.save_application"), \
                patch("app.modules.pipeline.webhook.record_audit"):
            response = self.client.post(
                "/webhooks/github/map", data=self.body, headers=self._headers()
            )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(trigger.call_args.kwargs["trigger_type"], "GitPush")
        self.assertEqual(trigger.call_args.kwargs["requested_ref"], "a" * 40)

    def test_duplicate_delivery_does_not_trigger_again(self):
        with patch("app.modules.pipeline.webhook.find_application", return_value=self.application), \
                patch("app.modules.pipeline.webhook.get_webhook_secret", return_value=self.secret), \
                patch("app.modules.pipeline.webhook.claim_webhook_delivery", return_value=False), \
                patch("app.modules.pipeline.webhook.trigger_pipeline") as trigger:
            response = self.client.post(
                "/webhooks/github/map", data=self.body, headers=self._headers()
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["status"], "duplicate")
        trigger.assert_not_called()

    def test_invalid_signature_is_rejected_before_trigger(self):
        with patch("app.modules.pipeline.webhook.find_application", return_value=self.application), \
                patch("app.modules.pipeline.webhook.get_webhook_secret", return_value=self.secret), \
                patch("app.modules.pipeline.webhook.record_audit"), \
                patch("app.modules.pipeline.webhook.trigger_pipeline") as trigger:
            response = self.client.post(
                "/webhooks/github/map", data=self.body,
                headers=self._headers("sha256=" + "0" * 64),
            )
        self.assertEqual(response.status_code, 401)
        trigger.assert_not_called()


class AuthorizationAndSecretRegressionTests(unittest.TestCase):
    def test_activity_and_pipeline_stage_mask_secrets(self):
        secret = "token-value-that-must-not-leak"
        application = {"activity_logs": []}
        add_activity(application, "TEST", f"Authorization: Bearer {secret}")
        self.assertNotIn(secret, application["activity_logs"][0]["message"])

        run = {
            "id": "run-1", "application_id": "map", "updated_at": "",
            "stages": [{
                "name": "PUSH", "status": "Waiting", "message": "",
                "started_at": "", "finished_at": "",
            }],
        }
        with patch.object(engine, "_save_pipeline_run"), \
                patch("app.modules.audit.service.record_audit"):
            engine._update_stage(run, "PUSH", "Failed", f"password={secret}")
        self.assertNotIn(secret, run["stages"][0]["message"])

    def test_developer_cannot_view_another_users_application(self):
        from app import create_app
        from app.delivery_store import replace_applications

        with TemporaryDirectory() as directory:
            root = Path(directory)
            delivery_db = root / "delivery.db"
            sqlalchemy_db = root / "users.db"

            class TestConfig:
                TESTING = True
                SECRET_KEY = "test"
                SQLALCHEMY_DATABASE_URI = f"sqlite:///{sqlalchemy_db}"
                SQLALCHEMY_TRACK_MODIFICATIONS = False

            with patch.dict(os.environ, {"DELIVERY_DATABASE_PATH": str(delivery_db)}):
                app = create_app(TestConfig)
                replace_applications([{
                    "id": "private-app", "name": "private-app", "owner": "other",
                    "user_id": 999, "namespace": "private-app", "source_type": "docker",
                    "services": [{"name": "web", "image": "nginx:stable-alpine"}],
                    "status": "Draft",
                }])
                client = app.test_client()
                with client.session_transaction() as session:
                    session["_user_id"] = "2"  # seeded Developer
                    session["_fresh"] = True
                response = client.get("/applications/private-app")
                self.assertEqual(response.status_code, 404)

    def test_only_admin_can_update_platform_registry_credential(self):
        from app import create_app
        from app import registry_credentials

        with TemporaryDirectory() as directory:
            root = Path(directory)

            class TestConfig:
                TESTING = True
                SECRET_KEY = "test"
                SQLALCHEMY_DATABASE_URI = f"sqlite:///{root / 'users.db'}"
                SQLALCHEMY_TRACK_MODIFICATIONS = False

            environment = {
                "DELIVERY_DATABASE_PATH": str(root / "delivery.db"),
                "PLATFORM_DATA_DIR": str(root),
            }
            credential_file = root / "registry_credentials.json"
            with patch.dict(os.environ, environment), patch.object(
                registry_credentials, "CREDENTIALS_FILE", credential_file
            ):
                app = create_app(TestConfig)
                client = app.test_client()
                with client.session_transaction() as session:
                    session["_user_id"] = "2"
                    session["_fresh"] = True
                    session["_csrf_token"] = "csrf"
                response = client.post(
                    "/cicd/registry-credential",
                    data={
                        "csrf_token": "csrf",
                        "registry_url": "docker.io",
                        "registry_username": "platform-org",
                        "registry_credential": "must-not-be-stored",
                    },
                )
                self.assertEqual(response.status_code, 302)
                self.assertFalse(credential_file.exists())

    def test_admin_saves_global_token_without_rendering_it(self):
        from app import create_app
        from app import registry_credentials
        from app.modules.audit.service import load_audit_logs

        secret = "global-registry-token-must-stay-secret"
        with TemporaryDirectory() as directory:
            root = Path(directory)

            class TestConfig:
                TESTING = True
                SECRET_KEY = "test"
                SQLALCHEMY_DATABASE_URI = f"sqlite:///{root / 'users.db'}"
                SQLALCHEMY_TRACK_MODIFICATIONS = False

            environment = {
                "DELIVERY_DATABASE_PATH": str(root / "delivery.db"),
                "PLATFORM_DATA_DIR": str(root),
            }
            credential_file = root / "registry_credentials.json"
            with patch.dict(os.environ, environment), patch.object(
                registry_credentials, "CREDENTIALS_FILE", credential_file
            ):
                app = create_app(TestConfig)
                client = app.test_client()
                with client.session_transaction() as session:
                    session["_user_id"] = "1"
                    session["_fresh"] = True
                    session["_csrf_token"] = "csrf"
                response = client.post(
                    "/cicd/registry-credential",
                    data={
                        "csrf_token": "csrf",
                        "registry_url": "docker.io",
                        "registry_username": "platform-org",
                        "registry_credential": secret,
                    },
                )
                self.assertEqual(response.status_code, 302)
                stored = registry_credentials.get_platform_registry_credential()
                self.assertEqual(stored["credential"], secret)
                page = client.get("/cicd")
                self.assertEqual(page.status_code, 200)
                self.assertNotIn(secret.encode(), page.data)
                self.assertNotIn(
                    secret,
                    json.dumps(load_audit_logs(), ensure_ascii=False),
                )


if __name__ == "__main__":
    unittest.main()
