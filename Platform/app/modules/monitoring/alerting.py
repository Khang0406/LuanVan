"""Alerting engine — check thresholds and generate alerts."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import BASE_DIR

from .collector import application_metrics, node_metrics

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
    if not ALERTS_FILE.exists():
        return []
    with ALERTS_FILE.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_alerts(alerts: list[dict[str, Any]]) -> None:
    ALERTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with ALERTS_FILE.open("w", encoding="utf-8") as f:
        json.dump(alerts, f, ensure_ascii=False, indent=2)


def check_thresholds(applications: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Run all threshold checks and return current alerts. Does NOT persist — caller decides."""
    alerts: list[dict[str, Any]] = []
    thresholds = DEFAULT_THRESHOLDS

    # Node checks
    nodes = node_metrics()
    for node in nodes:
        if node.get("cpu_percent", 0) > thresholds["cpu_percent"]:
            alerts.append(_alert("WARNING", f"Node {node['name']} CPU {node['cpu_percent']}% > {thresholds['cpu_percent']}%"))
        if node.get("memory_percent", 0) > thresholds["memory_percent"]:
            alerts.append(_alert("CRITICAL", f"Node {node['name']} RAM {node['memory_percent']}% > {thresholds['memory_percent']}%"))

    # Application pod checks
    apps = application_metrics(applications)
    for app in apps:
        down = app.get("total_pods", 0) - app.get("ready_pods", 0)
        if down > thresholds["pod_down_count"]:
            alerts.append(_alert("WARNING", f"App {app['namespace']}: {down} pod(s) not Ready"))
        restarts = app.get("restarts", 0)
        if restarts > thresholds["restart_count"]:
            alerts.append(_alert("WARNING", f"App {app['namespace']}: {restarts} pod restart(s) (threshold {thresholds['restart_count']})"))

    return alerts


def collect_and_persist(applications: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Run checks, deduplicate against recent open alerts, save new alerts."""
    existing = load_alerts()
    existing_open = [a for a in existing if not a.get("acknowledged")]

    new_alerts = check_thresholds(applications)
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