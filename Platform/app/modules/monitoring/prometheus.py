"""Prometheus HTTP API client — query metrics from Prometheus server.

This module replaces direct kubectl calls with PromQL queries for
richer metrics (CPU, memory, disk, network, application SLI).

Usage:
    from app.modules.monitoring.prometheus import PrometheusClient, get_prometheus_client
    client = get_prometheus_client()
    cpu = client.instant_query("avg(rate(node_cpu_seconds_total[5m])) * 100")
"""

from __future__ import annotations

import functools
import json
import time
from pathlib import Path
from typing import Any

import requests

from app.config import BASE_DIR, Config

# ---------------------------------------------------------------------------
# cache helper — simple TTL cache for query results
# ---------------------------------------------------------------------------


def _ttl_cache(ttl_seconds: float = 15.0):
    """Decorator that caches a function result for *ttl_seconds* seconds."""
    def decorator(func):  # noqa: D401
        cache: dict[str, Any] = {}

        @functools.wraps(func)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            key = (args, tuple(sorted(kwargs.items())))
            now = time.monotonic()
            if key in cache:
                val, timestamp = cache[key]
                if now - timestamp < ttl_seconds:
                    return val
            result = func(*args, **kwargs)
            cache[key] = (result, now)
            # Cleanup old entries periodically
            if len(cache) > 200:
                cache.clear()
            return result

        wrapper.cache_clear = lambda: cache.clear()  # type: ignore[attr-defined]
        return wrapper

    return decorator


# ---------------------------------------------------------------------------
# PrometheusClient
# ---------------------------------------------------------------------------


class PrometheusClient:
    """Thin wrapper around the Prometheus HTTP API v1.

    Parameters
    ----------
    prometheus_url : str
        Base URL of the Prometheus server (e.g. ``http://prometheus:9090``).
    timeout : int
        Seconds before requests are aborted.
    """

    def __init__(
        self,
        prometheus_url: str = "",
        timeout: int = 5,
    ) -> None:
        self.base_url = (prometheus_url or Config.PROMETHEUS_URL).rstrip("/")
        self.timeout = timeout
        self._session = requests.Session()
        self._session.headers["Accept"] = "application/json"
        self._health_ok: bool | None = None
        self._health_ts: float = 0.0

    # ------------------------------------------------------------------
    # low-level HTTP helpers
    # ------------------------------------------------------------------

    def _get(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        try:
            resp = self._session.get(url, params=params, timeout=self.timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException:
            return {"status": "error", "data": {"result": []}}

    def health_check(self) -> bool:
        """``GET /-/healthy`` → True if Prometheus is reachable (cached 30s)."""
        now = time.monotonic()
        if self._health_ok is not None and now - self._health_ts < 30:
            return self._health_ok
        try:
            resp = self._session.get(
                f"{self.base_url}/-/healthy", timeout=self.timeout
            )
            self._health_ok = resp.status_code == 200
        except requests.RequestException:
            self._health_ok = False
        self._health_ts = now
        return self._health_ok

    def is_available(self) -> bool:
        """Alias for health_check (used by fallback logic)."""
        return self.health_check()

    # ------------------------------------------------------------------
    # query API
    # ------------------------------------------------------------------

    @_ttl_cache(ttl_seconds=15)
    def instant_query(self, query: str) -> list[dict[str, Any]]:
        """``GET /api/v1/query?query=<promql>`` → list of result dicts.

        Each dict contains ``metric`` labels + a ``value`` list ``[ts, val]``.
        """
        data = self._get("/api/v1/query", params={"query": query})
        if data.get("status") != "success":
            return []
        result = data.get("data", {}).get("result", [])
        if not isinstance(result, list):
            return []
        return result

    def range_query(
        self,
        query: str,
        start: str,
        end: str,
        step: str = "1m",
    ) -> list[dict[str, Any]]:
        """``GET /api/v1/query_range`` → time-series data for charts.

        ``start`` / ``end`` should be Unix timestamps (seconds) or RFC3339 strings.
        """
        data = self._get(
            "/api/v1/query_range",
            params={"query": query, "start": start, "end": end, "step": step},
        )
        if data.get("status") != "success":
            return []
        result = data.get("data", {}).get("result", [])
        if not isinstance(result, list):
            return []
        return result

    # ------------------------------------------------------------------
    # high-level helpers — ported from collector.py
    # ------------------------------------------------------------------

    def node_cpu_percent(self) -> list[dict[str, Any]]:
        """Per-node CPU usage % (node_exporter primary, cAdvisor fallback)."""
        result = self.instant_query(
            'avg(rate(node_cpu_seconds_total{mode!="idle"}[5m])) by (instance) * 100'
        )
        if not result:
            result = self.instant_query(
                'avg(rate(container_cpu_usage_seconds_total{id!="/",job="kubernetes-cadvisor"}[5m])) by (instance) * 100'
            )
        return result

    def node_memory_percent(self) -> list[dict[str, Any]]:
        """Per-node RAM usage % (node_exporter primary, cAdvisor fallback)."""
        result = self.instant_query(
            "(1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100"
        )
        if not result:
            result = self.instant_query(
                '(1 - sum(container_memory_working_set_bytes{id!="/",job="kubernetes-cadvisor"}) by (instance)'
                ' / sum(machine_memory_bytes) by (instance)) * 100'
            )
        return result

    def node_disk_percent(self, mountpoint: str = "/") -> list[dict[str, Any]]:
        """Per-node disk usage % on *mountpoint*."""
        result = self.instant_query(
            f'(1 - node_filesystem_avail_bytes{{mountpoint="{mountpoint}",fstype!=""}}'
            f' / node_filesystem_size_bytes{{mountpoint="{mountpoint}",fstype!=""}}) * 100'
        )
        return result

    def network_rx_bps(self) -> list[dict[str, Any]]:
        """Per-node network receive rate in bits/sec."""
        result = self.instant_query(
            "sum(rate(node_network_receive_bytes_total[5m])) by (instance) * 8"
        )
        if not result:
            result = self.instant_query(
                "rate(container_network_receive_bytes_total[5m]) * 8"
            )
        return result

    def network_tx_bps(self) -> list[dict[str, Any]]:
        """Per-node network transmit rate in bits/sec."""
        result = self.instant_query(
            "sum(rate(node_network_transmit_bytes_total[5m])) by (instance) * 8"
        )
        if not result:
            result = self.instant_query(
                "rate(container_network_transmit_bytes_total[5m]) * 8"
            )
        return result

    def pod_ready_count(self, namespace: str = "") -> list[dict[str, Any]]:
        """Count of pods with Ready condition, grouped by namespace (kube-state-metrics fallback cAdvisor)."""
        base = 'kube_pod_status_ready{condition="true"}'
        if namespace:
            base = f'kube_pod_status_ready{{condition="true", namespace="{namespace}"}}'
        result = self.instant_query(f"count({base}) by (namespace)")
        return result

    def pod_restarts_total(self, namespace: str = "") -> list[dict[str, Any]]:
        """Sum of pod container restarts, grouped by pod."""
        base = "kube_pod_container_status_restarts_total"
        if namespace:
            base = f'kube_pod_container_status_restarts_total{{namespace="{namespace}"}}'
        result = self.instant_query(f"sum({base}) by (pod)")
        return result

    def total_nodes(self) -> int:
        """Return number of nodes seen by Prometheus."""
        result = self.instant_query("count(up{job=~'kubernetes-nodes.*'})")
        if not result:
            return 0
        try:
            return int(float(result[0]["value"][1]))
        except (IndexError, KeyError, ValueError):
            return 0

    def total_pods(self, namespace: str = "") -> int:
        """Return total number of pods via kube-state-metrics."""
        base = "kube_pod_info"
        if namespace:
            base = f'kube_pod_info{{namespace="{namespace}"}}'
        result = self.instant_query(f"count({base})")
        if not result:
            return 0
        try:
            return int(float(result[0]["value"][1]))
        except (IndexError, KeyError, ValueError):
            return 0

    def app_request_latency_p95(self, namespace: str = "") -> list[dict[str, Any]]:
        """95th percentile request latency (if app exposes histogram)."""
        base = "http_request_duration_seconds_bucket"
        if namespace:
            base = f'http_request_duration_seconds_bucket{{namespace="{namespace}"}}'
        return self.instant_query(
            f"histogram_quantile(0.95, rate({base}[5m]))"
        )


# ---------------------------------------------------------------------------
# URL resolution — auto-detect Prometheus address from active cluster
# ---------------------------------------------------------------------------

_CLUSTERS_FILE = BASE_DIR / "app" / "data" / "clusters.json"
_DEFAULT_PROMETHEUS_PORT = 30900

_cached_prometheus_url: str = ""
_cached_prometheus_ts: float = 0.0


def resolve_prometheus_url() -> str:
    """Return a reachable Prometheus URL from active clusters (newest first).

    Tries each active cluster in ``clusters.json`` (most recent first)
    and returns the URL of the first one whose ``/-/healthy`` responds.
    Results are cached for 60 seconds.

    Falls back to ``Config.PROMETHEUS_URL`` when no active cluster is reachable.
    """
    import time

    global _cached_prometheus_url, _cached_prometheus_ts
    now = time.monotonic()
    if _cached_prometheus_url and now - _cached_prometheus_ts < 60:
        return _cached_prometheus_url

    candidates: list[str] = []

    if _CLUSTERS_FILE.exists():
        try:
            clusters = json.loads(_CLUSTERS_FILE.read_text(encoding="utf-8"))
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
                        candidates.append(f"{master_ip}:{_DEFAULT_PROMETHEUS_PORT}")
                    else:
                        candidates.append(master_ip)
                else:
                    candidates.append(f"http://{master_ip}:{_DEFAULT_PROMETHEUS_PORT}")

    candidates.append(Config.PROMETHEUS_URL)

    for url in candidates:
        try:
            resp = requests.get(f"{url}/-/healthy", timeout=2)
            if resp.status_code == 200:
                _cached_prometheus_url = url
                _cached_prometheus_ts = now
                return url
        except Exception:
            continue

    _cached_prometheus_url = Config.PROMETHEUS_URL
    _cached_prometheus_ts = now
    return Config.PROMETHEUS_URL


_client: PrometheusClient | None = None


def get_prometheus_client() -> PrometheusClient:
    """Return a singleton PrometheusClient (lazy init).

    Uses ``resolve_prometheus_url()`` to auto-detect the correct
    Prometheus address from the active K3s cluster.
    """
    global _client
    if _client is None:
        url = resolve_prometheus_url()
        _client = PrometheusClient(prometheus_url=url)
    return _client


def clear_client_cache() -> None:
    """Clear the TTL caches on the default client."""
    global _client
    if _client is not None:
        # type: ignore[attr-defined]
        _client.instant_query.cache_clear()  # type: ignore[attr-defined]