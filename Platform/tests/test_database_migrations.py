"""Alembic baseline tests that do not require an external database."""
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text


BASE_DIR = Path(__file__).resolve().parents[1]


class DatabaseMigrationTests(unittest.TestCase):
    def _config(self, url: str) -> Config:
        config = Config(str(BASE_DIR / "alembic.ini"))
        config.set_main_option("sqlalchemy.url", url)
        return config

    def test_fresh_database_is_built_entirely_by_migrations(self):
        with TemporaryDirectory() as directory:
            url = f"sqlite:///{Path(directory) / 'fresh.db'}"
            with patch.dict(os.environ, {"DATABASE_URL": url}):
                command.upgrade(self._config(url), "head")
            tables = set(inspect(create_engine(url)).get_table_names())
            self.assertTrue({
                "alembic_version", "users", "applications", "pipeline_runs",
                "email_verification_tokens", "security_roles", "security_user_roles",
            } <= tables)

    def test_framework_migration_backfills_legacy_identity(self):
        with TemporaryDirectory() as directory:
            url = f"sqlite:///{Path(directory) / 'legacy-user.db'}"
            config = self._config(url)
            with patch.dict(os.environ, {"DATABASE_URL": url}):
                command.upgrade(config, "0002_account_email_verification")
                engine = create_engine(url)
                with engine.begin() as connection:
                    connection.execute(text(
                        "INSERT INTO users (username, password_hash, role, status) "
                        "VALUES ('legacy', 'werkzeug-hash', 'Developer', 'Active')"
                    ))
                command.upgrade(config, "head")
            with create_engine(url).connect() as connection:
                row = connection.execute(text(
                    "SELECT fs_uniquifier, active, confirmed_at FROM users "
                    "WHERE username = 'legacy'"
                )).one()
            self.assertEqual(len(row.fs_uniquifier), 32)
            self.assertTrue(row.active)
            self.assertIsNotNone(row.confirmed_at)

    def test_baseline_can_round_trip_on_disposable_database(self):
        with TemporaryDirectory() as directory:
            url = f"sqlite:///{Path(directory) / 'rollback.db'}"
            with patch.dict(os.environ, {"DATABASE_URL": url}):
                config = self._config(url)
                command.upgrade(config, "head")
                command.downgrade(config, "base")
                self.assertNotIn("users", inspect(create_engine(url)).get_table_names())
                command.upgrade(config, "head")
            self.assertIn("users", inspect(create_engine(url)).get_table_names())


if __name__ == "__main__":
    unittest.main()
