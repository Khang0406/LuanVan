"""Acceptance tests for quota summaries, warnings and dashboard isolation."""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


def test_config(root: Path):
    class TestConfig:
        TESTING = True
        SECRET_KEY = "phase-a54-test"
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{root / 'users.db'}"
        SQLALCHEMY_TRACK_MODIFICATIONS = False

    return TestConfig


class QuotaDashboardTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.environment = patch.dict(os.environ, {
            "DELIVERY_DATABASE_PATH": str(self.root / "delivery.db"),
            "PLATFORM_DATA_DIR": str(self.root),
        })
        self.environment.start()

        from app import create_app
        from app.db import db
        from app.models import User
        from app.modules.projects.service import create_project

        self.app = create_app(test_config(self.root))
        with self.app.app_context():
            admin = User.query.filter_by(username="admin").one()
            owner = User(
                username="dashboard-owner",
                email="dashboard-owner@example.com",
                role="Developer",
                status=User.STATUS_ACTIVE,
                active=True,
            )
            outsider = User(
                username="dashboard-outsider",
                email="dashboard-outsider@example.com",
                role="Developer",
                status=User.STATUS_ACTIVE,
                active=True,
            )
            owner.set_password("password-123")
            outsider.set_password("password-123")
            db.session.add_all([owner, outsider])
            db.session.commit()
            project = create_project("Dashboard Project", "", owner)
            self.project_id = project.id
            self.sessions = {
                "admin": admin.fs_uniquifier,
                "owner": owner.fs_uniquifier,
                "outsider": outsider.fs_uniquifier,
            }

    def tearDown(self):
        self.environment.stop()
        self.directory.cleanup()

    def client_for(self, name: str):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["_user_id"] = self.sessions[name]
            session["_fresh"] = True
            session["active_project_id"] = self.project_id
        return client

    @staticmethod
    def csrf(client, path: str) -> str:
        client.get(path)
        with client.session_transaction() as session:
            return session["_csrf_token"]

    def save_application(self, replicas: int = 9) -> None:
        from app.modules.applications.service import save_applications

        save_applications([{
            "id": "dashboard-app",
            "project_id": self.project_id,
            "name": "Dashboard App",
            "namespace": "dashboard-project",
            "source_type": "docker",
            "services": [{
                "name": "web",
                "image": "nginx:1.27-alpine",
                "replicas": replicas,
                "max_replicas": 10,
                "cpu_request": "100m",
                "cpu_limit": "300m",
                "memory_request": "64Mi",
                "memory_limit": "256Mi",
            }],
            "status": "Running",
        }])

    def test_thresholds_include_boundaries_and_zero_limit(self):
        from app.modules.quotas.service import quota_percentage, quota_status

        self.assertEqual(quota_status(6, 10), "normal")
        self.assertEqual(quota_status(7, 10), "warning")
        self.assertEqual(quota_status(85, 100), "critical")
        self.assertEqual(quota_status(10, 10), "exhausted")
        self.assertEqual(quota_status(12, 10), "exhausted")
        self.assertEqual(quota_status(0, 0), "normal")
        self.assertEqual(quota_status(1, 0), "exhausted")
        self.assertEqual(quota_percentage(12, 10), 120.0)

    def test_dashboard_builds_remaining_percentage_and_display_values(self):
        self.save_application()
        from app.db import db
        from app.models import Project
        from app.modules.quotas.service import build_quota_dashboard

        with self.app.app_context():
            dashboard = build_quota_dashboard(db.session.get(Project, self.project_id))
            resources = {item["key"]: item for item in dashboard["resources"]}
            pods = resources["max_replicas"]
            self.assertEqual(pods["used"], 9)
            self.assertEqual(pods["remaining"], 1)
            self.assertEqual(pods["percentage"], 90.0)
            self.assertEqual(pods["status"], "critical")
            self.assertTrue(dashboard["summary"]["needs_upgrade"])
            self.assertEqual(resources["memory_request_mib"]["limit_display"], "4 GiB")

    def test_project_page_renders_progress_warning_and_upgrade_link(self):
        self.save_application()
        response = self.client_for("owner").get(
            f"/projects/{self.project_id}/subscription"
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'data-quota-resource="max_replicas"', response.data)
        self.assertIn(b'data-quota-status="critical"', response.data)
        self.assertIn(b"90.0%", response.data)
        self.assertIn("Gửi yêu cầu".encode(), response.data)

    def test_api_keeps_legacy_usage_and_adds_canonical_summary(self):
        self.save_application()
        response = self.client_for("owner").get(
            f"/api/v1/projects/{self.project_id}/subscription"
        )

        self.assertEqual(response.status_code, 200)
        data = response.get_json()["data"]
        self.assertEqual(data["usage"]["max_replicas"], 9)
        resources = {item["key"]: item for item in data["resources"]}
        self.assertEqual(resources["max_replicas"]["status"], "critical")
        self.assertEqual(resources["max_replicas"]["remaining"], 1)
        self.assertEqual(data["quota_summary"]["critical_count"], 1)
        self.assertIsNone(data["upgrade_request"])

    def test_pending_request_is_returned_and_hides_duplicate_form(self):
        owner = self.client_for("owner")
        path = f"/projects/{self.project_id}/subscription"
        created = owner.post(path, data={
            "csrf_token": self.csrf(owner, path),
            "plan": "pro",
            "reason": "Cần thêm tài nguyên cho workload production.",
        })
        self.assertEqual(created.status_code, 302)

        page = owner.get(path)
        api_data = owner.get(
            f"/api/v1/projects/{self.project_id}/subscription"
        ).get_json()["data"]
        self.assertIn("Yêu cầu nâng cấp đang chờ".encode(), page.data)
        self.assertNotIn(b'<form method="post"><input', page.data)
        self.assertEqual(api_data["upgrade_request"]["status"], "Pending")
        self.assertEqual(api_data["upgrade_request"]["requested_plan"], "pro")

    def test_dashboard_remains_tenant_isolated(self):
        outsider = self.client_for("outsider")
        self.assertEqual(
            outsider.get(f"/projects/{self.project_id}/subscription").status_code,
            404,
        )
        self.assertEqual(
            outsider.get(
                f"/api/v1/projects/{self.project_id}/subscription"
            ).status_code,
            404,
        )

    def test_admin_overview_shows_usage_and_kubernetes_status(self):
        self.save_application()
        response = self.client_for("admin").get("/admin/subscriptions")

        self.assertEqual(response.status_code, 200)
        self.assertIn("Tổng quan quota theo project".encode(), response.data)
        self.assertIn(b'data-project-quota-status="critical"', response.data)
        self.assertIn(b"90.0%", response.data)


if __name__ == "__main__":
    unittest.main()
