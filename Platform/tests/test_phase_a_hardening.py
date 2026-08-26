import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


def _test_config(root: Path):
    class TestConfig:
        TESTING = True
        SECRET_KEY = "phase-a-hardening-test"
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{root / 'users.db'}"
        SQLALCHEMY_TRACK_MODIFICATIONS = False

    return TestConfig


class ProductionConfigurationTests(unittest.TestCase):
    def test_production_requires_postgresql(self):
        from app.config import Config

        with patch.object(Config, "PLATFORM_ENV", "production"), \
             patch.object(Config, "SECRET_KEY", "strong-secret"), \
             patch.object(Config, "SESSION_COOKIE_SECURE", True), \
             patch.object(Config, "REMEMBER_COOKIE_SECURE", True), \
             patch.object(Config, "SQLALCHEMY_DATABASE_URI", "sqlite:///unsafe.db"), \
             patch.dict(os.environ, {"REDIS_URL": "redis://localhost:6379/0"}):
            with self.assertRaisesRegex(RuntimeError, "PostgreSQL"):
                Config.validate_production()

    def test_production_requires_redis(self):
        from app.config import Config

        with patch.object(Config, "PLATFORM_ENV", "production"), \
             patch.object(Config, "SECRET_KEY", "strong-secret"), \
             patch.object(Config, "SESSION_COOKIE_SECURE", True), \
             patch.object(Config, "REMEMBER_COOKIE_SECURE", True), \
             patch.object(
                 Config,
                 "SQLALCHEMY_DATABASE_URI",
                 "postgresql+psycopg2://platform:test@db/platform",
             ), \
             patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "REDIS_URL"):
                Config.validate_production()


class HttpContractTests(unittest.TestCase):
    def test_api_echoes_request_id_in_header_and_envelope(self):
        from app import create_app

        with TemporaryDirectory() as directory:
            root = Path(directory)
            environment = {
                "DELIVERY_DATABASE_PATH": str(root / "delivery.db"),
                "PLATFORM_DATA_DIR": str(root),
            }
            with patch.dict(os.environ, environment):
                client = create_app(_test_config(root)).test_client()
                response = client.get(
                    "/api/v1/health",
                    headers={"X-Request-ID": "acceptance-request-1"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["X-Request-ID"], "acceptance-request-1")
        self.assertEqual(
            response.get_json()["meta"]["request_id"],
            "acceptance-request-1",
        )

    def test_readiness_fails_when_configured_redis_is_unavailable(self):
        from app import create_app

        with TemporaryDirectory() as directory:
            root = Path(directory)
            environment = {
                "DELIVERY_DATABASE_PATH": str(root / "delivery.db"),
                "PLATFORM_DATA_DIR": str(root),
                "REDIS_URL": "redis://unavailable.invalid:6379/0",
            }
            with patch.dict(os.environ, environment), \
                 patch("app.redis_client.ping", return_value=False):
                client = create_app(_test_config(root)).test_client()
                response = client.get("/readyz")

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["status"], "not_ready")


class WorkerSafetyConfigurationTests(unittest.TestCase):
    def test_worker_has_bounded_runtime_and_result_retention(self):
        from app.worker import celery_app

        self.assertGreater(celery_app.conf.task_time_limit, 0)
        self.assertGreater(celery_app.conf.task_soft_time_limit, 0)
        self.assertGreaterEqual(
            celery_app.conf.task_time_limit,
            celery_app.conf.task_soft_time_limit,
        )
        self.assertGreater(celery_app.conf.result_expires, 0)


if __name__ == "__main__":
    unittest.main()
