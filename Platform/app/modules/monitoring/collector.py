"""Collect metrics from K3s cluster — node resources and application pod status."""

from __future__ import annotations

import json
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

import threading
import time
from collections import deque

from app.config import BASE_DIR

DATA_FILE = BASE_DIR / "app" / "data" / "monitoring_metrics.json"

# ---------------------------------------------------------------------------
# Time-series ring buffer — fallback chart data when Prometheus is unavailable
# ---------------------------------------------------------------------------
_MAX_SAMPLES = 60  # ~30 min at 30s intervals (30*60/30=60)

_chart_history: dict[str, deque[dict[str, Any]]] = {
    "cpu": deque(maxlen=_MAX_SAMPLES),
    "memory": deque(maxlen=_MAX_SAMPLES),
    "network_rx": deque(maxlen=_MAX_SAMPLES),
    "network_tx": deque(maxlen=_MAX_SAMPLES),
}
_chart_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Background collector cache — avoids blocking page loads
# ---------------------------------------------------------------------------
_cache_lock = threading.Lock()
_cache: dict[str, Any] = {
    "nodes": [],
    "app_metrics": [],
    "summary": {"nodes": 0, "node_cpu_pct": 0, "node_ram_pct": 0,
                 "apps": 0, "total_pods": 0, "ready_pods": 0, "pod_health_pct": 0},
    "alerts": [],
    "alert_counts": {"total": 0, "active": 0, "critical": 0, "warning": 0},
    "grafana_embed": "",
    "fetched_at": "",
}
_collector_running = False
_collector_thread: threading.Thread | None = None


def get_cached_data() -> dict[str, Any]:
    """Return the latest cached monitoring snapshot (non-blocking)."""
    with _cache_lock:
        return dict(_cache)


def start_background_collector(interval: float = 30.0) -> None:
    """Launch a daemon thread that refreshes the cache every *interval* seconds."""
    global _collector_running, _collector_thread
    if _collector_running:
        return
    _collector_running = True
    _collector_thread = threading.Thread(target=_collect_loop, args=(interval,), daemon=True)
    _collector_thread.start()


def _collect_loop(interval: float) -> None:
    """Background loop: collect Prometheus+kubectl data and update cache."""
    while _collector_running:
        try:
            _refresh_cache()
        except Exception:
            pass
        time.sleep(interval)


def _refresh_cache() -> None:
    """Single refresh cycle — tries Prometheus first, falls back to kubectl."""
    import importlib

    svc = importlib.import_module("app.modules.applications.service")
    alert = importlib.import_module("app.modules.monitoring.alerting")
    graf = importlib.import_module("app.modules.monitoring.grafana")

    apps: list[dict[str, Any]] = []
    try:
        apps = svc.load_applications()
    except Exception:
        pass

    # --- node metrics ---
    nodes: list[dict[str, Any]] = []
    try:
        nodes = alert.get_prometheus_node_metrics()
    except Exception:
        pass
    if not nodes:
        try:
            nodes = node_metrics()
        except Exception:
            pass
    if not nodes:
        try:
            nodes = node_metrics_via_ssh()
        except Exception:
            pass

    # --- app metrics ---
    app_metrics_list: list[dict[str, Any]] = []
    try:
        app_metrics_list = alert.get_prometheus_app_metrics(apps)
    except Exception:
        pass
    if not app_metrics_list:
        try:
            app_metrics_list = application_metrics(apps)
        except Exception:
            pass

    # --- summary ---
    total_pods = sum(a.get("total_pods", 0) for a in app_metrics_list)
    ready_pods = sum(a.get("ready_pods", 0) for a in app_metrics_list)
    summary = {
        "nodes": len(nodes),
        "node_cpu_pct": round(sum(n.get("cpu_percent", 0) for n in nodes) / max(len(nodes), 1), 1),
        "node_ram_pct": round(sum(n.get("memory_percent", 0) for n in nodes) / max(len(nodes), 1), 1),
        "apps": len(apps),
        "total_pods": total_pods,
        "ready_pods": ready_pods,
        "pod_health_pct": round(ready_pods / max(total_pods, 1) * 100, 1),
    }

    # --- alerts ---
    all_alerts: list[dict[str, Any]] = []
    try:
        all_alerts = alert.collect_and_persist(apps, nodes=nodes, app_metrics=app_metrics_list)
    except Exception:
        pass

    alert_counts: dict[str, int] = {"total": 0, "active": 0, "critical": 0, "warning": 0}
    try:
        alert_counts = alert.alert_summary()
    except Exception:
        pass

    # --- grafana ---
    grafana_embed: str = ""
    try:
        grafana_embed = graf.get_grafana_embed_url() or ""
    except Exception:
        pass

    # --- ring buffer ---
    try:
        record_snapshot(apps, nodes=nodes)
    except Exception:
        pass

    # --- update cache ---
    with _cache_lock:
        _cache["nodes"] = nodes
        _cache["app_metrics"] = app_metrics_list
        _cache["summary"] = summary
        _cache["alerts"] = all_alerts
        _cache["alert_counts"] = alert_counts
        _cache["grafana_embed"] = grafana_embed
        _cache["fetched_at"] = datetime.now().strftime("%H:%M:%S")


def record_snapshot(
    applications: list[dict[str, Any]] | None = None,
    nodes: list[dict[str, Any]] | None = None,
) -> None:
    """Sample current kubectl metrics into the ring buffer for charting."""
    now_ts = int(time.time())
    if nodes is None:
        nodes = node_metrics()

    cpu_samples: list[dict[str, Any]] = []
    mem_samples: list[dict[str, Any]] = []
    net_rx: list[dict[str, Any]] = []
    net_tx: list[dict[str, Any]] = []

    for node in nodes:
        name = node.get("name", "unknown")
        cpu_samples.append({"instance": name, "value": node.get("cpu_percent", 0)})
        mem_samples.append({"instance": name, "value": node.get("memory_percent", 0)})
        net_rx.append({"instance": name, "value": 0})   # kubectl top doesn't have network
        net_tx.append({"instance": name, "value": 0})

    with _chart_lock:
        _chart_history["cpu"].append({"ts": now_ts, "data": cpu_samples})
        _chart_history["memory"].append({"ts": now_ts, "data": mem_samples})
        _chart_history["network_rx"].append({"ts": now_ts, "data": net_rx})
        _chart_history["network_tx"].append({"ts": now_ts, "data": net_tx})


def get_chart_history() -> dict[str, list[dict[str, Any]]]:
    """Return chart-friendly time-series (kubectl fallback data)."""
    with _chart_lock:
        return {k: list(v) for k, v in _chart_history.items()}


def _run_kubectl_capture(*args: str) -> tuple[bool, str]:
    """Run kubectl against the platform's default kubeconfig, return (ok, stdout)."""
    from pathlib import Path

    kubeconfig = BASE_DIR / "k3s" / "kubeconfig.yaml"
    home_kubeconfig = Path.home() / ".kube" / "config"
    cmd = ["kubectl"]
    if kubeconfig.exists():
        cmd.extend(["--kubeconfig", str(kubeconfig)])
    elif home_kubeconfig.exists():
        cmd.extend(["--kubeconfig", str(home_kubeconfig)])
    cmd.extend(args)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
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
# SSH-based fallback — collect node metrics directly when kubectl is down
# ---------------------------------------------------------------------------

def _ssh_run(host: str, user: str, key_path: str, port: int, command: str, timeout: int = 10) -> tuple[bool, str]:
    """Run a command via SSH, return (ok, stdout)."""
    cmd = [
        "ssh",
        "-i", str(Path(key_path).expanduser()),
        "-p", str(port),
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=5",
        "-o", "BatchMode=yes",
        f"{user}@{host}",
        command,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return result.returncode == 0, result.stdout.strip()
    except Exception:
        return False, ""


def _ssh_cpu_memory(host: str, user: str, key_path: str, port: int) -> tuple[float, float, float, str]:
    """Single SSH call: return (cpu%, mem%, mem_bytes, hostname)."""
    cmd = (
        "hostname; "
        "awk '{u=$2+$4; t=$2+$3+$4+$5+$6+$7+$8+$9+$10; "
        "printf \"cpu %.1f\\n\", u/t*100}' /proc/stat | head -1; "
        "awk '/MemTotal/{t=$2} /MemAvailable/{a=$2} "
        "END{printf \"mem %.1f %d\\n\", (1-a/t)*100, t*1024}' /proc/meminfo"
    )
    ok, out = _ssh_run(host, user, key_path, port, cmd)
    if not ok or not out:
        return 0.0, 0.0, 0.0, ""

    lines = out.strip().splitlines()
    if len(lines) < 3:
        return 0.0, 0.0, 0.0, ""

    hostname_out = lines[0].strip()
    cpu_pct = 0.0
    mem_pct = 0.0
    mem_bytes = 0.0

    for line in lines[1:]:
        parts = line.split()
        if len(parts) < 2:
            continue
        if parts[0] == "cpu":
            try:
                cpu_pct = round(float(parts[1]), 1)
            except ValueError:
                pass
        elif parts[0] == "mem":
            try:
                mem_pct = round(float(parts[1]), 1)
                mem_bytes = float(parts[2])
            except (ValueError, IndexError):
                pass

    return cpu_pct, mem_pct, mem_bytes, hostname_out


def node_metrics_via_ssh() -> list[dict[str, Any]]:
    """Collect CPU/memory from all servers via SSH (single call per node)."""
    servers_file = BASE_DIR / "app" / "data" / "servers.json"
    if not servers_file.exists():
        return []

    try:
        servers = json.loads(servers_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []

    nodes: list[dict[str, Any]] = []
    for srv in servers:
        ip = srv.get("ip", "").strip()
        user = srv.get("ssh_user", "").strip()
        key_path = srv.get("ssh_key_path", "").strip()
        port = srv.get("ssh_port", 22)
        name = srv.get("name", ip)

        if not ip or not user or not key_path:
            continue

        cpu_pct, mem_pct, mem_bytes, hostname = _ssh_cpu_memory(ip, user, key_path, port)
        if not hostname:
            hostname = name

        nodes.append({
            "name": hostname,
            "cpu_cores": 0,
            "cpu_percent": cpu_pct,
            "memory_bytes": mem_bytes,
            "memory_percent": mem_pct,
        })

    return nodes


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