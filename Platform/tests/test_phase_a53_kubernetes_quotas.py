"""Acceptance tests for project namespaces and Kubernetes quota enforcement."""
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
        SECRET_KEY = "phase-a53-test"
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{root / 'users.db'}"
        SQLALCHEMY_TRACK_MODIFICATIONS = False
        KUBERNETES_QUOTA_SYNC_ENABLED = False

    return TestConfig


class KubernetesQuotaTests(unittest.TestCase):
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
                username="k8s-owner", email="k8s-owner@example.com",
                role="Developer", status=User.STATUS_ACTIVE, active=True,
            )
            owner.set_password("password-123")
            db.session.add(owner)
            db.session.commit()
            project = create_project("Kubernetes Quota", "", owner)
            self.project_id = project.id
            self.owner_id = owner.id
            self.namespace = project.kubernetes_namespace

    def tearDown(self):
        self.environment.stop()
        self.directory.cleanup()

    @staticmethod
    def _form(name: str) -> MultiDict:
        return MultiDict([
            ("name", name), ("owner", "k8s-owner"),
            ("source_type", "docker"), ("docker_image", "nginx:1.27-alpine"),
            ("service_names", "web"), ("service_images", "nginx:1.27-alpine"),
            ("service_ports", "80"), ("service_replicas", "1"),
            ("service_types", "ClusterIP"), ("service_node_ports", ""),
            ("service_envs", ""), ("service_commands", ""),
            ("service_args", ""), ("service_publics", "on"),
        ])

    @staticmethod
    def _observed_responses(documents, *, pod_limit: str | None = None):
        namespace, quota, limit_range = documents
        quota_hard = dict(quota["spec"]["hard"])
        if pod_limit is not None:
            quota_hard["pods"] = pod_limit
        return [
            (True, "namespace/resourcequota/limitrange configured"),
            (True, json.dumps({
                "metadata": {
                    "name": namespace["metadata"]["name"],
                    "labels": namespace["metadata"]["labels"],
                },
            })),
            (True, json.dumps({"spec": {"hard": quota_hard}})),
            (True, json.dumps({"spec": limit_range["spec"]})),
        ]

    def test_basic_plan_generates_namespace_resourcequota_and_limitrange(self):
        from app.db import db
        from app.models import Project
        from app.modules.quotas.kubernetes import desired_quota_documents

        with self.app.app_context():
            project = db.session.get(Project, self.project_id)
            documents = desired_quota_documents(project)

        self.assertEqual([item["kind"] for item in documents], [
            "Namespace", "ResourceQuota", "LimitRange",
        ])
        self.assertEqual(documents[0]["metadata"]["name"], self.namespace)
        hard = documents[1]["spec"]["hard"]
        self.assertEqual(hard["pods"], "10")
        self.assertEqual(hard["services"], "10")
        self.assertEqual(hard["requests.cpu"], "2000m")
        self.assertEqual(hard["limits.memory"], "8192Mi")
        defaults = documents[2]["spec"]["limits"][0]
        self.assertEqual(defaults["defaultRequest"]["cpu"], "100m")
        self.assertEqual(defaults["default"]["memory"], "512Mi")

    def test_sync_applies_and_verifies_desired_state(self):
        from app.db import db
        from app.models import Project
        from app.modules.quotas.kubernetes import (
            desired_quota_documents,
            sync_project_quota,
        )

        with self.app.app_context():
            project = db.session.get(Project, self.project_id)
            responses = self._observed_responses(desired_quota_documents(project))
            with patch(
                "app.modules.quotas.kubernetes.run_kubectl",
                side_effect=responses,
            ) as kubectl:
                result = sync_project_quota(project)

            self.assertEqual(result["status"], "Synced")
            self.assertEqual(project.quota_sync_status, "Synced")
            self.assertEqual(project.quota_desired_hash, project.quota_observed_hash)
            self.assertIsNotNone(project.quota_synced_at)
            apply_call = kubectl.call_args_list[0]
            self.assertIn("ResourceQuota", apply_call.kwargs["stdin"])
            self.assertIn("LimitRange", apply_call.kwargs["stdin"])

    def test_inspection_detects_external_quota_drift(self):
        from app.db import db
        from app.models import Project
        from app.modules.quotas.kubernetes import (
            desired_quota_documents,
            inspect_project_quota,
        )

        with self.app.app_context():
            project = db.session.get(Project, self.project_id)
            responses = self._observed_responses(
                desired_quota_documents(project), pod_limit="999"
            )[1:]
            with patch(
                "app.modules.quotas.kubernetes.run_kubectl",
                side_effect=responses,
            ):
                result = inspect_project_quota(project)

            self.assertEqual(result["status"], "Drifted")
            self.assertNotEqual(project.quota_desired_hash, project.quota_observed_hash)

    def test_sync_failure_is_persisted_without_changing_subscription(self):
        from app.db import db
        from app.models import Project
        from app.modules.quotas.kubernetes import sync_project_quota

        with self.app.app_context():
            project = db.session.get(Project, self.project_id)
            plan_key = project.subscription.plan.key
            with patch(
                "app.modules.quotas.kubernetes.run_kubectl",
                return_value=(False, "Unable to connect to the server"),
            ):
                result = sync_project_quota(project)

            self.assertEqual(result["status"], "Error")
            self.assertIn("Unable to connect", project.quota_sync_message)
            self.assertEqual(project.subscription.plan.key, plan_key)

    def test_multiple_applications_share_the_project_namespace(self):
        from app.db import db
        from app.models import User
        from app.modules.applications.service import create_application

        with self.app.app_context():
            owner = db.session.get(User, self.owner_id)
            first = create_application(self._form("Shared One"), owner, self.project_id)
            second = create_application(self._form("Shared Two"), owner, self.project_id)

        self.assertEqual(first["namespace"], self.namespace)
        self.assertEqual(second["namespace"], self.namespace)

    def test_project_manifest_uses_shared_namespace_and_resources_for_database(self):
        from app.modules.deployments.manifest import build_manifest

        application = {
            "id": "manifest-app",
            "project_id": self.project_id,
            "namespace": self.namespace,
            "services": [{
                "name": "web", "image": "nginx:1.27-alpine",
                "container_port": 80, "replicas": 1, "service_type": "ClusterIP",
                "cpu_request": "100m", "cpu_limit": "500m",
                "memory_request": "128Mi", "memory_limit": "512Mi",
            }],
            "databases": [{
                "name": "postgres", "image": "postgres:16", "port": 5432,
                "kind": "StatefulSet", "replicas": 1, "storage_size": "1Gi",
            }],
            "resource_quota": {"cpu": "1", "pods": 1},
        }
        manifest = build_manifest(application)

        self.assertIn(f"name: {self.namespace}", manifest)
        self.assertNotIn("name: manifest-app-quota", manifest)
        self.assertIn("cpu: 100m", manifest)
        self.assertIn("memory: 128Mi", manifest)
        self.assertIn("cpu: 500m", manifest)
        self.assertIn("memory: 512Mi", manifest)

    def test_workload_cleanup_never_deletes_all_project_pods_or_namespace(self):
        from app.modules.deployments.kubectl import delete_application_workloads

        application = {
            "id": "cleanup-app", "namespace": self.namespace,
            "services": [{"name": "web", "service_type": "ClusterIP"}],
        }
        calls: list[list[str]] = []

        def retry(args, **_kwargs):
            calls.append(args)
            return True, "ok"

        def direct(args, **_kwargs):
            calls.append(args)
            if args[:2] == ["get", "pods"]:
                return True, ""
            return True, "ok"

        with patch(
            "app.modules.deployments.kubectl._run_kubectl_with_retry",
            side_effect=retry,
        ), patch(
            "app.modules.deployments.kubectl.run_kubectl", side_effect=direct,
        ), patch("app.modules.deployments.kubectl.save_application"), patch(
            "app.modules.deployments.kubectl.time.sleep"
        ):
            success, _ = delete_application_workloads(application)

        self.assertTrue(success)
        self.assertFalse(any("namespace" in call[:2] and "delete" in call for call in calls))
        self.assertFalse(any("--all" in call for call in calls))
        pod_deletes = [call for call in calls if call[:2] == ["delete", "pod"]]
        self.assertTrue(pod_deletes)
        self.assertTrue(all("-l" in call for call in pod_deletes))

    def test_plan_change_marks_kubernetes_quota_pending(self):
        from app.db import db
        from app.models import Project, User
        from app.modules.subscriptions.service import assign_plan

        with self.app.app_context():
            project = db.session.get(Project, self.project_id)
            admin = User.query.filter_by(role="Admin").first()
            project.quota_sync_status = Project.QUOTA_SYNC_SYNCED
            project.quota_observed_hash = "old-observed"
            db.session.commit()

            assign_plan(project, admin, "pro")

            self.assertEqual(project.quota_sync_status, "Pending")
            self.assertEqual(project.quota_observed_hash, "")
            self.assertIn("chờ đồng bộ", project.quota_sync_message)


if __name__ == "__main__":
    unittest.main()
