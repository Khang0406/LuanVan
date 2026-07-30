"""Alerting engine — check thresholds and generate alerts."""

from __future__ import annotations

from datetime import datetime
import hashlib
from pathlib import Path
from typing import Any

from app.config import BASE_DIR
from app.json_store import is_list_of_dicts, read_json, write_json

from .collector import application_metrics as kubectl_app_metrics
from .collector import node_metrics as kubectl_node_metrics
from .prometheus import get_prometheus_client
from .notifications import send_alert_email

ALERTS_FILE = BASE_DIR / "app" / "data" / "alerts.json"

# Configurable thresholds
DEFAULT_THRESHOLDS = {
    "cpu_percent": 80,        # node CPU > 80%
    "memory_percent": 90,     # node RAM > 90%
    "pod_down_count": 0,      # any pod not Ready triggers alert
    "restart_count": 5,       # pod restart > 5 times
}


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_alerts() -> list[dict[str, Any]]:
    return read_json(ALERTS_FILE, [], is_list_of_dicts)


def save_alerts(alerts: list[dict[str, Any]]) -> None:
    if not is_list_of_dicts(alerts):
        raise ValueError("alerts must be a list of objects")
    write_json(ALERTS_FILE, alerts)


def get_prometheus_node_metrics() -> list[dict[str, Any]]:
    """Get node metrics from Prometheus (fallback to kubectl if unavailable)."""
    client = get_prometheus_client()
    if not client.is_available():
        return []

    cpu_results = client.node_cpu_percent()
    if not cpu_results:
        return []

    mem_results = client.node_memory_percent()
    ready_results = client.node_ready()
    nodes: list[dict[str, Any]] = []

    # build a set of all instance names
    instances: dict[str, dict[str, Any]] = {}
    for r in cpu_results:
        inst = r.get("metric", {}).get("instance", "unknown")
        inst = inst.rsplit(":", 1)[0]
        try:
            cpu_pct = float(r.get("value", [None, 0])[1])
        except (TypeError, ValueError):
            cpu_pct = 0.0
        instances[inst] = {"name": inst, "cpu_percent": round(cpu_pct, 1), "cpu_cores": 0, "memory_percent": 0, "memory_bytes": 0}

    for r in mem_results:
        inst = r.get("metric", {}).get("instance", "unknown")
        inst = inst.rsplit(":", 1)[0]
        try:
            mem_pct = float(r.get("value", [None, 0])[1])
        except (TypeError, ValueError):
            mem_pct = 0.0
        if inst in instances:
            instances[inst]["memory_percent"] = round(mem_pct, 1)
        else:
            instances[inst] = {"name": inst, "cpu_percent": 0, "cpu_cores": 0, "memory_percent": round(mem_pct, 1), "memory_bytes": 0}

    readiness = {}
    for row in ready_results:
        name = row.get("metric", {}).get("node", "")
        try:
            readiness[name] = float(row.get("value", [None, 0])[1]) == 1
        except (TypeError, ValueError):
            readiness[name] = False
    for node in instances.values():
        matching = next(
            (ready for name, ready in readiness.items() if name in node["name"] or node["name"] in name),
            None,
        )
        if matching is not None:
            node["ready"] = matching

    return list(instances.values())


def get_prometheus_app_metrics(applications: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Get application pod metrics from Prometheus."""
    client = get_prometheus_client()
    if not client.is_available():
        return []

    # Quick check: does Prometheus have any kube_pod_info data at all?
    any_pods = client.total_pods()
    if any_pods == 0:
        return []

    result: list[dict[str, Any]] = []
    for app in applications:
        namespace = app.get("namespace", "")
        ready_result = client.pod_ready_count(namespace)
        restart_result = client.pod_restarts_total(namespace)
        cpu_result = client.pod_cpu_millicores(namespace)
        memory_result = client.pod_memory_bytes(namespace)
        desired_replicas, ready_replicas = client.deployment_replicas(namespace)
        hpas = client.hpa_replicas(namespace)

        total = sum(int(float(r.get("value", [None, 0])[1])) for r in ready_result)
        ready = total  # pod_ready_count already filters for ready=true
        restarts = sum(int(float(r.get("value", [None, 0])[1])) for r in restart_result)

        # also get total pod count
        total_pods_raw = client.total_pods(namespace)
        actual_total = total_pods_raw if total_pods_raw > 0 else total
        cpu_by_pod = {
            r.get("metric", {}).get("pod", ""): float(r.get("value", [None, 0])[1])
            for r in cpu_result
        }
        memory_by_pod = {
            r.get("metric", {}).get("pod", ""): float(r.get("value", [None, 0])[1])
            for r in memory_result
        }
        restart_by_pod = {
            r.get("metric", {}).get("pod", ""): int(float(r.get("value", [None, 0])[1]))
            for r in restart_result
        }
        pod_names = sorted(set(cpu_by_pod) | set(memory_by_pod) | set(restart_by_pod))

        result.append({
            "app_id": app["id"],
            "namespace": namespace,
            "total_pods": actual_total,
            "ready_pods": ready,
            "restarts": restarts,
            "cpu_millicores": round(sum(cpu_by_pod.values()), 2),
            "memory_bytes": round(sum(memory_by_pod.values()), 2),
            "desired_replicas": desired_replicas,
            "ready_replicas": ready_replicas,
            "deployment_version": app.get("current_deployment_id", ""),
            "image_digests": [
                service.get("image_digest", "") for service in app.get("services", [])
                if service.get("image_digest")
            ],
            "hpas": hpas,
            "metric_source": "Prometheus",
            "status": "Healthy" if actual_total > 0 and ready == actual_total else "Degraded" if ready > 0 else "Down",
            "pods": [
                {
                    "name": name,
                    "cpu_millicores": round(cpu_by_pod.get(name, 0), 2),
                    "memory_bytes": round(memory_by_pod.get(name, 0), 2),
                    "restarts": restart_by_pod.get(name, 0),
                }
                for name in pod_names
            ],
        })

    return result


def check_thresholds(
    applications: list[dict[str, Any]],
    nodes: list[dict[str, Any]] | None = None,
    app_metrics: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Run all threshold checks and return current alerts. Does NOT persist — caller decides.

    Uses Prometheus metrics when available; falls back to kubectl collector.py.
    Accepts pre-fetched nodes/app_metrics to avoid duplicate queries.
    """
    alerts: list[dict[str, Any]] = []
    thresholds = DEFAULT_THRESHOLDS

    if nodes is None:
        nodes = get_prometheus_node_metrics()
        if not nodes:
            nodes = kubectl_node_metrics()

    for node in nodes:
        if node.get("ready") is False:
            alerts.append(_alert(
                "NODE_OFFLINE", "CRITICAL", f"Node {node['name']} không Ready.",
                target=node["name"],
            ))
        cpu_high = node.get("cpu_percent", 0) > thresholds["cpu_percent"]
        memory_high = node.get("memory_percent", 0) > thresholds["memory_percent"]
        if cpu_high or memory_high:
            alerts.append(_alert(
                "NODE_RESOURCE_HIGH",
                "CRITICAL" if memory_high else "WARNING",
                f"Node {node['name']} CPU {node.get('cpu_percent', 0)}%, RAM {node.get('memory_percent', 0)}%.",
                target=node["name"],
            ))

    if app_metrics is None:
        app_metrics = get_prometheus_app_metrics(applications)
        if not app_metrics:
            app_metrics = kubectl_app_metrics(applications)

    for app in app_metrics:
        app_id = app.get("app_id", "")
        namespace = app.get("namespace", "")
        total = app.get("total_pods", 0)
        ready = app.get("ready_pods", 0)
        if total > 0 and ready == 0:
            alerts.append(_alert(
                "APPLICATION_NO_READY_REPLICA", "CRITICAL",
                f"Application {namespace} không còn ready replica.",
                target=app_id, application_id=app_id, namespace=namespace,
            ))
        restarts = app.get("restarts", 0)
        crashloop = any(pod.get("reason") == "CrashLoopBackOff" for pod in app.get("pods", []))
        if restarts > thresholds["restart_count"] or crashloop:
            alerts.append(_alert(
                "POD_RESTART_OR_CRASHLOOP", "WARNING",
                f"Application {namespace}: {restarts} pod restart(s)"
                + ("; phát hiện CrashLoopBackOff." if crashloop else "."),
                target=app_id, application_id=app_id, namespace=namespace,
            ))
        for hpa in app.get("hpas", []):
            current = int(hpa.get("current_replicas", 0))
            maximum = int(hpa.get("max_replicas", 0))
            if maximum > 0 and current >= maximum:
                alerts.append(_alert(
                    "HPA_MAX_REPLICAS", "WARNING",
                    f"HPA {hpa.get('name', '')} đã chạm max replicas {maximum}.",
                    target=f"{app_id}:{hpa.get('name', '')}",
                    application_id=app_id, namespace=namespace,
                ))

    return alerts


def collect_and_persist(
    applications: list[dict[str, Any]],
    nodes: list[dict[str, Any]] | None = None,
    app_metrics: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Persist and notify only Firing/Resolved transitions for stable fingerprints."""
    existing = load_alerts()
    latest: dict[str, dict[str, Any]] = {}
    for alert in existing:
        fingerprint = alert.get("fingerprint", "")
        if fingerprint and fingerprint not in latest:
            latest[fingerprint] = alert

    current = check_thresholds(applications, nodes=nodes, app_metrics=app_metrics)
    current_by_fingerprint = {alert["fingerprint"]: alert for alert in current}
    transitions: list[dict[str, Any]] = []
    for fingerprint, alert in current_by_fingerprint.items():
        if latest.get(fingerprint, {}).get("state") == "Firing":
            continue
        transitions.append(alert)

    for fingerprint, previous in latest.items():
        if previous.get("state") != "Firing" or fingerprint in current_by_fingerprint:
            continue
        resolved = dict(previous)
        resolved.update({
            "time": _now(),
            "state": "Resolved",
            "message": f"RESOLVED: {previous.get('message', '')}",
            "acknowledged": True,
            "acknowledged_at": _now(),
        })
        transitions.append(resolved)

    for transition in transitions:
        sent, status = send_alert_email(transition)
        transition["notification"] = {
            "channel": "SMTP",
            "sent": sent,
            "status": status,
        }
        existing.insert(0, transition)

    # trim old (keep last 200)
    existing = existing[:200]
    save_alerts(existing)
    return existing


def acknowledge_alert(alert_id: int) -> bool:
    """Mark an alert as acknowledged."""
    alerts = load_alerts()
    if 0 <= alert_id < len(alerts):
        alerts[alert_id]["acknowledged"] = True
        alerts[alert_id]["acknowledged_at"] = _now()
        save_alerts(alerts)
        return True
    return False


def alert_summary() -> dict[str, int]:
    """Count of alerts by severity."""
    alerts = load_alerts()
    total = len(alerts)
    latest: dict[str, dict[str, Any]] = {}
    for alert in alerts:
        latest.setdefault(alert.get("fingerprint", str(id(alert))), alert)
    active_alerts = [
        alert for alert in latest.values()
        if alert.get("state", "Firing") == "Firing" and not alert.get("acknowledged")
    ]
    active = len(active_alerts)
    critical = sum(1 for a in active_alerts if a.get("severity") == "CRITICAL")
    warning = sum(1 for a in active_alerts if a.get("severity") == "WARNING")
    return {"total": total, "active": active, "critical": critical, "warning": warning}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _alert(
    alert_type: str,
    severity: str,
    message: str,
    *,
    target: str,
    application_id: str = "",
    namespace: str = "",
) -> dict[str, Any]:
    fingerprint = hashlib.sha256(f"{alert_type}|{target}".encode()).hexdigest()[:20]
    return {
        "time": _now(),
        "type": alert_type,
        "fingerprint": fingerprint,
        "state": "Firing",
        "severity": severity,
        "message": message,
        "application_id": application_id,
        "namespace": namespace,
        "acknowledged": False,
        "acknowledged_at": "",
    }
