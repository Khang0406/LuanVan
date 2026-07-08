"""Collect metrics from K3s cluster — node resources and application pod status."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from typing import Any

from app.config import BASE_DIR

DATA_FILE = BASE_DIR / "app" / "data" / "monitoring_metrics.json"


def _run_kubectl_capture(*args: str) -> tuple[bool, str]:
    """Run kubectl against the platform's default kubeconfig, return (ok, stdout)."""
    kubeconfig = BASE_DIR / "k3s" / "kubeconfig.yaml"
    cmd = ["kubectl"]
    if kubeconfig.exists():
        cmd.extend(["--kubeconfig", str(kubeconfig)])
    cmd.extend(args)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return result.returncode == 0, result.stdout.strip()
    except Exception:
        return False, ""


def node_metrics() -> list[dict[str, Any]]:
    """Return per-node CPU / memory / disk snapshot."""
    ok, raw = _run_kubectl_capture("top", "nodes", "--no-headers")
    if not ok:
        return []
    nodes: list[dict[str, Any]] = []
    for line in raw.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        nodes.append(
            {
                "name": parts[0],
                "cpu_cores": _parse_cpu_to_m(parts[1]),
                "cpu_percent": _parse_cpu_to_percent(parts[2]),
                "memory_bytes": _parse_mem_to_bytes(parts[3]),
                "memory_percent": _parse_mem_to_percent(parts[4]),
            }
        )
    return nodes


def application_metrics(applications: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Augment application list with real pod health data from cluster."""
    ok, raw = _run_kubectl_capture("get", "pods", "--all-namespaces", "-o", "json")
    pods_data: dict[str, Any] = {}
    if ok and raw:
        try:
            pods_data = json.loads(raw)
        except json.JSONDecodeError:
            pass

    # Build a map namespace -> pod status
    ns_pods: dict[str, list[dict[str, Any]]] = {}
    for item in pods_data.get("items", []):
        ns = item.get("metadata", {}).get("namespace", "")
        ns_pods.setdefault(ns, []).append(_parse_pod_status(item))

    result: list[dict[str, Any]] = []
    for app in applications:
        namespace = app.get("namespace", "")
        pods = ns_pods.get(namespace, [])
        total = len(pods)
        ready = sum(1 for p in pods if p.get("ready"))
        restarts = sum(p.get("restarts", 0) for p in pods)
        result.append(
            {
                "app_id": app["id"],
                "namespace": namespace,
                "total_pods": total,
                "ready_pods": ready,
                "restarts": restarts,
                "status": "Healthy" if total > 0 and ready == total else "Degraded" if ready > 0 else "Down",
                "pods": pods[:10],  # limit detail
            }
        )
    return result


def cluster_summary(applications: list[dict[str, Any]]) -> dict[str, Any]:
    """High-level cluster summary cards."""
    nodes = node_metrics()
    apps = application_metrics(applications)
    total_pods = sum(a["total_pods"] for a in apps)
    ready_pods = sum(a["ready_pods"] for a in apps)
    return {
        "nodes": len(nodes),
        "node_cpu_pct": round(sum(n["cpu_percent"] for n in nodes) / max(len(nodes), 1), 1),
        "node_ram_pct": round(sum(n["memory_percent"] for n in nodes) / max(len(nodes), 1), 1),
        "apps": len(apps),
        "total_pods": total_pods,
        "ready_pods": ready_pods,
        "pod_health_pct": round(ready_pods / max(total_pods, 1) * 100, 1),
    }


def save_snapshot(applications: list[dict[str, Any]]) -> None:
    """Persist current metrics snapshot to JSON for history."""
    DATA_FILE.parent.mkdir(parents=True, exist_ok=True)
    snapshot = {
        "nodes": node_metrics(),
        "apps": application_metrics(applications),
        "summary": cluster_summary(applications),
    }
    with DATA_FILE.open("w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# parsing helpers
# ---------------------------------------------------------------------------


def _parse_cpu_to_m(val: str) -> float:
    """kubectl top output: '250m' → 250, '1' (core) → 1000."""
    val = val.strip().lower()
    if val.endswith("m"):
        return float(val[:-1])
    if val.endswith("n"):
        return float(val[:-1]) / 1_000_000
    try:
        return float(val) * 1000  # cores → millicores
    except ValueError:
        return 0.0


def _parse_cpu_to_percent(val: str) -> float:
    """kubectl top already includes '%' e.g. '5%' → 5.0."""
    return float(val.replace("%", ""))



def _parse_mem_to_bytes(val: str) -> float:
    """Parse memory quantities like '200Mi' → bytes."""
    val = val.strip()
    factors = {
        "Ki": 1024,
        "Mi": 1024**2,
        "Gi": 1024**3,
        "Ti": 1024**4,
        "K": 1000,
        "M": 1000**2,
        "G": 1000**3,
    }
    for suffix, factor in factors.items():
        if val.endswith(suffix):
            return float(val[: -len(suffix)]) * factor
    try:
        return float(val)
    except ValueError:
        return 0.0


def _parse_mem_to_percent(val: str) -> float:
    return float(val.replace("%", ""))


def _parse_pod_status(item: dict[str, Any]) -> dict[str, Any]:
    status = item.get("status", {})
    name = item.get("metadata", {}).get("name", "")
    phase = status.get("phase", "Unknown")
    conditions = status.get("conditions", [])
    ready = any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions)
    restarts = sum(s.get("restartCount", 0) for s in status.get("containerStatuses", []))
    return {"name": name, "phase": phase, "ready": ready, "restarts": restarts}