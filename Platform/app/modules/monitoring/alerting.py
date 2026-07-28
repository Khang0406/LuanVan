"""Alerting engine — check thresholds and generate alerts."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import BASE_DIR
from app.json_store import is_list_of_dicts, read_json, write_json

from .collector import application_metrics as kubectl_app_metrics
from .collector import node_metrics as kubectl_node_metrics
from .prometheus import get_prometheus_client

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

        total = sum(int(float(r.get("value", [None, 0])[1])) for r in ready_result)
        ready = total  # pod_ready_count already filters for ready=true
        restarts = sum(int(float(r.get("value", [None, 0])[1])) for r in restart_result)

        # also get total pod count
        total_pods_raw = client.total_pods(namespace)
        actual_total = total_pods_raw if total_pods_raw > 0 else total

        result.append({
            "app_id": app["id"],
            "namespace": namespace,
            "total_pods": actual_total,
            "ready_pods": ready,
            "restarts": restarts,
            "status": "Healthy" if actual_total > 0 and ready == actual_total else "Degraded" if ready > 0 else "Down",
            "pods": [],
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
        if node.get("cpu_percent", 0) > thresholds["cpu_percent"]:
            alerts.append(_alert("WARNING", f"Node {node['name']} CPU {node['cpu_percent']}% > {thresholds['cpu_percent']}%"))
        if node.get("memory_percent", 0) > thresholds["memory_percent"]:
            alerts.append(_alert("CRITICAL", f"Node {node['name']} RAM {node['memory_percent']}% > {thresholds['memory_percent']}%"))

    if app_metrics is None:
        app_metrics = get_prometheus_app_metrics(applications)
        if not app_metrics:
            app_metrics = kubectl_app_metrics(applications)

    for app in app_metrics:
        down = app.get("total_pods", 0) - app.get("ready_pods", 0)
        if down > thresholds["pod_down_count"]:
            alerts.append(_alert("WARNING", f"App {app['namespace']}: {down} pod(s) not Ready"))
        restarts = app.get("restarts", 0)
        if restarts > thresholds["restart_count"]:
            alerts.append(_alert("WARNING", f"App {app['namespace']}: {restarts} pod restart(s) (threshold {thresholds['restart_count']})"))

    return alerts


def collect_and_persist(
    applications: list[dict[str, Any]],
    nodes: list[dict[str, Any]] | None = None,
    app_metrics: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Run checks, deduplicate against recent open alerts, save new alerts."""
    existing = load_alerts()
    existing_open = [a for a in existing if not a.get("acknowledged")]

    new_alerts = check_thresholds(applications, nodes=nodes, app_metrics=app_metrics)
    added = 0
    for alert in new_alerts:
        # simple dedup: same message in last 5 min
        if not any(a["message"] == alert["message"] for a in existing_open):
            existing.insert(0, alert)
            added += 1

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
    active = sum(1 for a in alerts if not a.get("acknowledged"))
    critical = sum(1 for a in alerts if a.get("severity") == "CRITICAL" and not a.get("acknowledged"))
    warning = sum(1 for a in alerts if a.get("severity") == "WARNING" and not a.get("acknowledged"))
    return {"total": total, "active": active, "critical": critical, "warning": warning}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _alert(severity: str, message: str) -> dict[str, Any]:
    return {
        "time": _now(),
        "severity": severity,
        "message": message,
        "acknowledged": False,
        "acknowledged_at": "",
    }