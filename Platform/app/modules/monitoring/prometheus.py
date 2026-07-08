"""Prometheus HTTP API client — query metrics from Prometheus server.

This module replaces direct kubectl calls with PromQL queries for
richer metrics (CPU, memory, disk, network, application SLI).

Usage:
    from app.modules.monitoring.prometheus import PrometheusClient
    client = PrometheusClient(prometheus_url="http://prometheus:9090")
    cpu = client.instant_query("avg(rate(node_cpu_seconds_total[5m])) * 100")

TODO for partner:
    - Implement PrometheusClient class with:
        - __init__(prometheus_url, timeout=10)
        - instant_query(query: str) -> list[dict]
        - range_query(query: str, start: str, end: str, step: str) -> list[dict]
        - health_check() -> bool
    - Port metrics from collector.py to use PromQL instead of kubectl top
    - Add metrics caching layer (TTL ~15s, faster than kubectl round trip)
"""

# PLACEHOLDER — partner fills implementation below