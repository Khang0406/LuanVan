"""Monitoring module — Prometheus metrics collection, Grafana dashboards, and K8s manifests."""
from app.modules.monitoring.prometheus import PrometheusClient
from app.modules.monitoring.grafana import GrafanaClient
from app.modules.monitoring.k8s_manifests import (
    deploy_servicemonitor,
    deploy_monitoring_stack,
    deploy_prometheus,
    deploy_grafana,
    deploy_node_exporter,
    deploy_kube_state_metrics,
    deploy_alertmanager,
    create_service_monitor,
    prometheus_deployment,
    grafana_deployment,
    node_exporter_manifest,
    kube_state_metrics_manifest,
    alertmanager_manifest,
)
from app.modules.monitoring.alerting import check_thresholds, acknowledge_alert, alert_summary, load_alerts, collect_and_persist, get_prometheus_node_metrics, get_prometheus_app_metrics
from app.modules.monitoring.collector import get_cached_data, start_background_collector

__all__ = [
    "PrometheusClient",
    "GrafanaClient",
    "deploy_monitoring_stack",
    "deploy_servicemonitor",
    "deploy_prometheus",
    "deploy_grafana",
    "deploy_node_exporter",
    "deploy_kube_state_metrics",
    "deploy_alertmanager",
    "create_service_monitor",
    "prometheus_deployment",
    "grafana_deployment",
    "node_exporter_manifest",
    "kube_state_metrics_manifest",
    "alertmanager_manifest",
    "check_thresholds",
    "acknowledge_alert",
    "alert_summary",
    "load_alerts",
    "collect_and_persist",
    "get_prometheus_node_metrics",
    "get_prometheus_app_metrics",
    "get_cached_data",
    "start_background_collector",
]
