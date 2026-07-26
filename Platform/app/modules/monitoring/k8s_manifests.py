"""Generate Kubernetes manifests for Prometheus & Grafana deployment.

This module creates the kube-prometheus-stack resources directly
via kubectl (or Helm equivalently), without requiring the partner
to install Helm on the platform server.

Core resources generated:
    - Namespace: monitoring
    - Prometheus Deployment + ConfigMap (prometheus.yml) + PVC + Service
    - Grafana Deployment + ConfigMap (datasources/dashboards) + PVC + Service
    - Node Exporter DaemonSet (host-level metrics)
    - Kube-state-metrics Deployment (K8s object metrics)
    - Alertmanager Deployment + ConfigMap + Service
    - ServiceMonitors for application pods (auto-discovered via labels)
    - RBAC: ServiceAccount, ClusterRole, ClusterRoleBinding for Prometheus

Usage:
    from app.modules.monitoring.k8s_manifests import (
        ensure_monitoring_namespace,
        deploy_prometheus,
        deploy_grafana,
        deploy_node_exporter,
        deploy_kube_state_metrics,
        deploy_alertmanager,
        create_service_monitor,
        deploy_monitoring_stack,
    )
"""

from __future__ import annotations

import base64
import json
import textwrap
from typing import Any

import yaml

from app.modules.deployments.kubectl import run_kubectl
from app.modules.monitoring.grafana import CLUSTER_OVERVIEW_DASHBOARD

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

MONITORING_NAMESPACE = "monitoring"

PROMETHEUS_CONFIG_YAML = textwrap.dedent("""\
    global:
      scrape_interval: 15s
      evaluation_interval: 15s

    alerting:
      alertmanagers:
        - static_configs:
            - targets:
              - alertmanager:9093

    rule_files:
      - /etc/prometheus/rules/*.yml

    scrape_configs:
      - job_name: 'prometheus'
        static_configs:
          - targets: ['localhost:9090']

      - job_name: 'kubernetes-nodes'
        scheme: https
        tls_config:
          ca_file: /var/run/secrets/kubernetes.io/serviceaccount/ca.crt
          insecure_skip_verify: true
        bearer_token_file: /var/run/secrets/kubernetes.io/serviceaccount/token
        kubernetes_sd_configs:
          - role: node
        relabel_configs:
          - action: labelmap
            regex: __meta_kubernetes_node_label_(.+)

      - job_name: 'kubernetes-cadvisor'
        scheme: https
        metrics_path: /metrics/cadvisor
        tls_config:
          ca_file: /var/run/secrets/kubernetes.io/serviceaccount/ca.crt
          insecure_skip_verify: true
        bearer_token_file: /var/run/secrets/kubernetes.io/serviceaccount/token
        kubernetes_sd_configs:
          - role: node
        relabel_configs:
          - target_label: __metrics_path__
            replacement: /metrics/cadvisor
          - action: labelmap
            regex: __meta_kubernetes_node_label_(.+)

      - job_name: 'kubernetes-service-endpoints'
        kubernetes_sd_configs:
          - role: endpoints
        relabel_configs:
          - source_labels: [__meta_kubernetes_service_annotation_prometheus_io_scrape]
            action: keep
            regex: true
          - source_labels: [__meta_kubernetes_service_annotation_prometheus_io_scheme]
            action: replace
            target_label: __scheme__
            regex: (https?)
          - source_labels: [__meta_kubernetes_service_annotation_prometheus_io_path]
            action: replace
            target_label: __metrics_path__
            regex: (.+)
          - source_labels: [__address__, __meta_kubernetes_service_annotation_prometheus_io_port]
            action: replace
            target_label: __address__
            regex: ([^:]+)(?::\\d+)?;(\\d+)
            replacement: $1:$2
          - action: labelmap
            regex: __meta_kubernetes_service_label_(.+)
          - source_labels: [__meta_kubernetes_namespace]
            action: replace
            target_label: kubernetes_namespace
          - source_labels: [__meta_kubernetes_service_name]
            action: replace
            target_label: kubernetes_service_name

      - job_name: 'kubernetes-pods'
        kubernetes_sd_configs:
          - role: pod
        relabel_configs:
          - source_labels: [__meta_kubernetes_pod_annotation_prometheus_io_scrape]
            action: keep
            regex: true
          - source_labels: [__meta_kubernetes_pod_annotation_prometheus_io_path]
            action: replace
            target_label: __metrics_path__
            regex: (.+)
          - source_labels: [__address__, __meta_kubernetes_pod_annotation_prometheus_io_port]
            action: replace
            regex: ([^:]+)(?::\\d+)?;(\\d+)
            replacement: $1:$2
            target_label: __address__
          - action: labelmap
            regex: __meta_kubernetes_pod_label_(.+)
          - source_labels: [__meta_kubernetes_namespace]
            action: replace
            target_label: kubernetes_namespace
          - source_labels: [__meta_kubernetes_pod_name]
            action: replace
            target_label: kubernetes_pod_name

      - job_name: 'node-exporter'
        kubernetes_sd_configs:
          - role: endpoints
            namespaces:
              names:
                - monitoring
        relabel_configs:
          - source_labels: [__meta_kubernetes_service_name]
            action: keep
            regex: node-exporter
          - source_labels: [__meta_kubernetes_endpoint_port_name]
            action: keep
            regex: metrics

      - job_name: 'kube-state-metrics'
        kubernetes_sd_configs:
          - role: endpoints
            namespaces:
              names:
                - monitoring
        relabel_configs:
          - source_labels: [__meta_kubernetes_service_name]
            action: keep
            regex: kube-state-metrics
          - source_labels: [__meta_kubernetes_endpoint_port_name]
            action: keep
            regex: http
""")


# ---------------------------------------------------------------------------
# Prometheus Alert Rules
# ---------------------------------------------------------------------------

PROMETHEUS_RULES_YAML = textwrap.dedent("""\
    groups:
      - name: platform-alerts
        rules:
          - alert: HighNodeCPUUsage
            expr: avg(rate(node_cpu_seconds_total{mode!="idle"}[5m])) by (instance) * 100 > 80
            for: 5m
            labels:
              severity: warning
            annotations:
              summary: "Node {{ $labels.instance }} CPU usage > 80%"
              description: "CPU usage on {{ $labels.instance }} is {{ $value }}% for 5 minutes."

          - alert: HighNodeMemoryUsage
            expr: (1 - avg(node_memory_MemAvailable_bytes) by (instance) / avg(node_memory_MemTotal_bytes) by (instance)) * 100 > 90
            for: 5m
            labels:
              severity: critical
            annotations:
              summary: "Node {{ $labels.instance }} memory usage > 90%"
              description: "Memory usage on {{ $labels.instance }} is {{ $value }}%."

          - alert: PodFrequentlyRestarting
            expr: rate(kube_pod_container_status_restarts_total[15m]) > 0
            for: 5m
            labels:
              severity: warning
            annotations:
              summary: "Pod {{ $labels.pod }} is restarting frequently"
              description: "Pod {{ $labels.pod }} in namespace {{ $labels.namespace }} has restarted {{ $value }} times in 15m."

          - alert: PodNotReady
            expr: kube_pod_status_ready{condition="false"} == 1
            for: 5m
            labels:
              severity: warning
            annotations:
              summary: "Pod {{ $labels.pod }} is not Ready"
              description: "Pod {{ $labels.pod }} in namespace {{ $labels.namespace }} is not ready for 5 minutes."

          - alert: DeploymentReplicasMismatch
            expr: kube_deployment_spec_replicas != kube_deployment_status_replicas_available
            for: 10m
            labels:
              severity: warning
            annotations:
              summary: "Deployment {{ $labels.deployment }} replicas mismatch"
              description: "Expected {{ $labels.kube_deployment_spec_replicas }} replicas but {{ $labels.kube_deployment_status_replicas_available }} are available."
""")


# ---------------------------------------------------------------------------
# Alertmanager Config
# ---------------------------------------------------------------------------

ALERTMANAGER_CONFIG_YAML = textwrap.dedent("""\
    global:
      resolve_timeout: 5m

    route:
      group_by: ['alertname', 'severity']
      group_wait: 30s
      group_interval: 5m
      repeat_interval: 4h
      receiver: 'platform-webhook'
      routes:
        - match:
            severity: critical
          receiver: 'platform-webhook'
          group_wait: 10s
          repeat_interval: 1h

    receivers:
      - name: 'platform-webhook'
        webhook_configs:
          - url: 'http://platform-service:8000/monitoring/alerts/webhook'
            send_resolved: true

    inhibit_rules:
      - source_match:
          severity: 'critical'
        target_match:
          severity: 'warning'
        equal: ['alertname']
""")


# ---------------------------------------------------------------------------
# namespace
# ---------------------------------------------------------------------------

def ensure_monitoring_namespace() -> bool:
    """Create the monitoring namespace if it doesn't exist."""
    ok, out = run_kubectl(["get", "namespace", MONITORING_NAMESPACE, "--ignore-not-found"])
    if ok and MONITORING_NAMESPACE in out:
        return True
    ok, _ = run_kubectl(["create", "namespace", MONITORING_NAMESPACE])
    return ok


# ---------------------------------------------------------------------------
# prometheus
# ---------------------------------------------------------------------------

def prometheus_config_map() -> str:
    """Generate a Prometheus ConfigMap YAML."""
    cm = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": "prometheus-server-conf",
            "namespace": MONITORING_NAMESPACE,
        },
        "data": {
            "prometheus.yml": PROMETHEUS_CONFIG_YAML,
        },
    }
    return yaml.dump(cm, default_flow_style=False)


def prometheus_rules_config_map() -> str:
    """Generate a Prometheus alerting rules ConfigMap YAML."""
    cm = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": "prometheus-rules",
            "namespace": MONITORING_NAMESPACE,
        },
        "data": {
            "platform-alerts.yml": PROMETHEUS_RULES_YAML,
        },
    }
    return yaml.dump(cm, default_flow_style=False)


def prometheus_deployment(
    storage_size: str = "10Gi",
    replicas: int = 1,
    retention: str = "15d",
) -> str:
    """Generate Prometheus Deployment + PVC + Service YAML (one document)."""
    manifests: list[dict[str, Any]] = []

    # PVC
    manifests.append({
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": "prometheus-data",
            "namespace": MONITORING_NAMESPACE,
        },
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": storage_size}},
        },
    })

    # ConfigMap
    manifests.append(yaml.safe_load(prometheus_config_map()))

    # Rules ConfigMap
    manifests.append(yaml.safe_load(prometheus_rules_config_map()))

    # ServiceAccount
    manifests.append({
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {
            "name": "prometheus",
            "namespace": MONITORING_NAMESPACE,
        },
    })

    # ClusterRole
    manifests.append({
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "ClusterRole",
        "metadata": {"name": "prometheus"},
        "rules": [
            {
                "apiGroups": [""],
                "resources": ["nodes", "nodes/proxy", "nodes/metrics", "services", "endpoints", "pods"],
                "verbs": ["get", "list", "watch"],
            },
            {
                "apiGroups": [""],
                "resources": ["configmaps"],
                "verbs": ["get"],
            },
            {
                "apiGroups": ["discovery.k8s.io"],
                "resources": ["endpointslices"],
                "verbs": ["get", "list", "watch"],
            },
            {
                "apiGroups": ["apps"],
                "resources": ["deployments", "replicasets"],
                "verbs": ["get", "list", "watch"],
            },
            {
                "nonResourceURLs": ["/metrics"],
                "verbs": ["get"],
            },
        ],
    })

    # ClusterRoleBinding
    manifests.append({
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "ClusterRoleBinding",
        "metadata": {"name": "prometheus"},
        "roleRef": {
            "apiGroup": "rbac.authorization.k8s.io",
            "kind": "ClusterRole",
            "name": "prometheus",
        },
        "subjects": [
            {
                "kind": "ServiceAccount",
                "name": "prometheus",
                "namespace": MONITORING_NAMESPACE,
            }
        ],
    })

    # Deployment
    manifests.append({
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": "prometheus",
            "namespace": MONITORING_NAMESPACE,
            "labels": {"app": "prometheus"},
        },
        "spec": {
            "replicas": replicas,
            "selector": {"matchLabels": {"app": "prometheus"}},
            "template": {
                "metadata": {"labels": {"app": "prometheus"}},
                "spec": {
                    "serviceAccountName": "prometheus",
                    "containers": [
                        {
                            "name": "prometheus",
                            "image": "prom/prometheus:v2.54.1",
                            "args": [
                                "--config.file=/etc/prometheus/prometheus.yml",
                                f"--storage.tsdb.retention.time={retention}",
                                "--web.enable-lifecycle",
                            ],
                            "ports": [{"containerPort": 9090}],
                            "volumeMounts": [
                                {"name": "config", "mountPath": "/etc/prometheus"},
                                {"name": "rules", "mountPath": "/etc/prometheus/rules"},
                                {"name": "data", "mountPath": "/prometheus"},
                            ],
                            "resources": {
                                "requests": {"cpu": "200m", "memory": "512Mi"},
                                "limits": {"cpu": "1000m", "memory": "2Gi"},
                            },
                        }
                    ],
                    "volumes": [
                        {
                            "name": "config",
                            "configMap": {"name": "prometheus-server-conf"},
                        },
                        {
                            "name": "rules",
                            "configMap": {"name": "prometheus-rules"},
                        },
                        {
                            "name": "data",
                            "persistentVolumeClaim": {"claimName": "prometheus-data"},
                        },
                    ],
                },
            },
        },
    })

    # Service
    manifests.append({
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": "prometheus",
            "namespace": MONITORING_NAMESPACE,
            "labels": {"app": "prometheus"},
        },
        "spec": {
            "type": "NodePort",
            "selector": {"app": "prometheus"},
            "ports": [
                {
                    "name": "web",
                    "port": 9090,
                    "targetPort": 9090,
                    "nodePort": 30900,
                }
            ],
        },
    })

    return "---\n".join(yaml.dump(m, default_flow_style=False) for m in manifests)


def deploy_prometheus(
    storage_size: str = "10Gi",
    replicas: int = 1,
    retention: str = "15d",
) -> bool:
    """Apply all Prometheus manifests to the cluster. Returns True on success."""
    ensure_monitoring_namespace()
    yaml_str = prometheus_deployment(storage_size, replicas, retention)
    ok, _ = run_kubectl(["apply", "-f", "-"], stdin=yaml_str)
    return ok


# ---------------------------------------------------------------------------
# node-exporter (DaemonSet)
# ---------------------------------------------------------------------------

def node_exporter_manifest() -> str:
    """Generate Node Exporter DaemonSet + Service YAML."""
    manifests: list[dict[str, Any]] = []

    # DaemonSet
    manifests.append({
        "apiVersion": "apps/v1",
        "kind": "DaemonSet",
        "metadata": {
            "name": "node-exporter",
            "namespace": MONITORING_NAMESPACE,
            "labels": {"app": "node-exporter"},
        },
        "spec": {
            "selector": {"matchLabels": {"app": "node-exporter"}},
            "template": {
                "metadata": {
                    "labels": {"app": "node-exporter"},
                    "annotations": {
                        "prometheus.io/scrape": "true",
                        "prometheus.io/port": "9100",
                    },
                },
                "spec": {
                    "hostNetwork": True,
                    "hostPID": True,
                    "containers": [
                        {
                            "name": "node-exporter",
                            "image": "quay.io/prometheus/node-exporter:v1.8.2",
                            "args": ["--path.rootfs=/host"],
                            "ports": [
                                {"name": "metrics", "containerPort": 9100, "hostPort": 9100},
                            ],
                            "volumeMounts": [
                                {"name": "root", "mountPath": "/host", "readOnly": True},
                            ],
                            "resources": {
                                "requests": {"cpu": "50m", "memory": "64Mi"},
                                "limits": {"cpu": "200m", "memory": "128Mi"},
                            },
                        }
                    ],
                    "volumes": [
                        {"name": "root", "hostPath": {"path": "/"}},
                    ],
                    "tolerations": [
                        {"operator": "Exists"},
                    ],
                },
            },
        },
    })

    # Service (headless for discovery)
    manifests.append({
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": "node-exporter",
            "namespace": MONITORING_NAMESPACE,
            "labels": {"app": "node-exporter"},
        },
        "spec": {
            "clusterIP": "None",
            "selector": {"app": "node-exporter"},
            "ports": [
                {"name": "metrics", "port": 9100, "targetPort": 9100},
            ],
        },
    })

    return "---\n".join(yaml.dump(m, default_flow_style=False) for m in manifests)


def deploy_node_exporter() -> bool:
    """Apply Node Exporter manifests. Returns True on success."""
    ensure_monitoring_namespace()
    yaml_str = node_exporter_manifest()
    ok, _ = run_kubectl(["apply", "-f", "-"], stdin=yaml_str)
    return ok


# ---------------------------------------------------------------------------
# kube-state-metrics
# ---------------------------------------------------------------------------

def kube_state_metrics_manifest() -> str:
    """Generate Kube-state-metrics Deployment + Service YAML."""
    manifests: list[dict[str, Any]] = []

    manifests.append({
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": "kube-state-metrics",
            "namespace": MONITORING_NAMESPACE,
            "labels": {"app": "kube-state-metrics"},
        },
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": "kube-state-metrics"}},
            "template": {
                "metadata": {
                    "labels": {"app": "kube-state-metrics"},
                    "annotations": {
                        "prometheus.io/scrape": "true",
                        "prometheus.io/port": "8080",
                    },
                },
                "spec": {
                    "serviceAccountName": "prometheus",
                    "containers": [
                        {
                            "name": "kube-state-metrics",
                            "image": "registry.k8s.io/kube-state-metrics/kube-state-metrics:v2.13.0",
                            "ports": [
                                {"name": "http", "containerPort": 8080},
                                {"name": "telemetry", "containerPort": 8081},
                            ],
                            "resources": {
                                "requests": {"cpu": "50m", "memory": "64Mi"},
                                "limits": {"cpu": "200m", "memory": "256Mi"},
                            },
                        }
                    ],
                },
            },
        },
    })

    # Service
    manifests.append({
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": "kube-state-metrics",
            "namespace": MONITORING_NAMESPACE,
            "labels": {"app": "kube-state-metrics"},
        },
        "spec": {
            "clusterIP": "None",
            "selector": {"app": "kube-state-metrics"},
            "ports": [
                {"name": "http", "port": 8080, "targetPort": 8080},
                {"name": "telemetry", "port": 8081, "targetPort": 8081},
            ],
        },
    })

    return "---\n".join(yaml.dump(m, default_flow_style=False) for m in manifests)


def deploy_kube_state_metrics() -> bool:
    """Apply Kube-state-metrics manifests. Returns True on success."""
    ensure_monitoring_namespace()
    yaml_str = kube_state_metrics_manifest()
    ok, _ = run_kubectl(["apply", "-f", "-"], stdin=yaml_str)
    return ok


# ---------------------------------------------------------------------------
# alertmanager
# ---------------------------------------------------------------------------

def alertmanager_config_map() -> str:
    """Generate Alertmanager configuration ConfigMap YAML."""
    cm = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": "alertmanager-config",
            "namespace": MONITORING_NAMESPACE,
        },
        "data": {
            "alertmanager.yml": ALERTMANAGER_CONFIG_YAML,
        },
    }
    return yaml.dump(cm, default_flow_style=False)


def alertmanager_manifest() -> str:
    """Generate Alertmanager Deployment + Service YAML."""
    manifests: list[dict[str, Any]] = []

    # ConfigMap
    manifests.append(yaml.safe_load(alertmanager_config_map()))

    # Deployment
    manifests.append({
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": "alertmanager",
            "namespace": MONITORING_NAMESPACE,
            "labels": {"app": "alertmanager"},
        },
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": "alertmanager"}},
            "template": {
                "metadata": {"labels": {"app": "alertmanager"}},
                "spec": {
                    "containers": [
                        {
                            "name": "alertmanager",
                            "image": "quay.io/prometheus/alertmanager:v0.27.0",
                            "args": [
                                "--config.file=/etc/alertmanager/alertmanager.yml",
                                "--storage.path=/alertmanager",
                            ],
                            "ports": [{"containerPort": 9093}],
                            "volumeMounts": [
                                {"name": "config", "mountPath": "/etc/alertmanager"},
                                {"name": "data", "mountPath": "/alertmanager"},
                            ],
                            "resources": {
                                "requests": {"cpu": "50m", "memory": "64Mi"},
                                "limits": {"cpu": "200m", "memory": "256Mi"},
                            },
                        }
                    ],
                    "volumes": [
                        {"name": "config", "configMap": {"name": "alertmanager-config"}},
                        {"name": "data", "emptyDir": {}},
                    ],
                },
            },
        },
    })

    # Service
    manifests.append({
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": "alertmanager",
            "namespace": MONITORING_NAMESPACE,
            "labels": {"app": "alertmanager"},
        },
        "spec": {
            "type": "NodePort",
            "selector": {"app": "alertmanager"},
            "ports": [
                {
                    "name": "web",
                    "port": 9093,
                    "targetPort": 9093,
                    "nodePort": 30903,
                }
            ],
        },
    })

    return "---\n".join(yaml.dump(m, default_flow_style=False) for m in manifests)


def deploy_alertmanager() -> bool:
    """Apply Alertmanager manifests. Returns True on success."""
    ensure_monitoring_namespace()
    yaml_str = alertmanager_manifest()
    ok, _ = run_kubectl(["apply", "-f", "-"], stdin=yaml_str)
    return ok


# ---------------------------------------------------------------------------
# grafana
# ---------------------------------------------------------------------------

def grafana_config_maps() -> str:
    """Generate Grafana datasource ConfigMap YAML."""
    datasource = {
        "apiVersion": 1,
        "datasources": [
            {
                "name": "Prometheus",
                "type": "prometheus",
                "url": "http://prometheus.monitoring.svc.cluster.local:9090",
                "access": "proxy",
                "isDefault": True,
            }
        ],
    }
    cm = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": "grafana-datasources",
            "namespace": MONITORING_NAMESPACE,
        },
        "data": {
            "datasources.yaml": yaml.dump(datasource, default_flow_style=False),
        },
    }
    return yaml.dump(cm, default_flow_style=False)


def grafana_deployment(storage_size: str = "5Gi") -> str:
    """Generate Grafana Deployment + PVC + Service YAML (one document)."""
    manifests: list[dict[str, Any]] = []

    # PVC
    manifests.append({
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": "grafana-data",
            "namespace": MONITORING_NAMESPACE,
        },
        "spec": {
            "accessModes": ["ReadWriteOnce"],
            "resources": {"requests": {"storage": storage_size}},
        },
    })

    # Datasource ConfigMap
    manifests.append(yaml.safe_load(grafana_config_maps()))

    # Dashboard provider ConfigMap
    manifests.append({
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": "grafana-dashboards-provider",
            "namespace": MONITORING_NAMESPACE,
        },
        "data": {
            "provider.yaml": yaml.dump({
                "apiVersion": 1,
                "providers": [{
                    "name": "default",
                    "orgId": 1,
                    "folder": "",
                    "type": "file",
                    "disableDeletion": False,
                    "editable": True,
                    "options": {"path": "/etc/grafana/provisioning/dashboards"},
                }],
            }, default_flow_style=False),
        },
    })

    # Dashboard JSON ConfigMap (cluster-overview)
    manifests.append({
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": "grafana-dashboard-cluster-overview",
            "namespace": MONITORING_NAMESPACE,
            "labels": {"grafana_dashboard": "1"},
        },
        "data": {
            "cluster-overview.json": json.dumps(CLUSTER_OVERVIEW_DASHBOARD["dashboard"]),
        },
    })

    # Deployment
    manifests.append({
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": "grafana",
            "namespace": MONITORING_NAMESPACE,
            "labels": {"app": "grafana"},
        },
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": "grafana"}},
            "template": {
                "metadata": {"labels": {"app": "grafana"}},
                "spec": {
                    "containers": [
                        {
                            "name": "grafana",
                            "image": "grafana/grafana:11.5.1",
                            "ports": [{"containerPort": 3000}],
                            "env": [
                                {"name": "GF_SECURITY_ADMIN_USER", "value": "admin"},
                                {"name": "GF_SECURITY_ADMIN_PASSWORD", "value": "admin"},
                                {"name": "GF_AUTH_ANONYMOUS_ENABLED", "value": "true"},
                                {"name": "GF_AUTH_ANONYMOUS_ORG_ROLE", "value": "Viewer"},
                                {"name": "GF_SECURITY_ALLOW_EMBEDDING", "value": "true"},
                                {"name": "GF_SERVER_ROOT_URL", "value": "/grafana"},
                                {"name": "GF_SERVER_SERVE_FROM_SUB_PATH", "value": "true"},
                                {"name": "GF_SERVER_ENFORCE_DOMAIN", "value": "false"},
                            ],
                            "volumeMounts": [
                                {"name": "data", "mountPath": "/var/lib/grafana"},
                                {"name": "datasources", "mountPath": "/etc/grafana/provisioning/datasources"},
                                {"name": "dashboards-provider", "mountPath": "/etc/grafana/provisioning/dashboards/provider.yaml", "subPath": "provider.yaml"},
                                {"name": "dashboard-cluster-overview", "mountPath": "/etc/grafana/provisioning/dashboards/cluster-overview.json", "subPath": "cluster-overview.json"},
                            ],
                            "resources": {
                                "requests": {"cpu": "100m", "memory": "256Mi"},
                                "limits": {"cpu": "500m", "memory": "512Mi"},
                            },
                        }
                    ],
                    "volumes": [
                        {
                            "name": "data",
                            "persistentVolumeClaim": {"claimName": "grafana-data"},
                        },
                        {
                            "name": "datasources",
                            "configMap": {"name": "grafana-datasources"},
                        },
                        {
                            "name": "dashboards-provider",
                            "configMap": {"name": "grafana-dashboards-provider"},
                        },
                        {
                            "name": "dashboard-cluster-overview",
                            "configMap": {"name": "grafana-dashboard-cluster-overview"},
                        },
                    ],
                },
            },
        },
    })

    # Service
    manifests.append({
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": "grafana",
            "namespace": MONITORING_NAMESPACE,
            "labels": {"app": "grafana"},
        },
        "spec": {
            "type": "NodePort",
            "selector": {"app": "grafana"},
            "ports": [
                {
                    "name": "web",
                    "port": 3000,
                    "targetPort": 3000,
                    "nodePort": 30300,
                }
            ],
        },
    })

    return "---\n".join(yaml.dump(m, default_flow_style=False) for m in manifests)


def deploy_grafana(storage_size: str = "5Gi") -> bool:
    """Apply all Grafana manifests to the cluster. Returns True on success."""
    ensure_monitoring_namespace()
    yaml_str = grafana_deployment(storage_size)
    ok, _ = run_kubectl(["apply", "-f", "-"], stdin=yaml_str)
    return ok


# ---------------------------------------------------------------------------
# service monitor
# ---------------------------------------------------------------------------

def create_service_monitor(
    app_id: str,
    namespace: str,
    port_name: str = "http",
    path: str = "/metrics",
    interval: str = "30s",
) -> bool:
    """Create a ServiceMonitor CRD for an application (requires kube-prometheus-stack CRDs).

    Assumes the application's Service exposes metrics on the given port/path
    and carries the label ``app.kubernetes.io/part-of = <app_id>``.
    """
    sm = {
        "apiVersion": "monitoring.coreos.com/v1",
        "kind": "ServiceMonitor",
        "metadata": {
            "name": f"app-{app_id}",
            "namespace": MONITORING_NAMESPACE,
            "labels": {
                "app.kubernetes.io/part-of": app_id,
                "managed-by": "luanvan-platform",
                "release": "prometheus",
            },
        },
        "spec": {
            "selector": {
                "matchLabels": {"app.kubernetes.io/part-of": app_id},
            },
            "namespaceSelector": {
                "matchNames": [namespace],
            },
            "endpoints": [
                {
                    "port": port_name,
                    "path": path,
                    "interval": interval,
                }
            ],
        },
    }
    yaml_str = yaml.dump(sm, default_flow_style=False)
    ok, _ = run_kubectl(["apply", "-f", "-"], stdin=yaml_str)
    return ok


def deploy_service_monitor_template(app_id: str, namespace: str) -> str:
    """Return the ServiceMonitor YAML as a string (for use with Jinja2 templates)."""
    sm = {
        "apiVersion": "monitoring.coreos.com/v1",
        "kind": "ServiceMonitor",
        "metadata": {
            "name": f"app-{app_id}",
            "namespace": MONITORING_NAMESPACE,
            "labels": {
                "app.kubernetes.io/part-of": app_id,
                "managed-by": "luanvan-platform",
                "release": "prometheus",
            },
        },
        "spec": {
            "selector": {
                "matchLabels": {"app.kubernetes.io/part-of": app_id},
            },
            "namespaceSelector": {
                "matchNames": [namespace],
            },
            "endpoints": [
                {
                    "port": "http",
                    "path": "/metrics",
                    "interval": "30s",
                }
            ],
        },
    }
    return yaml.dump(sm, default_flow_style=False)


# ---------------------------------------------------------------------------
# one-shot deploy helpers
# ---------------------------------------------------------------------------

def deploy_servicemonitor(application: dict[str, Any]) -> str | None:
    """Create a ServiceMonitor for a deployed application.

    Called automatically after a successful pipeline deploy.
    Returns the ServiceMonitor name on success, None if skipped.
    """
    app_id = application.get("id", "")
    namespace = application.get("namespace", "")
    if not app_id or not namespace:
        return None
    if not application.get("services"):
        return None  # no services to scrape
    ok = create_service_monitor(app_id, namespace)
    return f"app-{app_id}" if ok else None


def deploy_monitoring_stack(
    prometheus_storage: str = "10Gi",
    grafana_storage: str = "5Gi",
) -> dict[str, bool]:
    """Deploy the full monitoring stack in one call.

    Returns a dict with success status for each component.
    """
    result = {
        "namespace": ensure_monitoring_namespace(),
        "prometheus": deploy_prometheus(storage_size=prometheus_storage),
        "node_exporter": deploy_node_exporter(),
        "kube_state_metrics": deploy_kube_state_metrics(),
        "alertmanager": deploy_alertmanager(),
        "grafana": deploy_grafana(storage_size=grafana_storage),
    }
    return result