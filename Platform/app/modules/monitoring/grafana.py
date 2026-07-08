"""Grafana API client + dashboard provisioning for the platform.

This module manages Grafana dashboards and data sources via
the Grafana HTTP API, enabling programmatic dashboard setup
from within the platform.

Usage:
    from app.modules.monitoring.grafana import GrafanaClient
    client = GrafanaClient(grafana_url="http://grafana:3000", api_key="...")
    client.ensure_datasource("prometheus", "http://prometheus:9090")
    client.import_dashboard("cluster-overview", dashboard_json)

TODO for partner:
    - Implement GrafanaClient class with:
        - __init__(grafana_url, api_key, timeout=10)
        - ensure_datasource(name: str, url: str, type: str = "prometheus") -> bool
        - import_dashboard(name: str, dashboard_json: dict) -> bool
        - list_dashboards() -> list[dict]
        - proxy_embed_url(dashboard_uid: str) -> str (iframe-friendly URL)
    - Add method to generate a read-only API key for iframe embedding
    - Wire into routes.py to serve Grafana dashboards as iframes
"""

# PLACEHOLDER — partner fills implementation below