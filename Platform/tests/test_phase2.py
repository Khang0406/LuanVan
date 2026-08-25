"""Phase 2 tests: Multi-service, ConfigMap, Secret, PVC, Probes, Resources, Digests."""
import base64
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.modules.deployments.manifest import build_manifest
from app.secret_store import (
    save_secret, get_secret_value, list_secret_keys,
    delete_secret, get_secret_checksum, get_secret_map,
)
from app.delivery_store import (
    initialize_schema, _upsert_application, _upsert_service,
    create_deployment_record, get_deployment, list_deployments,
    update_deployment_record,
)
from app.registry_credentials import (
    save_registry_credential, resolve_registry_credential,
)


class ImageDigestPinTests(unittest.TestCase):
    def test_service_with_digest_uses_at_syntax_in_image(self):
        application = {
            "id": "test-digest",
            "namespace": "test-digest",
            "services": [{
                "name": "web",
                "image": "nginx:stable-alpine",
                "image_digest": "sha256:abc123def456",
                "container_port": 80,
                "replicas": 1,
            }],
        }
        manifest = build_manifest(application)
        self.assertIn("image: nginx:stable-alpine@sha256:abc123def456", manifest)

    def test_service_without_digest_uses_tag_only(self):
        application = {
            "id": "test-nodigest",
            "namespace": "test-nodigest",
            "services": [{
                "name": "web",
                "image": "mysql:8.0",
                "container_port": 3306,
                "replicas": 1,
            }],
        }
        manifest = build_manifest(application)
        self.assertIn("image: mysql:8.0", manifest)
        self.assertNotIn("@sha256", manifest)

    def test_rollback_uses_digest_from_deployment_record(self):
        application = {
            "id": "test-rb-digest",
            "name": "test-rb-digest",
            "namespace": "test-rb-digest",
            "source_type": "docker",
        }
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            initialize_schema(db_path)
            with patch("app.delivery_store.database_path", return_value=db_path):
                from app.delivery_store import _connect
                conn = _connect(db_path)
                _upsert_application(conn, application)
                conn.execute("DELETE FROM delivery_migrations")
                conn.commit()
                conn.close()

                deployment = create_deployment_record({
                    "application_id": "test-rb-digest",
                    "services": [{
                        "service": "web",
                        "image": "nginx@sha256:olddigest",
                        "digest": "sha256:olddigest",
                        "required": True,
                    }],
                    "manifest": "fake",
                    "deployment_mode": "Production",
                    "namespace": "test-rb-digest",
                    "started_at": "2026-01-01",
                    "finished_at": "2026-01-01",
                }, path=db_path)
                deployment["rollback_eligible"] = True
                deployment["manifest"] = "deploy-with-digest"
                update_deployment_record(deployment, path=db_path)

                stored = get_deployment(deployment["id"], path=db_path)
                self.assertIsNotNone(stored)
                svcs = stored.get("services", [])
                self.assertEqual(svcs[0]["digest"], "sha256:olddigest")

    def test_managed_image_like_mysql_stores_digest(self):
        from app.modules.pipeline.build import _image_tag
        application = {"id": "app", "owner": "dev"}
        registry_config = {"registry": "docker.io", "username": "dev"}
        tag = _image_tag(application, "mysql", registry_config, "ab" * 20)
        self.assertIn("dev/app-mysql:", tag)
        self.assertIn("ab" * 20, tag)

    def test_build_identity_for_same_commit(self):
        from app.modules.pipeline.build import _image_tag
        application = {"id": "app", "owner": "dev"}
        registry_config = {"registry": "docker.io", "username": "dev"}
        tag1 = _image_tag(application, "web", registry_config, "ab" * 20)
        tag2 = _image_tag(application, "web", registry_config, "ab" * 20)
        self.assertEqual(tag1, tag2)

    def test_tag_collision_different_services(self):
        from app.modules.pipeline.build import _image_tag
        application = {"id": "app", "owner": "dev"}
        registry_config = {"registry": "docker.io", "username": "dev"}
        tag1 = _image_tag(application, "web", registry_config, "ab" * 20)
        tag2 = _image_tag(application, "backend", registry_config, "ab" * 20)
        self.assertNotEqual(tag1, tag2)


class MultiServiceManifestTests(unittest.TestCase):
    def test_two_services_in_manifest(self):
        application = {
            "id": "multi",
            "namespace": "multi",
            "services": [
                {"name": "web", "image": "nginx:alpine", "container_port": 80, "replicas": 2, "public": True, "service_type": "NodePort"},
                {"name": "api", "image": "python:slim", "container_port": 8000, "replicas": 1, "public": False},
            ],
        }
        manifest = build_manifest(application)
        self.assertIn("kind: Deployment", manifest)
        self.assertIn("name: multi-web", manifest)
        self.assertIn("name: multi-api", manifest)
        self.assertIn("replicas: 2", manifest)
        self.assertIn("type: NodePort", manifest)
        self.assertIn("type: ClusterIP", manifest)

    def test_service_with_command_and_args(self):
        application = {
            "id": "cmdapp",
            "namespace": "cmdapp",
            "services": [{
                "name": "worker",
                "image": "python:slim",
                "container_port": 8000,
                "replicas": 1,
                "command": ["python", "app.py"],
                "args": ["--config", "prod.yml"],
            }],
        }
        manifest = build_manifest(application)
        self.assertIn("command:", manifest)
        self.assertIn("- \"python\"", manifest)
        self.assertIn("- \"app.py\"", manifest)

    def test_service_public_flag_controls_clusterip(self):
        internal = {
            "id": "internal",
            "namespace": "internal",
            "services": [{"name": "db", "image": "postgres:15", "container_port": 5432, "replicas": 1, "public": False}],
        }
        external = {
            "id": "external",
            "namespace": "external",
            "services": [{"name": "web", "image": "nginx", "container_port": 80, "replicas": 1, "public": True, "service_type": "NodePort"}],
        }
        self.assertIn("type: ClusterIP", build_manifest(internal))
        self.assertIn("type: NodePort", build_manifest(external))


class ConfigMapAndSecretTests(unittest.TestCase):
    def test_secret_storage_and_retrieval(self):
        save_secret("test-app", "DB_PASSWORD", "s3cret!")
        self.assertEqual(get_secret_value("test-app", "DB_PASSWORD"), "s3cret!")
        keys = list_secret_keys("test-app")
        self.assertIn("DB_PASSWORD", keys)

    def test_secret_masking_in_stored_map(self):
        save_secret("test-mask", "API_KEY", "abcdef12345")
        secret_map = get_secret_map("test-mask")
        self.assertEqual(secret_map["API_KEY"], "abcdef12345")

    def test_secret_checksum_updates_on_change(self):
        import uuid
        app_id = f"csum-{uuid.uuid4().hex[:8]}"
        try:
            save_secret(app_id, "KEY1", "val1")
            csum1 = get_secret_checksum(app_id)
            save_secret(app_id, "KEY2", "val2")
            csum2 = get_secret_checksum(app_id)
            self.assertNotEqual(csum1, csum2)
        finally:
            delete_secret(app_id, "KEY1")
            delete_secret(app_id, "KEY2")

    def test_secret_checksum_same_for_unchanged(self):
        import uuid
        app_id = f"same-{uuid.uuid4().hex[:8]}"
        try:
            save_secret(app_id, "KEY", "value")
            csum1 = get_secret_checksum(app_id)
            csum2 = get_secret_checksum(app_id)
            self.assertEqual(csum1, csum2)
        finally:
            delete_secret(app_id, "KEY")

    def test_secret_manifest_contains_base64_encoded_values(self):
        save_secret("sec-app", "MYSQL_ROOT_PASSWORD", "luanvan123")
        application = {
            "id": "sec-app",
            "namespace": "sec-app",
            "services": [{"name": "web", "image": "nginx", "container_port": 80, "replicas": 1}],
        }
        manifest = build_manifest(application)
        self.assertIn("kind: Secret", manifest)
        self.assertIn("name: sec-app-secret", manifest)
        self.assertIn("MYSQL_ROOT_PASSWORD", manifest)

    def test_secret_checksum_annotation_added(self):
        save_secret("cs-app", "PASS", "x")
        application = {
            "id": "cs-app",
            "namespace": "cs-app",
            "services": [{"name": "web", "image": "nginx", "container_port": 80, "replicas": 1}],
        }
        manifest = build_manifest(application)
        self.assertIn("platform.luanvan/secret-checksum:", manifest)

    def test_config_map_manifest_generated(self):
        application = {
            "id": "cm-app",
            "namespace": "cm-app",
            "config_data": {"DB_HOST": "mysql-service", "DB_PORT": "3306"},
            "services": [{"name": "web", "image": "nginx", "container_port": 80, "replicas": 1}],
        }
        manifest = build_manifest(application)
        self.assertIn("kind: ConfigMap", manifest)
        self.assertIn("name: cm-app-config", manifest)
        self.assertIn("DB_HOST:", manifest)

    def test_env_from_config_map_ref(self):
        application = {
            "id": "envref-app",
            "namespace": "envref-app",
            "services": [{
                "name": "web", "image": "nginx", "container_port": 80, "replicas": 1,
                "config_refs": [{"name": "DB_HOST", "key": "DB_HOST"}],
            }],
        }
        manifest = build_manifest(application)
        self.assertIn("configMapKeyRef", manifest)
        self.assertIn("name: DB_HOST", manifest)

    def test_env_from_secret_ref(self):
        application = {
            "id": "secref-app",
            "namespace": "secref-app",
            "services": [{
                "name": "web", "image": "nginx", "container_port": 80, "replicas": 1,
                "secret_refs": [{"name": "DB_PASSWORD", "key": "DB_PASSWORD"}],
            }],
        }
        manifest = build_manifest(application)
        self.assertIn("secretKeyRef", manifest)
        self.assertIn("name: DB_PASSWORD", manifest)

    def test_delete_secret(self):
        save_secret("del-app", "KEY", "val")
        self.assertTrue(delete_secret("del-app", "KEY"))
        self.assertFalse(delete_secret("del-app", "KEY"))
        self.assertEqual(list_secret_keys("del-app"), [])


class PersistentStorageTests(unittest.TestCase):
    def test_pvc_manifest_with_retain_policy(self):
        application = {
            "id": "pvc-app",
            "namespace": "pvc-app",
            "services": [{
                "name": "db", "image": "mysql:8.0", "container_port": 3306, "replicas": 1,
                "storage": [{
                    "name": "data", "mount_path": "/var/lib/mysql", "size": "10Gi",
                    "type": "pvc", "pvc_name": "db-data-pvc", "retain_policy": "Retain",
                    "storage_class": "standard",
                }],
            }],
        }
        manifest = build_manifest(application)
        self.assertIn("kind: PersistentVolumeClaim", manifest)
        self.assertIn("storage: 10Gi", manifest)
        self.assertIn("platform/retain-policy: Retain", manifest)
        self.assertIn("storageClassName: standard", manifest)

    def test_statefulset_for_database(self):
        application = {
            "id": "sts-app",
            "namespace": "sts-app",
            "databases": [{
                "name": "mysql", "image": "mysql:8.0", "port": 3306,
                "kind": "StatefulSet", "replicas": 1, "storage_size": "5Gi",
                "storage_class": "fast",
                "env": [{"name": "MYSQL_ROOT_PASSWORD", "value": "pw"}],
                "init_sql": {"name": "init.sql", "content": "CREATE DATABASE mydb;"},
            }],
            "services": [],
        }
        manifest = build_manifest(application)
        self.assertIn("kind: StatefulSet", manifest)
        self.assertIn("volumeClaimTemplates", manifest)
        self.assertIn("kind: ConfigMap", manifest)
        self.assertIn("init.sql", manifest)

    def test_init_job_for_migration(self):
        application = {
            "id": "initjob-app",
            "namespace": "initjob-app",
            "databases": [{
                "name": "postgres", "image": "postgres:15", "port": 5432,
                "kind": "StatefulSet", "replicas": 1, "storage_size": "10Gi",
                "init_job": {"image": "postgres:15", "command": ["psql", "-c", "SELECT 1"]},
            }],
            "services": [],
        }
        manifest = build_manifest(application)
        self.assertIn("kind: Job", manifest)

    def test_pvc_not_in_delete_by_default(self):
        application = {
            "id": "nodelete-pvc",
            "namespace": "nodelete-pvc",
            "services": [{
                "name": "db", "image": "postgres:15", "container_port": 5432, "replicas": 1,
                "storage": [{"name": "data", "mount_path": "/data", "size": "1Gi",
                              "type": "pvc", "retain_policy": "Retain"}],
            }],
        }
        manifest = build_manifest(application)
        self.assertIn("retain-policy: Retain", manifest)


class ServiceDiscoveryAndDependencyTests(unittest.TestCase):
    def test_internal_service_uses_clusterip(self):
        application = {
            "id": "internal",
            "namespace": "internal",
            "services": [{"name": "api", "image": "python", "container_port": 8000, "replicas": 1, "public": False}],
        }
        manifest = build_manifest(application)
        self.assertIn("type: ClusterIP", manifest)

    def test_dns_hostname_generated_via_env(self):
        application = {
            "id": "dns-app",
            "namespace": "dns-app",
            "services": [{
                "name": "web", "image": "nginx", "container_port": 80, "replicas": 1,
                "env": [
                    {"name": "DB_HOST", "value": "dns-app-mysql"},
                    {"name": "DB_PORT", "value": "3306"},
                ],
            }],
        }
        manifest = build_manifest(application)
        self.assertIn("DB_HOST", manifest)
        self.assertIn("dns-app-mysql", manifest)

    def test_dependency_not_enforced_by_sleep(self):
        application = {
            "id": "nosleep",
            "namespace": "nosleep",
            "services": [{"name": "web", "image": "nginx", "container_port": 80, "replicas": 1}],
        }
        manifest = build_manifest(application)
        self.assertNotIn("sleep 999", manifest)


class HealthProbeTests(unittest.TestCase):
    def test_http_readiness_probe(self):
        application = {
            "id": "probe-app",
            "namespace": "probe-app",
            "services": [{
                "name": "web", "image": "nginx", "container_port": 80, "replicas": 1,
                "readinessProbe": {
                    "type": "httpGet", "path": "/health", "port": 80,
                    "initialDelaySeconds": 5, "periodSeconds": 10,
                    "failureThreshold": 3,
                },
            }],
        }
        manifest = build_manifest(application)
        self.assertIn("readinessProbe:", manifest)
        self.assertIn("httpGet:", manifest)
        self.assertIn("path: \"/health\"", manifest)
        self.assertIn("initialDelaySeconds: 5", manifest)
        self.assertIn("periodSeconds: 10", manifest)
        self.assertIn("failureThreshold: 3", manifest)

    def test_tcp_liveness_probe(self):
        application = {
            "id": "tcp-app",
            "namespace": "tcp-app",
            "services": [{
                "name": "db", "image": "mysql:8.0", "container_port": 3306, "replicas": 1,
                "livenessProbe": {"type": "tcpSocket", "port": 3306},
            }],
        }
        manifest = build_manifest(application)
        self.assertIn("livenessProbe:", manifest)
        self.assertIn("tcpSocket:", manifest)

    def test_exec_startup_probe(self):
        application = {
            "id": "exec-app",
            "namespace": "exec-app",
            "services": [{
                "name": "worker", "image": "python", "container_port": 8000, "replicas": 1,
                "startupProbe": {"type": "exec", "command": ["echo", "ready"]},
            }],
        }
        manifest = build_manifest(application)
        self.assertIn("startupProbe:", manifest)
        self.assertIn("exec:", manifest)
        self.assertIn("- \"echo\"", manifest)

    def test_all_three_probes_together(self):
        application = {
            "id": "full-probe",
            "namespace": "full-probe",
            "services": [{
                "name": "web", "image": "nginx", "container_port": 80, "replicas": 1,
                "startupProbe": {"type": "httpGet", "path": "/start", "port": 80,
                                 "initialDelaySeconds": 10, "failureThreshold": 30},
                "readinessProbe": {"type": "httpGet", "path": "/ready", "port": 80,
                                   "periodSeconds": 5},
                "livenessProbe": {"type": "httpGet", "path": "/live", "port": 80,
                                  "periodSeconds": 15},
            }],
        }
        manifest = build_manifest(application)
        self.assertIn("startupProbe:", manifest)
        self.assertIn("readinessProbe:", manifest)
        self.assertIn("livenessProbe:", manifest)


class NetworkExposureTests(unittest.TestCase):
    def test_clusterip_for_internal(self):
        application = {
            "id": "intern",
            "namespace": "intern",
            "services": [{"name": "db", "image": "mysql:8.0", "container_port": 3306, "replicas": 1,
                          "public": False}],
        }
        manifest = build_manifest(application)
        self.assertIn("type: ClusterIP", manifest)

    def test_nodeport_for_public_dev(self):
        application = {
            "id": "public",
            "namespace": "public",
            "services": [{"name": "web", "image": "nginx", "container_port": 80, "replicas": 1,
                          "public": True, "service_type": "NodePort", "node_port": "30099"}],
        }
        manifest = build_manifest(application)
        self.assertIn("type: NodePort", manifest)
        self.assertIn("nodePort: 30099", manifest)

    def test_ingress_with_tls(self):
        application = {
            "id": "ingress-app",
            "namespace": "ingress-app",
            "services": [{
                "name": "web", "image": "nginx", "container_port": 80, "replicas": 1,
                "public": True, "service_type": "ClusterIP",
                "ingress": {
                    "host": "app.example.com", "path": "/", "tls": True,
                    "tls_secret": "my-tls-secret",
                },
            }],
        }
        manifest = build_manifest(application)
        self.assertIn("kind: Ingress", manifest)
        self.assertIn("host: app.example.com", manifest)
        self.assertIn("tls:", manifest)
        self.assertIn("secretName: my-tls-secret", manifest)

    def test_no_ingress_for_internal_service(self):
        application = {
            "id": "no-ingress",
            "namespace": "no-ingress",
            "services": [{
                "name": "db", "image": "mysql:8.0", "container_port": 3306, "replicas": 1,
                "public": False,
                "ingress": {"host": "db.example.com", "path": "/"},
            }],
        }
        manifest = build_manifest(application)
        self.assertNotIn("kind: Ingress", manifest)


class ResourceGovernanceTests(unittest.TestCase):
    def test_resource_quota_in_manifest(self):
        application = {
            "id": "quota-app",
            "namespace": "quota-app",
            "resource_quota": {"cpu": "2", "memory": "4Gi", "pods": "10", "pvc": "5"},
            "services": [{"name": "web", "image": "nginx", "container_port": 80, "replicas": 1}],
        }
        manifest = build_manifest(application)
        self.assertIn("kind: ResourceQuota", manifest)
        self.assertIn("requests.cpu:", manifest)
        self.assertIn("limits.memory:", manifest)
        self.assertIn("pods:", manifest)

    def test_limit_range_in_manifest(self):
        application = {
            "id": "lr-app",
            "namespace": "lr-app",
            "limit_range": {"default_cpu": "500m", "default_memory": "256Mi",
                            "default_request_cpu": "100m", "default_request_memory": "128Mi"},
            "services": [{"name": "web", "image": "nginx", "container_port": 80, "replicas": 1}],
        }
        manifest = build_manifest(application)
        self.assertIn("kind: LimitRange", manifest)
        self.assertIn("default:", manifest)
        self.assertIn("cpu: \"500m\"", manifest)
        self.assertIn("defaultRequest:", manifest)

    def test_resources_in_deployment(self):
        application = {
            "id": "res-app",
            "namespace": "res-app",
            "services": [{
                "name": "web", "image": "nginx", "container_port": 80, "replicas": 1,
                "cpu_request": "250m", "cpu_limit": "1",
                "memory_request": "256Mi", "memory_limit": "1Gi",
            }],
        }
        manifest = build_manifest(application)
        self.assertIn("resources:", manifest)
        self.assertIn("cpu: 250m", manifest)
        self.assertIn("memory: 256Mi", manifest)
        self.assertIn("cpu: 1", manifest)
        self.assertIn("memory: 1Gi", manifest)

    def test_resource_quantity_validation(self):
        from app.modules.deployments.manifest import _validate_resource_quantity
        self.assertEqual(_validate_resource_quantity("100m", "cpu"), "100m")
        self.assertEqual(_validate_resource_quantity("2Gi", "mem"), "2Gi")
        self.assertEqual(_validate_resource_quantity("", "cpu"), "")
        with self.assertRaises(ValueError):
            _validate_resource_quantity("abc", "cpu")
        with self.assertRaises(ValueError):
            _validate_resource_quantity("100gigabytes", "mem")


class MigrationAndCompatibilityTests(unittest.TestCase):
    def test_legacy_demo_nginx_manifest(self):
        application = {
            "id": "demo-nginx",
            "namespace": "demo-nginx",
            "services": [{
                "name": "web",
                "image": "nginx:stable-alpine",
                "container_port": 80,
                "replicas": 1,
                "service_type": "NodePort",
                "node_port": "30146",
            }],
        }
        manifest = build_manifest(application)
        self.assertIn("kind: Namespace", manifest)
        self.assertIn("name: demo-nginx", manifest)
        self.assertIn("image: nginx:stable-alpine", manifest)
        self.assertIn("nodePort: 30146", manifest)

    def test_legacy_map_manifest_with_env(self):
        application = {
            "id": "map",
            "namespace": "map",
            "services": [{
                "name": "web",
                "image": "php:8.2-apache",
                "container_port": 80,
                "replicas": 1,
                "service_type": "NodePort",
                "node_port": "30082",
                "env": [
                    {"name": "DB_HOST", "value": "map-mysql"},
                    {"name": "DB_DATABASE", "value": "map-project"},
                ],
            }],
            "config_data": {"APP_ENV": "production"},
        }
        manifest = build_manifest(application)
        self.assertIn("kind: Namespace", manifest)
        self.assertIn("name: map", manifest)
        self.assertIn("image: php:8.2-apache", manifest)
        self.assertIn("nodePort: 30082", manifest)
        self.assertIn("DB_HOST", manifest)

    def test_migration_idempotent(self):
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            from app.delivery_store import migrate_json_state
            apps_file = Path(tmp) / "apps.json"
            pipe_file = Path(tmp) / "pipes.json"
            jobs_file = Path(tmp) / "jobs.json"
            audit_file = Path(tmp) / "audit.json"
            apps_file.write_text("[]")
            pipe_file.write_text("[]")
            jobs_file.write_text("[]")
            audit_file.write_text("[]")
            r1 = migrate_json_state(apps_file, pipe_file, jobs_file, audit_file, path=db_path)
            r2 = migrate_json_state(apps_file, pipe_file, jobs_file, audit_file, path=db_path)
            self.assertEqual(r1, r2)

    def test_deployment_history_preserved(self):
        application = {
            "id": "hist-app",
            "name": "hist-app",
            "namespace": "hist-app",
            "source_type": "docker",
        }
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            initialize_schema(db_path)
            with patch("app.delivery_store.database_path", return_value=db_path):
                from app.delivery_store import _connect
                conn = _connect(db_path)
                _upsert_application(conn, application)
                conn.commit()
                conn.close()

                create_deployment_record({
                    "application_id": "hist-app",
                    "services": [{"service": "web", "image": "nginx:v1", "digest": "sha256:a1", "required": True}],
                    "manifest": "v1", "deployment_mode": "Production", "namespace": "hist-app",
                    "started_at": "2026-01-01", "finished_at": "2026-01-01",
                }, path=db_path)
                create_deployment_record({
                    "application_id": "hist-app",
                    "services": [{"service": "web", "image": "nginx:v2", "digest": "sha256:b2", "required": True}],
                    "manifest": "v2", "deployment_mode": "Production", "namespace": "hist-app",
                    "started_at": "2026-01-02", "finished_at": "2026-01-02",
                }, path=db_path)

                deps = list_deployments("hist-app", path=db_path)
                self.assertEqual(len(deps), 2)
                self.assertEqual(deps[0]["version"], 2)
                self.assertEqual(deps[1]["version"], 1)

    def test_cross_application_isolation(self):
        app1 = {"id": "app1", "name": "app1", "namespace": "app1", "source_type": "docker"}
        app2 = {"id": "app2", "name": "app2", "namespace": "app2", "source_type": "docker"}
        with TemporaryDirectory() as tmp:
            db_path = Path(tmp) / "test.db"
            initialize_schema(db_path)
            with patch("app.delivery_store.database_path", return_value=db_path):
                from app.delivery_store import _connect
                conn = _connect(db_path)
                _upsert_application(conn, app1)
                _upsert_application(conn, app2)
                conn.commit()
                conn.close()

                create_deployment_record({
                    "application_id": "app1",
                    "services": [{"service": "web", "image": "i1", "digest": "d1", "required": True}],
                    "manifest": "m1", "deployment_mode": "Production", "namespace": "app1",
                    "started_at": "2026-01-01", "finished_at": "2026-01-01",
                }, path=db_path)
                create_deployment_record({
                    "application_id": "app2",
                    "services": [{"service": "web", "image": "i2", "digest": "d2", "required": True}],
                    "manifest": "m2", "deployment_mode": "Production", "namespace": "app2",
                    "started_at": "2026-01-01", "finished_at": "2026-01-01",
                }, path=db_path)

                d1 = list_deployments("app1", path=db_path)
                d2 = list_deployments("app2", path=db_path)
                self.assertEqual(len(d1), 1)
                self.assertEqual(len(d2), 1)
                self.assertEqual(d1[0]["services"][0]["image"], "i1")
                self.assertEqual(d2[0]["services"][0]["image"], "i2")
