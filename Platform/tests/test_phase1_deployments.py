import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.delivery_store import (
    create_deployment_record,
    get_deployment,
    list_deployments,
    replace_applications,
)
from app.modules.deployments import kubectl
from app.modules.deployments.manifest import build_manifest
from app.modules.deployments import service as deployment_service


class VerifyTests(unittest.TestCase):
    def _application(self):
        return {
            "id": "map",
            "namespace": "map",
            "services": [{
                "name": "web", "required": True, "replicas": 1,
                "service_type": "NodePort", "container_port": 80,
            }],
        }

    def test_verify_requires_rollout_replicas_ready_containers_and_service(self):
        deployment_json = json.dumps({
            "spec": {"replicas": 1},
            "status": {"readyReplicas": 1, "availableReplicas": 1, "updatedReplicas": 1},
        })
        pods_json = json.dumps({"items": [{
            "status": {"phase": "Running", "containerStatuses": [{"ready": True}]}
        }]})
        with patch.object(
            kubectl, "run_kubectl",
            side_effect=[
                (True, "successfully rolled out"),
                (True, deployment_json),
                (True, pods_json),
                (True, "{}"),
            ],
        ), patch.object(kubectl, "discover_application_url", return_value="http://node:30082"):
            success, message, details = kubectl.verify_application(self._application(), 60)
        self.assertTrue(success)
        self.assertIn("1/1 ready", message)
        self.assertEqual(details["url"], "http://node:30082")

    def test_verify_fails_when_container_is_not_ready(self):
        deployment_json = json.dumps({
            "spec": {"replicas": 1},
            "status": {"readyReplicas": 1, "availableReplicas": 1, "updatedReplicas": 1},
        })
        pods_json = json.dumps({"items": [{
            "status": {"phase": "Running", "containerStatuses": [{"ready": False}]}
        }]})
        with patch.object(
            kubectl, "run_kubectl",
            side_effect=[
                (True, "rolled out"), (True, deployment_json),
                (True, pods_json), (True, "{}"),
            ],
        ), patch.object(kubectl, "discover_application_url", return_value="http://node:30082"):
            success, message, _ = kubectl.verify_application(self._application(), 60)
        self.assertFalse(success)
        self.assertIn("pod/container Ready", message)

    def test_discovered_nodeport_url_includes_public_path(self):
        service_payload = json.dumps({
            "spec": {
                "type": "NodePort",
                "ports": [{"nodePort": 30082}],
            }
        })
        application = self._application()
        application["services"][0]["public_path"] = "/frontend/index.php"
        with patch.object(
            kubectl, "run_kubectl", return_value=(True, service_payload)
        ), patch.object(kubectl, "_get_node_ip", return_value="203.0.113.10"):
            url = kubectl.discover_application_url(application)
        self.assertEqual(
            url,
            "http://203.0.113.10:30082/frontend/index.php",
        )

    def test_production_manifest_does_not_clone_source_at_runtime(self):
        application = {
            **self._application(),
            "source_type": "github",
            "github_url": "https://github.com/example/map.git",
            "deployment_mode": "production",
            "development_fallback_enabled": False,
        }
        application["services"][0]["image"] = "khang/map-web:" + "a" * 40
        manifest = build_manifest(application)
        self.assertNotIn("initContainers:", manifest)
        self.assertNotIn("git clone", manifest)
        self.assertNotIn("docker-php-ext-install", manifest)

    def test_auto_allocated_node_port_is_persisted_before_redeploy(self):
        application = self._application()
        application["services"][0]["node_port"] = ""
        service_json = json.dumps({"spec": {"ports": [{"nodePort": 30146}]}})
        with patch.object(kubectl, "run_kubectl", return_value=(True, service_json)), \
                patch.object(kubectl, "save_application"), \
                patch.object(kubectl, "add_activity"):
            changed = kubectl.preserve_allocated_node_ports(application)
        self.assertTrue(changed)
        self.assertEqual(application["services"][0]["node_port"], "30146")


class DeploymentAndRollbackTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.database = Path(self.temp.name) / "platform.db"
        self.env = patch.dict(os.environ, {"DELIVERY_DATABASE_PATH": str(self.database)})
        self.env.start()
        self.application = {
            "id": "map", "name": "map", "owner": "khang", "namespace": "map",
            "source_type": "github", "services": [{"name": "web", "image": "khang/map-web:aaa"}],
            "status": "Running", "current_deployment_id": None,
        }
        replace_applications([self.application])

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def _target(self):
        target = create_deployment_record({
            "application_id": "map", "pipeline_run_id": None,
            "previous_deployment_id": None,
            "repository": "https://github.com/example/map.git",
            "branch": "main", "commit_sha": "a" * 40,
            "services": [{"service": "web", "image": f"khang/map-web:{'a' * 40}", "digest": "sha256:" + "1" * 64}],
            "manifest": "apiVersion: v1\nkind: Service\nmetadata:\n  name: map-web\n",
            "deployment_mode": "Production", "rollback_eligible": True,
            "namespace": "map", "url": "http://node:30082",
            "verify_status": "Ready", "status": "Ready",
            "started_at": "2026-01-01", "finished_at": "2026-01-01",
            "actor": "khang",
        })
        self.application["current_deployment_id"] = "deployment-current"
        return target

    def _common_patches(self):
        return (
            patch.object(deployment_service, "create_job", return_value={"id": "job-1"}),
            patch.object(deployment_service, "finish_job"),
            patch.object(deployment_service, "record_audit"),
            patch.object(deployment_service, "save_application"),
            patch.object(deployment_service, "add_activity"),
        )

    def test_deployment_versions_are_monotonic(self):
        first = self._target()
        second = create_deployment_record({
            **first, "id": None, "version": 999, "status": "Progressing",
        })
        self.assertEqual(first["version"], 1)
        self.assertEqual(second["version"], 2)
        self.assertEqual(len(list_deployments("map")), 2)

    def test_rollback_success_updates_current_to_new_rollback_version(self):
        target = self._target()
        patches = self._common_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patch.object(deployment_service, "run_kubectl", return_value=(True, "applied")), \
                patch.object(
                    deployment_service, "verify_application",
                    return_value=(True, "web 1/1 ready", {"url": "http://node:30082"}),
                ):
            success, _, rollback = deployment_service.rollback_application(
                self.application, target["id"], actor="khang"
            )
        self.assertTrue(success)
        self.assertEqual(rollback["rollback_of_deployment_id"], target["id"])
        self.assertEqual(self.application["current_deployment_id"], rollback["id"])
        self.assertEqual(get_deployment(rollback["id"])["status"], "Ready")

    def test_failed_rollback_does_not_overwrite_current_deployment(self):
        target = self._target()
        previous_current = self.application["current_deployment_id"]
        patches = self._common_patches()
        with patches[0], patches[1], patches[2], patches[3], patches[4], \
                patch.object(deployment_service, "run_kubectl", return_value=(False, "forbidden")):
            success, message, rollback = deployment_service.rollback_application(
                self.application, target["id"], actor="khang"
            )
        self.assertFalse(success)
        self.assertIn("forbidden", message)
        self.assertEqual(self.application["current_deployment_id"], previous_current)
        self.assertEqual(get_deployment(rollback["id"])["status"], "Failed")

    def test_cannot_rollback_deployment_from_another_application(self):
        target = self._target()
        other = {**self.application, "id": "other", "namespace": "other"}
        with patch.object(deployment_service, "record_audit"):
            success, message, rollback = deployment_service.rollback_application(
                other, target["id"], actor="khang"
            )
        self.assertFalse(success)
        self.assertIn("không thuộc application", message)
        self.assertIsNone(rollback)


if __name__ == "__main__":
    unittest.main()
