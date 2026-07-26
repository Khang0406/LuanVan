"""Grafana API client + dashboard provisioning for the platform.

This module manages Grafana dashboards and data sources via
the Grafana HTTP API, enabling programmatic dashboard setup
and iframe embedding from within the platform.

Usage:
    from app.modules.monitoring.grafana import GrafanaClient, get_grafana_client
    client = get_grafana_client()
    client.ensure_datasource("prometheus", "http://prometheus:9090")
    proxy_url = client.proxy_embed_url("cluster-overview")
"""

from __future__ import annotations

from pathlib import Path
from time import monotonic
from typing import Any

import requests

from app.config import BASE_DIR, Config

# ---------------------------------------------------------------------------
# URL resolution — auto-detect Grafana address from active cluster
# ---------------------------------------------------------------------------

_CLUSTERS_FILE_GF = BASE_DIR / "app" / "data" / "clusters.json"
_DEFAULT_GRAFANA_PORT = 30300

_cached_grafana_url: str = ""
_cached_grafana_ts: float = 0.0


def resolve_grafana_url() -> str:
    """Return a reachable Grafana URL from active clusters (newest first).

    Tries each active cluster in ``clusters.json`` (most recent first)
    and returns the URL of the first one whose ``/api/health`` responds.
    Results are cached for 60 seconds.

    Falls back to ``Config.GRAFANA_URL`` when no active cluster is reachable.
    """
    import json
    import time

    global _cached_grafana_url, _cached_grafana_ts
    now = time.monotonic()
    if _cached_grafana_url and now - _cached_grafana_ts < 60:
        return _cached_grafana_url

    candidates: list[str] = []

    if _CLUSTERS_FILE_GF.exists():
        try:
            clusters = json.loads(_CLUSTERS_FILE_GF.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            clusters = []
        if isinstance(clusters, list):
            for cluster in reversed(clusters):
                if cluster.get("status") != "active":
                    continue
                master_ip = cluster.get("master_ip", "").strip()
                if not master_ip:
                    continue
                if master_ip.startswith("http://") or master_ip.startswith("https://"):
                    if ":" not in master_ip.split("/")[2]:
                        candidates.append(f"{master_ip}:{_DEFAULT_GRAFANA_PORT}")
                    else:
                        candidates.append(master_ip)
                else:
                    candidates.append(f"http://{master_ip}:{_DEFAULT_GRAFANA_PORT}")

    candidates.append(Config.GRAFANA_URL)

    for url in candidates:
        try:
            resp = requests.get(f"{url}/api/health", timeout=2)
            if resp.status_code == 200:
                _cached_grafana_url = url
                _cached_grafana_ts = now
                return url
        except Exception:
            continue

    _cached_grafana_url = Config.GRAFANA_URL
    _cached_grafana_ts = now
    return Config.GRAFANA_URL

# ---------------------------------------------------------------------------
# Default cluster-overview dashboard JSON (minimal, but functional)
# ---------------------------------------------------------------------------

CLUSTER_OVERVIEW_DASHBOARD = {
    "dashboard": {
        "id": None,
        "uid": "cluster-overview",
        "title": "Cluster Overview",
        "timezone": "browser",
        "refresh": "30s",
        "panels": [
            {
                "id": 1,
                "title": "Node CPU Usage %",
                "type": "timeseries",
                "gridPos": {"h": 8, "w": 12, "x": 0, "y": 0},
                "targets": [
                    {"expr": "avg(rate(node_cpu_seconds_total{mode!=\"idle\"}[5m])) by (instance) * 100", "legendFormat": "{{instance}}", "refId": "A"}
                ],
            },
            {
                "id": 2,
                "title": "Node Memory Usage %",
                "type": "timeseries",
                "gridPos": {"h": 8, "w": 12, "x": 12, "y": 0},
                "targets": [
                    {"expr": "(1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100", "legendFormat": "{{instance}}", "refId": "A"}
                ],
            },
            {
                "id": 3,
                "title": "Pods by Namespace",
                "type": "barchart",
                "gridPos": {"h": 8, "w": 12, "x": 0, "y": 8},
                "targets": [
                    {"expr": "count(kube_pod_info) by (namespace)", "legendFormat": "{{namespace}}", "refId": "A"}
                ],
            },
            {
                "id": 4,
                "title": "Node Disk Usage %",
                "type": "timeseries",
                "gridPos": {"h": 8, "w": 12, "x": 12, "y": 8},
                "targets": [
                    {"expr": "(1 - node_filesystem_avail_bytes{fstype!=\"\",mountpoint=\"/\"} / node_filesystem_size_bytes{fstype!=\"\",mountpoint=\"/\"}) * 100", "legendFormat": "{{instance}}", "refId": "A"}
                ],
            },
            {
                "id": 5,
                "title": "Network Receive (B/s)",
                "type": "timeseries",
                "gridPos": {"h": 8, "w": 12, "x": 0, "y": 16},
                "targets": [
                    {"expr": "sum(rate(node_network_receive_bytes_total[5m])) by (instance)", "legendFormat": "{{instance}} recv", "refId": "A"}
                ],
            },
            {
                "id": 6,
                "title": "Network Transmit (B/s)",
                "type": "timeseries",
                "gridPos": {"h": 8, "w": 12, "x": 12, "y": 16},
                "targets": [
                    {"expr": "sum(rate(node_network_transmit_bytes_total[5m])) by (instance)", "legendFormat": "{{instance}} send", "refId": "A"}
                ],
            },
            {
                "id": 7,
                "title": "Pod CPU Usage (millicores)",
                "type": "timeseries",
                "gridPos": {"h": 8, "w": 12, "x": 0, "y": 24},
                "targets": [
                    {"expr": "sum(rate(container_cpu_usage_seconds_total{container!=\"\"}[5m])) by (pod, namespace) * 1000", "legendFormat": "{{namespace}}/{{pod}}", "refId": "A"}
                ],
            },
            {
                "id": 8,
                "title": "Pod Memory Usage (MiB)",
                "type": "timeseries",
                "gridPos": {"h": 8, "w": 12, "x": 12, "y": 24},
                "targets": [
                    {"expr": "sum(container_memory_working_set_bytes{container!=\"\"}) by (pod, namespace) / 1048576", "legendFormat": "{{namespace}}/{{pod}}", "refId": "A"}
                ],
            },
            {
                "id": 9,
                "title": "Total Pods (trend)",
                "type": "stat",
                "gridPos": {"h": 4, "w": 6, "x": 0, "y": 32},
                "targets": [
                    {"expr": "count(kube_pod_info)", "legendFormat": "", "refId": "A"}
                ],
            },
            {
                "id": 10,
                "title": "Total Nodes",
                "type": "stat",
                "gridPos": {"h": 4, "w": 6, "x": 6, "y": 32},
                "targets": [
                    {"expr": "count(up{job=\"kubernetes-nodes\"})", "legendFormat": "", "refId": "A"}
                ],
            },
            {
                "id": 11,
                "title": "CPU Requests % of Allocatable",
                "type": "gauge",
                "gridPos": {"h": 4, "w": 6, "x": 12, "y": 32},
                "targets": [
                    {"expr": "sum(kube_pod_container_resource_requests{resource=\"cpu\"}) / sum(kube_node_status_allocatable{resource=\"cpu\"}) * 100", "legendFormat": "", "refId": "A"}
                ],
                "fieldConfig": {"defaults": {"unit": "percent", "max": 100, "min": 0}},
            },
            {
                "id": 12,
                "title": "Memory Requests % of Allocatable",
                "type": "gauge",
                "gridPos": {"h": 4, "w": 6, "x": 18, "y": 32},
                "targets": [
                    {"expr": "sum(kube_pod_container_resource_requests{resource=\"memory\"}) / sum(kube_node_status_allocatable{resource=\"memory\"}) * 100", "legendFormat": "", "refId": "A"}
                ],
                "fieldConfig": {"defaults": {"unit": "percent", "max": 100, "min": 0}},
            },
        ],
    },
    "overwrite": True,
}

# ---------------------------------------------------------------------------
# GrafanaClient
# ---------------------------------------------------------------------------


class GrafanaClient:
    """HTTP client for Grafana REST API (admin operations + iframe proxy).

    Parameters
    ----------
    grafana_url : str
        Base URL (e.g. ``http://grafana:3000``).
    api_key : str
        Grafana API key with Admin role (created manually or via provisioning).
    timeout : int
        Request timeout in seconds.
    """

    def __init__(
        self,
        grafana_url: str = "",
        api_key: str = "",
        timeout: int = 5,
    ) -> None:
        self.base_url = (grafana_url or Config.GRAFANA_URL).rstrip("/")
        self.api_key = api_key or Config.GRAFANA_API_KEY
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            }
        )
        self._health_ok: bool | None = None
        self._health_ts: float = 0.0

    # ------------------------------------------------------------------
    # low-level HTTP
    # ------------------------------------------------------------------

    def _get(self, path: str) -> dict[str, Any] | list[dict[str, Any]]:
        try:
            resp = self._session.get(
                f"{self.base_url}{path}", timeout=self.timeout
            )
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException:
            return {}

    def _post(
        self, path: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            resp = self._session.post(
                f"{self.base_url}{path}", json=body, timeout=self.timeout
            )
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException:
            return {}

    def health_check(self) -> bool:
        """``GET /api/health`` → True if Grafana is reachable (cached 30s)."""
        now = monotonic()
        if self._health_ok is not None and now - self._health_ts < 30:
            return self._health_ok
        try:
            resp = self._session.get(
                f"{self.base_url}/api/health", timeout=self.timeout
            )
            self._health_ok = resp.status_code == 200
        except requests.RequestException:
            self._health_ok = False
        self._health_ts = now
        return self._health_ok

    def is_available(self) -> bool:
        """Alias for health_check."""
        return self.health_check()

    # ------------------------------------------------------------------
    # datasource management
    # ------------------------------------------------------------------

    def ensure_datasource(
        self, name: str, url: str, datasource_type: str = "prometheus"
    ) -> bool:
        """Create or update a Grafana datasource. Returns True on success."""
        # Check if datasource already exists (by name)
        existing = self._get("/api/datasources/name/" + name)
        if isinstance(existing, dict) and existing.get("id"):
            # Update — only if URL changed
            ds_id = existing["id"]
            if existing.get("url") != url:
                result = self._put(f"/api/datasources/{ds_id}", {
                    "name": name,
                    "type": datasource_type,
                    "url": url,
                    "access": "proxy",
                    "isDefault": True,
                })
                return bool(result.get("datasource") or result.get("message"))
            return True  # already correct

        # Create new
        result = self._post("/api/datasources", {
            "name": name,
            "type": datasource_type,
            "url": url,
            "access": "proxy",
            "isDefault": True,
        })
        return bool(result.get("datasource") or result.get("id"))

    def _put(
        self, path: str, body: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            resp = self._session.put(
                f"{self.base_url}{path}", json=body, timeout=self.timeout
            )
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException:
            return {}

    # ------------------------------------------------------------------
    # dashboard management
    # ------------------------------------------------------------------

    def import_dashboard(
        self, name: str, dashboard_json: dict[str, Any]
    ) -> bool:
        """Import (create or overwrite) a dashboard, injecting Prometheus datasource."""
        # Fetch the Prometheus datasource UID
        ds_uid = None
        try:
            dses = self._get("/api/datasources")
            if isinstance(dses, list):
                for ds in dses:
                    if ds.get("type") == "prometheus":
                        ds_uid = ds["uid"]
                        break
        except Exception:
            pass

        dashboard = dashboard_json.get("dashboard", dashboard_json)
        if ds_uid:
            ds_ref = {"type": "prometheus", "uid": ds_uid}
            # Inject datasource into every panel and every target
            for panel in dashboard.get("panels", []):
                if "datasource" not in panel:
                    panel["datasource"] = ds_ref
                for target in panel.get("targets", []):
                    if "datasource" not in target:
                        target["datasource"] = ds_ref

        payload = {"dashboard": dashboard, "overwrite": True}
        result = self._post("/api/dashboards/db", payload)
        return bool(result.get("uid") or result.get("id"))

    def list_dashboards(self) -> list[dict[str, Any]]:
        """Return all dashboards visible to this API key."""
        result = self._get("/api/search?type=dash-db")
        if isinstance(result, list):
            return result
        return []

    def get_dashboard_by_uid(self, uid: str) -> dict[str, Any] | None:
        """Fetch a single dashboard by its UID."""
        result = self._get(f"/api/dashboards/uid/{uid}")
        if isinstance(result, dict) and result.get("dashboard"):
            return result["dashboard"]
        return None

    # ------------------------------------------------------------------
    # iframe embedding
    # ------------------------------------------------------------------

    def proxy_embed_url(
        self,
        dashboard_uid: str,
        org_id: int = 1,
        theme: str = "light",
        refresh: str = "30s",
        kiosk: str = "",
    ) -> str:
        """Return proxy-based iframe URL so browser reaches Grafana through Flask."""
        import time
        kiosk_param = "&kiosk" if kiosk else ""
        return f"/monitoring/proxy/grafana/d/{dashboard_uid}?orgId={org_id}&refresh={refresh}&theme={theme}{kiosk_param}&_t={int(time.time())}"

    def ensure_cluster_overview(self) -> str | None:
        """Import the built-in cluster dashboard & return its iframe URL.

        Returns None if Grafana is unreachable or import fails.
        """
        if not self.health_check():
            return None
        ok = self.import_dashboard("cluster-overview", CLUSTER_OVERVIEW_DASHBOARD)
        if not ok:
            return None
        return self.proxy_embed_url("cluster-overview")


# ---------------------------------------------------------------------------
# module-level convenience
# ---------------------------------------------------------------------------

_client: GrafanaClient | None = None


def get_grafana_client() -> GrafanaClient:
    """Return a singleton GrafanaClient (lazy init).

    Uses ``resolve_grafana_url()`` to auto-detect the correct
    Grafana address from the active K3s cluster.
    """
    global _client
    if _client is None:
        url = resolve_grafana_url()
        _client = GrafanaClient(grafana_url=url)
    return _client


# ---------------------------------------------------------------------------
# convenience helper for templates
# ---------------------------------------------------------------------------

def get_grafana_embed_url() -> str | None:
    """Return iframe-ready Grafana URL, or None if Grafana is not reachable.

    Does NOT require API key — dashboard is provisioned via ConfigMap
    at deploy time (see ``k8s_manifests.grafana_deployment()``).
    """
    client = get_grafana_client()
    if not client.is_available():
        return None
    return client.proxy_embed_url("cluster-overview")
