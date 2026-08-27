"""Alembic baseline tests that do not require an external database."""
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


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
            self.assertTrue({"alembic_version", "users", "applications", "pipeline_runs"} <= tables)

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
