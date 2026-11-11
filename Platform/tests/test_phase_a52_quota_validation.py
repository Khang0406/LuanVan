"""Acceptance tests for project usage calculation and quota enforcement."""
from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from werkzeug.datastructures import MultiDict


def test_config(root: Path):
    class TestConfig:
        TESTING = True
        SECRET_KEY = "phase-a52-test"
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{root / 'users.db'}"
        SQLALCHEMY_TRACK_MODIFICATIONS = False

    return TestConfig


class QuotaValidationTests(unittest.TestCase):
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
            owner = User(
                username="quota-owner",
                email="quota-owner@example.com",
                role="Developer",
                status=User.STATUS_ACTIVE,
                active=True,
            )
            owner.set_password("password-123")
            db.session.add(owner)
            db.session.commit()
            project = create_project("Quota Project", "", owner)
            self.owner_id = owner.id
            self.owner_session = owner.fs_uniquifier
            self.project_id = project.id

    def tearDown(self):
        self.environment.stop()
        self.directory.cleanup()

    def _application(
        self,
        application_id: str = "quota-app",
        *,
        replicas: int = 1,
        service_count: int = 1,
        max_replicas: int = 10,
    ) -> dict:
        return {
            "id": application_id,
            "project_id": self.project_id,
            "name": application_id,
            "namespace": application_id,
            "source_type": "docker",
            "services": [
                {
                    "name": f"service-{index}",
                    "image": "nginx:1.27-alpine",
                    "replicas": replicas,
                    "max_replicas": max_replicas,
                    "cpu_request": "100m",
                    "cpu_limit": "500m",
                    "memory_request": "128Mi",
                    "memory_limit": "512Mi",
                }
                for index in range(service_count)
            ],
            "status": "Draft",
        }

    def _save(self, applications: list[dict]) -> None:
        from app.modules.applications.service import save_applications

        save_applications(applications)

    def _set_limit(self, key: str, value: int) -> None:
        from app.db import db
        from app.models import Project
        from app.modules.subscriptions.service import subscription_limits

        project = db.session.get(Project, self.project_id)
        limits = subscription_limits(project.subscription)
        limits[key] = value
        project.subscription.effective_limits_json = json.dumps(limits)
        db.session.commit()

    def test_kubernetes_quantities_are_normalized(self):
        from app.modules.quotas.service import cpu_to_millicores, memory_to_mib

        self.assertEqual(cpu_to_millicores("1"), 1000)
        self.assertEqual(cpu_to_millicores("0.5"), 500)
        self.assertEqual(cpu_to_millicores("250m"), 250)
        self.assertEqual(memory_to_mib("1Gi"), 1024)
        self.assertEqual(memory_to_mib("1000M"), 954)

    def test_single_application_update_does_not_replace_other_tenants(self):
        from app.modules.applications.service import load_applications, save_application

        first = self._application("first-app")
        second = self._application("second-app")
        second["project_id"] = self.project_id + 100
        self._save([first, second])

        first["status"] = "Running"
        save_application(first)

        applications = {item["id"]: item for item in load_applications()}
        self.assertEqual(set(applications), {"first-app", "second-app"})
        self.assertEqual(applications["first-app"]["status"], "Running")

    def test_usage_calculator_counts_workloads_ingress_and_storage(self):
        from app.modules.quotas.service import calculate_application_usage

        application = self._application(replicas=2)
        application["services"][0].update({
            "ingress": {"host": "quota.example.com", "path": "/"},
            "storage": [{
                "name": "data", "type": "pvc", "pvc_name": "data-pvc",
                "size": "2Gi",
            }],
        })
        application["services"].append({
            "name": "worker", "image": "worker:1.0", "replicas": 1,
            "cpu_request": "0.5", "cpu_limit": "1",
            "memory_request": "1Gi", "memory_limit": "2Gi",
        })
        application["databases"] = [{
            "name": "postgres", "kind": "StatefulSet", "replicas": 2,
            "storage_size": "3Gi",
        }]

        usage = calculate_application_usage(application)

        self.assertEqual(usage["max_applications"], 1)
        self.assertEqual(usage["max_services"], 3)
        self.assertEqual(usage["max_replicas"], 5)
        self.assertEqual(usage["cpu_request_millicores"], 900)
        self.assertEqual(usage["cpu_limit_millicores"], 3000)
        self.assertEqual(usage["memory_request_mib"], 1536)
        self.assertEqual(usage["memory_limit_mib"], 4096)
        self.assertEqual(usage["max_ingresses"], 1)
        self.assertEqual(usage["max_pvcs"], 3)
        self.assertEqual(usage["storage_mib"], 8192)

    def test_application_creation_is_rejected_before_persistence(self):
        from app.models import User
        from app.modules.applications.service import create_application, load_applications
        from app.modules.quotas.service import QuotaExceeded

        existing = [
            self._application(f"existing-{index}", service_count=0)
            for index in range(3)
        ]
        self._save(existing)
        form = MultiDict([
            ("name", "Fourth application"), ("owner", "quota-owner"),
            ("source_type", "docker"), ("docker_image", "nginx:1.27-alpine"),
            ("service_names", "web"), ("service_images", "nginx:1.27-alpine"),
            ("service_ports", "80"), ("service_replicas", "1"),
            ("service_types", "ClusterIP"), ("service_node_ports", ""),
            ("service_envs", ""), ("service_commands", ""),
            ("service_args", ""), ("service_publics", "on"),
        ])
        with self.app.app_context():
            owner = User.query.filter_by(id=self.owner_id).one()
            with self.assertRaises(QuotaExceeded) as caught:
                create_application(form, owner, self.project_id)
            self.assertEqual(caught.exception.details["resource"], "max_applications")
            self.assertEqual(caught.exception.details["used"], 3)
            self.assertEqual(caught.exception.details["requested"], 1)
            self.assertEqual(caught.exception.details["limit"], 3)
            self.assertEqual(len(load_applications()), 3)

    def test_scale_up_checks_projected_total_replicas(self):
        from app.modules.quotas.service import QuotaExceeded, validate_scale

        application = self._application(replicas=2, service_count=2, max_replicas=20)
        self._save([application])
        with self.app.app_context():
            with self.assertRaises(QuotaExceeded) as caught:
                validate_scale(application, 6)
            details = caught.exception.details
            self.assertEqual(details["resource"], "max_replicas")
            self.assertEqual(details["used"], 4)
            self.assertEqual(details["requested"], 8)
            self.assertEqual(details["projected"], 12)
            self.assertEqual(details["limit"], 10)

    def test_service_max_replicas_is_enforced(self):
        from app.modules.quotas.service import QuotaExceeded, validate_scale

        application = self._application(replicas=1, max_replicas=3)
        self._save([application])
        with self.app.app_context():
            with self.assertRaises(QuotaExceeded) as caught:
                validate_scale(application, 4)
            self.assertEqual(caught.exception.details["service"], "service-0")
            self.assertEqual(caught.exception.details["limit"], 3)

    def test_scale_down_is_allowed_while_project_is_over_quota(self):
        from app.modules.quotas.service import validate_scale

        application = self._application(replicas=6, service_count=2, max_replicas=20)
        self._save([application])
        with self.app.app_context():
            self._set_limit("max_replicas", 3)
            decision = validate_scale(application, 5)
            self.assertTrue(decision["scale_down"])
            self.assertEqual(decision["used"]["max_replicas"], 12)
            self.assertEqual(decision["projected"]["max_replicas"], 10)

    def test_deploy_is_rejected_after_plan_limit_is_reduced(self):
        from app.modules.quotas.service import QuotaExceeded, validate_deployment

        application = self._application()
        self._save([application])
        with self.app.app_context():
            self._set_limit("max_applications", 0)
            with self.assertRaises(QuotaExceeded) as caught:
                validate_deployment(application)
            self.assertEqual(caught.exception.details["projected"], 1)
            self.assertEqual(caught.exception.details["limit"], 0)

    def test_web_scale_rejects_before_calling_kubernetes(self):
        application = self._application(
            replicas=2, service_count=2, max_replicas=20
        )
        self._save([application])
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["_user_id"] = self.owner_session
            session["_fresh"] = True
            session["active_project_id"] = self.project_id
        client.get(f"/projects/{self.project_id}/subscription")
        with client.session_transaction() as session:
            csrf_token = session["_csrf_token"]

        with patch("app.ui.routes.scale_application") as kubernetes_scale:
            response = client.post(
                f"/applications/{application['id']}/scale",
                data={"csrf_token": csrf_token, "replicas": "6"},
            )

        self.assertEqual(response.status_code, 302)
        kubernetes_scale.assert_not_called()

    def test_api_returns_structured_quota_exceeded_error(self):
        application = self._application()
        self._save([application])
        with self.app.app_context():
            self._set_limit("max_replicas", 0)

        client = self.app.test_client()
        with client.session_transaction() as session:
            session["_user_id"] = self.owner_session
            session["_fresh"] = True
            session["active_project_id"] = self.project_id
        client.get(f"/projects/{self.project_id}/subscription")
        with client.session_transaction() as session:
            csrf_token = session["_csrf_token"]

        response = client.post(
            f"/api/v1/applications/{application['id']}/pipeline",
            headers={"X-CSRF-Token": csrf_token},
        )

        self.assertEqual(response.status_code, 409)
        payload = response.get_json()
        self.assertEqual(payload["error"]["code"], "QUOTA_EXCEEDED")
        self.assertEqual(payload["error"]["details"]["resource"], "max_replicas")
        self.assertEqual(payload["error"]["details"]["used"], 1)
        self.assertEqual(payload["error"]["details"]["limit"], 0)

        subscription = client.get(
            f"/api/v1/projects/{self.project_id}/subscription"
        ).get_json()["data"]
        self.assertEqual(subscription["usage"]["max_applications"], 1)
        self.assertEqual(subscription["usage"]["max_replicas"], 1)


if __name__ == "__main__":
    unittest.main()
