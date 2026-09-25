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
                "password_reset_tokens", "projects", "project_memberships",
                "rbac_roles", "rbac_permissions", "rbac_role_permissions",
                "api_tokens",
                "subscription_plans", "project_subscriptions",
                "subscription_upgrade_requests", "subscription_history",
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

    def test_project_migration_backfills_legacy_applications_and_memberships(self):
        with TemporaryDirectory() as directory:
            url = f"sqlite:///{Path(directory) / 'legacy-project.db'}"
            config = self._config(url)
            with patch.dict(os.environ, {"DATABASE_URL": url}):
                command.upgrade(config, "0004_account_recovery_security")
                engine = create_engine(url)
                with engine.begin() as connection:
                    connection.execute(text(
                        "INSERT INTO users "
                        "(username, password_hash, fs_uniquifier, active, role, status, "
                        "failed_login_count, status_changed_at) VALUES "
                        "('admin-old', 'hash', 'legacy-admin-id', 1, 'Admin', 'Active', 0, CURRENT_TIMESTAMP), "
                        "('dev-old', 'hash', 'legacy-dev-id', 1, 'Developer', 'Active', 0, CURRENT_TIMESTAMP)"
                    ))
                    connection.execute(text(
                        "INSERT INTO applications "
                        "(id, name, namespace, source_type, payload) VALUES "
                        "('legacy-app', 'Legacy App', 'legacy-app', 'docker', :payload)"
                    ), {"payload": '{"id":"legacy-app","name":"Legacy App"}'})
                command.upgrade(config, "head")
            with create_engine(url).connect() as connection:
                project = connection.execute(text(
                    "SELECT id, slug, owner_user_id FROM projects"
                )).one()
                application = connection.execute(text(
                    "SELECT project_id, payload FROM applications WHERE id = 'legacy-app'"
                )).one()
                memberships = connection.execute(text(
                    "SELECT m.user_id, m.status, r.key AS role_key "
                    "FROM project_memberships m JOIN rbac_roles r ON r.id = m.role_id "
                    "ORDER BY m.user_id"
                )).all()
            self.assertEqual(project.slug, "default-project")
            self.assertEqual(application.project_id, project.id)
            self.assertIn('"project_id": 1', application.payload)
            self.assertEqual(len(memberships), 2)
            self.assertTrue(all(row.status == "Active" for row in memberships))
            self.assertEqual(
                [row.role_key for row in memberships], ["project_admin", "developer"]
            )

    def test_subscription_migration_backfills_basic_plan_and_history(self):
        with TemporaryDirectory() as directory:
            url = f"sqlite:///{Path(directory) / 'legacy-subscription.db'}"
            config = self._config(url)
            with patch.dict(os.environ, {"DATABASE_URL": url}):
                command.upgrade(config, "0007_project_api_tokens")
                engine = create_engine(url)
                with engine.begin() as connection:
                    connection.execute(text(
                        "INSERT INTO users "
                        "(username, password_hash, fs_uniquifier, active, role, status, "
                        "failed_login_count, status_changed_at) VALUES "
                        "('plan-owner', 'hash', 'plan-owner-id', 1, 'Developer', "
                        "'Active', 0, CURRENT_TIMESTAMP)"
                    ))
                    owner_id = connection.execute(text(
                        "SELECT id FROM users WHERE username='plan-owner'"
                    )).scalar_one()
                    connection.execute(text(
                        "INSERT INTO projects (name, slug, description, status, owner_user_id) "
                        "VALUES ('Plan Project', 'plan-project', '', 'Active', :owner_id)"
                    ), {"owner_id": owner_id})
                command.upgrade(config, "head")
            with create_engine(url).connect() as connection:
                plans = connection.execute(text(
                    "SELECT key FROM subscription_plans ORDER BY key"
                )).scalars().all()
                subscription = connection.execute(text(
                    "SELECT p.key, s.effective_limits_json "
                    "FROM project_subscriptions s "
                    "JOIN subscription_plans p ON p.id=s.plan_id "
                    "JOIN projects project ON project.id=s.project_id "
                    "WHERE project.slug='plan-project'"
                )).one()
                history_count = connection.execute(text(
                    "SELECT COUNT(*) FROM subscription_history h "
                    "JOIN projects project ON project.id=h.project_id "
                    "WHERE project.slug='plan-project'"
                )).scalar_one()
            self.assertEqual(plans, ["basic", "custom", "pro"])
            self.assertEqual(subscription.key, "basic")
            self.assertIn('"max_applications":3', subscription.effective_limits_json)
            self.assertEqual(history_count, 1)


if __name__ == "__main__":
    unittest.main()
