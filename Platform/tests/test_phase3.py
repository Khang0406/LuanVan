"""Focused acceptance tests for the reduced Phase 3 scope."""

import unittest
from unittest.mock import MagicMock, patch

import yaml

from app.modules.deployments.kubectl import get_application_logs
from app.modules.deployments.manifest import build_manifest
from app.modules.monitoring.alerting import check_thresholds, collect_and_persist
from app.modules.monitoring.k8s_manifests import PROMETHEUS_RULES_YAML
from app.modules.monitoring.notifications import send_alert_email
from app.modules.monitoring.prometheus import PrometheusClient
from app.modules.monitoring.scaling import record_scale_events


class AutoscalingTests(unittest.TestCase):
    def test_hpa_reduced_scope_defaults(self):
        manifest = build_manifest({
            "id": "phase3-app",
            "namespace": "phase3-app",
            "services": [{
                "name": "frontend", "image": "nginx:alpine",
                "container_port": 80, "replicas": 2, "autoscaling": True,
            }],
        })
        docs = [doc for doc in yaml.safe_load_all(manifest) if doc]
        hpa = next(doc for doc in docs if doc["kind"] == "HorizontalPodAutoscaler")
        self.assertEqual(2, hpa["spec"]["minReplicas"])
        self.assertEqual(5, hpa["spec"]["maxReplicas"])
        self.assertEqual(
            50, hpa["spec"]["metrics"][0]["resource"]["target"]["averageUtilization"]
        )

    def test_scale_event_is_saved_only_when_replicas_change(self):
        metrics = [{
            "app_id": "phase3-app", "namespace": "phase3-app",
            "hpas": [{"name": "phase3-app-web", "current_replicas": 3, "max_replicas": 5}],
        }]
        with patch("app.modules.monitoring.scaling.load_scale_events", return_value=[]), \
             patch("app.modules.monitoring.scaling.write_json") as write:
            events = record_scale_events(metrics)
        self.assertEqual("initial", events[0]["direction"])
        self.assertEqual(3, write.call_args.args[1][0]["replicas"])


class MonitoringMetricTests(unittest.TestCase):
    def test_prometheus_queries_application_cpu_and_memory(self):
        client = PrometheusClient("http://prometheus.invalid")
        client.instant_query = MagicMock(return_value=[])
        client.pod_cpu_millicores("phase3-app")
        client.pod_memory_bytes("phase3-app")
        queries = [call.args[0] for call in client.instant_query.call_args_list]
        self.assertTrue(any("container_cpu_usage_seconds_total" in q and 'namespace="phase3-app"' in q for q in queries))
        self.assertTrue(any("container_memory_working_set_bytes" in q and 'namespace="phase3-app"' in q for q in queries))

    def test_exactly_five_core_prometheus_rules(self):
        rules = yaml.safe_load(PROMETHEUS_RULES_YAML)["groups"][0]["rules"]
        self.assertEqual(
            {
                "NodeOffline", "HighNodeResourceUsage",
                "PodRestartOrCrashLoopBackOff", "ApplicationNoReadyReplica",
                "HPAMaxReplicas",
            },
            {rule["alert"] for rule in rules},
        )


class AlertLifecycleTests(unittest.TestCase):
    def test_hpa_at_max_fires_and_then_resolves_without_repeat(self):
        apps = [{"id": "phase3-app"}]
        firing_metrics = [{
            "app_id": "phase3-app", "namespace": "phase3-app",
            "total_pods": 5, "ready_pods": 5, "restarts": 0, "pods": [],
            "hpas": [{"name": "web", "current_replicas": 5, "max_replicas": 5}],
        }]
        healthy_metrics = [{
            "app_id": "phase3-app", "namespace": "phase3-app",
            "total_pods": 2, "ready_pods": 2, "restarts": 0, "pods": [], "hpas": [],
        }]
        saved = []

        def save(value):
            saved[:] = value

        with patch("app.modules.monitoring.alerting.load_alerts", side_effect=lambda: list(saved)), \
             patch("app.modules.monitoring.alerting.save_alerts", side_effect=save), \
             patch("app.modules.monitoring.alerting.send_alert_email", return_value=(True, "sent")) as send:
            collect_and_persist(apps, nodes=[], app_metrics=firing_metrics)
            collect_and_persist(apps, nodes=[], app_metrics=firing_metrics)
            collect_and_persist(apps, nodes=[], app_metrics=healthy_metrics)
        self.assertEqual(["Firing", "Resolved"], [saved[1]["state"], saved[0]["state"]])
        self.assertEqual(2, send.call_count)

    def test_five_platform_alert_concepts_are_detected(self):
        alerts = check_thresholds(
            [],
            nodes=[
                {"name": "offline", "ready": False, "cpu_percent": 0, "memory_percent": 0},
                {"name": "busy", "ready": True, "cpu_percent": 95, "memory_percent": 20},
            ],
            app_metrics=[{
                "app_id": "app", "namespace": "app", "total_pods": 1,
                "ready_pods": 0, "restarts": 6,
                "pods": [{"reason": "CrashLoopBackOff"}],
                "hpas": [{"name": "web", "current_replicas": 5, "max_replicas": 5}],
            }],
        )
        self.assertEqual(5, len({alert["type"] for alert in alerts}))


class LogsAndNotificationTests(unittest.TestCase):
    def test_application_logs_filter_and_mask_known_secret(self):
        app = {"id": "phase3-app", "namespace": "phase3-app", "services": []}
        options = [{"service": "backend", "pod": "backend-1", "containers": ["api"]}]
        with patch("app.modules.deployments.kubectl.get_application_log_options", return_value=options), \
             patch("app.modules.deployments.kubectl.run_kubectl", return_value=(True, "password=known-value")), \
             patch("app.modules.deployments.kubectl.get_secret_map", return_value={"DB_PASSWORD": "known-value"}), \
             patch("app.modules.deployments.kubectl.add_activity"), \
             patch("app.modules.deployments.kubectl.save_application"):
            success, output = get_application_logs(
                app, service="backend", pod="backend-1", container="api",
                tail=25, since="15m",
            )
        self.assertTrue(success)
        self.assertNotIn("known-value", output)
        self.assertIn("***", output)

    def test_smtp_message_contains_state_but_not_credentials(self):
        smtp = MagicMock()
        smtp.return_value.__enter__.return_value = smtp
        env = {
            "SMTP_HOST": "smtp.example", "SMTP_PORT": "25",
            "SMTP_FROM": "platform@example", "SMTP_TO": "ops@example",
            "SMTP_STARTTLS": "false",
        }
        with patch.dict("os.environ", env, clear=True), \
             patch("app.modules.monitoring.notifications.smtplib.SMTP", smtp):
            success, _ = send_alert_email({
                "state": "Resolved", "type": "HPA_MAX_REPLICAS",
                "severity": "WARNING", "message": "HPA recovered",
            })
        self.assertTrue(success)
        sent_message = smtp.send_message.call_args.args[0]
        self.assertIn("Resolved", sent_message["Subject"])


if __name__ == "__main__":
    unittest.main()
