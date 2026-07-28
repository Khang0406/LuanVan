import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


class CloudRuntimeTests(unittest.TestCase):
    def test_global_pipeline_limit_is_atomic(self):
        from app.delivery_store import (
            initialize_schema,
            replace_applications,
            reserve_pipeline_run,
        )

        with TemporaryDirectory() as directory:
            database = Path(directory) / "delivery.db"
            initialize_schema(database)
            replace_applications(
                [
                    {
                        "id": application_id,
                        "name": application_id,
                        "namespace": application_id,
                        "source_type": "docker",
                    }
                    for application_id in ("app-a", "app-b")
                ],
                database,
            )
            first = {
                "id": "run-a",
                "application_id": "app-a",
                "status": "Running",
                "stages": [],
            }
            second = {
                "id": "run-b",
                "application_id": "app-b",
                "status": "Queued",
                "stages": [],
            }
            with patch.dict(os.environ, {"PIPELINE_MAX_CONCURRENT": "1"}):
                reserve_pipeline_run(first, database)
                with self.assertRaisesRegex(
                    ValueError, "Build Worker đang đạt giới hạn"
                ):
                    reserve_pipeline_run(second, database)

    def test_production_rejects_default_secret(self):
        from app.config import Config

        original_environment = Config.PLATFORM_ENV
        original_secret = Config.SECRET_KEY
        try:
            Config.PLATFORM_ENV = "production"
            Config.SECRET_KEY = "change-me"
            with self.assertRaisesRegex(RuntimeError, "FLASK_SECRET_KEY"):
                Config.validate_production()
        finally:
            Config.PLATFORM_ENV = original_environment
            Config.SECRET_KEY = original_secret

    def test_health_endpoints_do_not_require_login(self):
        from app import create_app

        with TemporaryDirectory() as directory:
            root = Path(directory)
            delivery_database = root / "delivery.db"
            users_database = root / "users.db"

            class TestConfig:
                TESTING = True
                SECRET_KEY = "test"
                SQLALCHEMY_DATABASE_URI = f"sqlite:///{users_database}"
                SQLALCHEMY_TRACK_MODIFICATIONS = False

            with patch.dict(
                os.environ,
                {"DELIVERY_DATABASE_PATH": str(delivery_database)},
            ):
                client = create_app(TestConfig).test_client()
                self.assertEqual(client.get("/healthz").status_code, 200)
                self.assertEqual(client.get("/readyz").status_code, 200)

    def test_production_bootstrap_uses_secret_password(self):
        from app import create_app
        from app.models import User

        with TemporaryDirectory() as directory:
            root = Path(directory)

            class ProductionConfig:
                TESTING = True
                PLATFORM_ENV = "production"
                SECRET_KEY = "test-secret"
                SQLALCHEMY_DATABASE_URI = f"sqlite:///{root / 'users.db'}"
                SQLALCHEMY_TRACK_MODIFICATIONS = False

            environment = {
                "DELIVERY_DATABASE_PATH": str(root / "delivery.db"),
                "PLATFORM_DATA_DIR": str(root),
                "PLATFORM_ADMIN_PASSWORD": "production-test-password",
            }
            with patch.dict(os.environ, environment):
                application = create_app(ProductionConfig)
                with application.app_context():
                    admin = User.query.filter_by(username="admin").one()
                    self.assertTrue(
                        admin.check_password("production-test-password")
                    )
                    self.assertFalse(admin.check_password("admin123"))
                    self.assertIsNone(User.query.filter_by(username="dev").first())

    def test_sensitive_ansible_vars_never_appear_in_argv(self):
        from app.security import temporary_ansible_extra_vars

        secret = "sudo-password-that-must-not-leak"
        with temporary_ansible_extra_vars(
            {"ansible_become_password": secret}
        ) as path:
            argv = ["ansible-playbook", "--extra-vars", f"@{path}"]
            self.assertNotIn(secret, " ".join(argv))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertIn(secret, path.read_text(encoding="utf-8"))
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
