"""Generate Kubernetes manifests for Prometheus & Grafana deployment.

This module creates the kube-prometheus-stack resources directly
via kubectl (or Helm equivalently), without requiring the partner
to install Helm on the platform server.

Core resources generated:
    - Namespace: monitoring
    - Prometheus Deployment + ConfigMap (prometheus.yml) + PVC + Service
    - Grafana Deployment + ConfigMap (datasources/dashboards) + PVC + Service
    - ServiceMonitors for application pods (auto-discovered via labels)
    - RBAC: ServiceAccount, ClusterRole, ClusterRoleBinding for Prometheus

Usage (callable from both pipeline & admin panel):
    from app.modules.monitoring.k8s_manifests import (
        ensure_monitoring_namespace,
        deploy_prometheus,
        deploy_grafana,
        create_service_monitor,
    )
    ensure_monitoring_namespace()
    deploy_prometheus(storage_size="10Gi")
    deploy_grafana(storage_size="5Gi")

TODO for partner:
    - Implement `prometheus_config_map()` → generates prometheus.yml with:
        - scrape_interval: 15s
        - kubernetes_sd_configs (pod/service discovery via K8s API)
        - relabel_configs for node-exporter, kube-state-metrics, app pods
        - remote_write if needed
    - Implement `deploy_prometheus(storage_size, replicas, retention)` →
        apply Deployment + ConfigMap + PVC + Service (ClusterIP + NodePort 30900)
    - Implement `deploy_grafana(storage_size)` →
        apply Deployment + ConfigMap (datasources) + PVC + Service (NodePort 30300)
    - Implement `create_service_monitor(app_id, namespace)` →
        apply ServiceMonitor CRD targeting app.kubernetes.io/part-of label
    - Implement `deploy_monitoring_stack()` → one-shot: namespace + prometheus + grafana
    - Port existing collector.py to use Prometheus queries instead of kubectl top
"""

# PLACEHOLDER — partner fills implementation below