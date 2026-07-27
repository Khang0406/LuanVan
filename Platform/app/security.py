import hmac
import secrets

from flask import abort, request, session


CSRF_EXEMPT_ENDPOINTS = {
    # These endpoints transparently proxy Grafana/Prometheus requests. Their
    # upstream APIs cannot add the platform's form token.
    "ui.monitoring_proxy_prometheus",
    "ui.monitoring_proxy_grafana",
    "ui.monitoring_grafana_static_proxy",
}


def generate_csrf_token() -> str:
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


def validate_csrf() -> None:
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return
    if request.endpoint in CSRF_EXEMPT_ENDPOINTS:
        return

    expected = session.get("_csrf_token", "")
    supplied = request.form.get("csrf_token", "") or request.headers.get("X-CSRF-Token", "")
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        abort(400, description="CSRF token không hợp lệ hoặc đã hết hạn.")
