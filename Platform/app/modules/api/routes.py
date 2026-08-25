"""REST Control Plane API — ``/api/v1/*``.

Reuses the existing service layer so the API stays a thin, authenticated facade
over the same code paths the web UI uses. Authentication:

- ``Authorization: Bearer <PLATFORM_API_TOKEN>`` for machine clients (token is
  treated as an admin principal). Disabled when ``PLATFORM_API_TOKEN`` is unset.
- Otherwise the Flask-Login session (used by the browser UI).

Mutating endpoints are exempt from CSRF because the API is not form-driven; all
endpoints still perform server-side authorization/scope checks.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from datetime import datetime, timezone

from flask import Blueprint, Response, abort, jsonify, request
from flask_login import current_user

api_bp = Blueprint("api", __name__, url_prefix="/api/v1")

TERMINAL_PIPELINE_STATUSES = {"Success", "Failed", "Interrupted", "DevelopmentFallback"}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ok(data, status: int = 200):
    return jsonify({"data": data, "error": None}), status


def _error(code: str, message: str, status: int):
    return jsonify({"data": None, "error": {"code": code, "message": message}}), status


def _token_principal() -> bool:
    """Return True when the request carries a valid API bearer token."""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return False
    expected = os.getenv("PLATFORM_API_TOKEN", "")
    if not expected:
        return False
    supplied = header[len("Bearer "):].strip()
    return secrets.compare_digest(supplied, expected)


def _is_authenticated() -> bool:
    return _token_principal() or current_user.is_authenticated


def _is_admin() -> bool:
    if _token_principal():
        return True
    return current_user.is_authenticated and getattr(current_user, "role", "") == "Admin"


def _require_auth():
    if not _is_authenticated():
        abort(401, description="Yêu cầu đăng nhập hoặc API token hợp lệ.")


def _require_admin():
    _require_auth()
    if not _is_admin():
        abort(403, description="Cần quyền Admin.")


@api_bp.errorhandler(401)
def _handle_unauthorized(error):
    return _error("UNAUTHORIZED", getattr(error, "description", "Unauthorized"), 401)


@api_bp.errorhandler(403)
def _handle_forbidden(error):
    return _error("FORBIDDEN", getattr(error, "description", "Forbidden"), 403)


@api_bp.errorhandler(404)
def _handle_not_found(error):
    return _error("NOT_FOUND", getattr(error, "description", "Not found"), 404)


@api_bp.get("/health")
def api_health():
    """Liveness for the control-plane API (no auth)."""
    return _ok({"status": "ok", "ts": _now()})


# ---------------------------------------------------------------------------
# servers
# ---------------------------------------------------------------------------

@api_bp.get("/servers")
def api_servers():
    _require_admin()
    from app.modules.servers.service import load_servers

    return _ok(load_servers())


# ---------------------------------------------------------------------------
# applications
# ---------------------------------------------------------------------------

@api_bp.get("/applications")
def api_applications():
    _require_auth()
    from app.modules.applications.service import load_accessible_applications

    return _ok(load_accessible_applications(current_user))


@api_bp.get("/applications/<application_id>")
def api_application(application_id):
    _require_auth()
    from app.modules.applications.service import find_accessible_application

    application = find_accessible_application(application_id, current_user)
    if not application:
        return _error("APPLICATION_NOT_FOUND", "Application không tồn tại.", 404)
    return _ok(application)


@api_bp.post("/applications/<application_id>/pipeline")
def api_trigger_pipeline(application_id):
    _require_auth()
    from app.modules.applications.service import find_accessible_application
    from app.modules.pipeline.engine import trigger_pipeline

    application = find_accessible_application(application_id, current_user)
    if not application:
        return _error("APPLICATION_NOT_FOUND", "Application không tồn tại.", 404)
    if getattr(current_user, "role", "") == "Viewer" and not _token_principal():
        return _error("FORBIDDEN", "Viewer không thể trigger pipeline.", 403)
    try:
        run = trigger_pipeline(application_id, actor=current_user)
    except ValueError as exc:
        return _error("PIPELINE_CONFLICT", str(exc), 409)
    return _ok(run, 202)


@api_bp.get("/applications/<application_id>/pipeline")
def api_application_pipeline(application_id):
    _require_auth()
    from app.modules.applications.service import find_accessible_application
    from app.modules.pipeline.engine import load_pipeline_runs

    if not find_accessible_application(application_id, current_user):
        return _error("APPLICATION_NOT_FOUND", "Application không tồn tại.", 404)
    return _ok(load_pipeline_runs(application_id))


# ---------------------------------------------------------------------------
# pipeline
# ---------------------------------------------------------------------------

@api_bp.get("/pipeline/<run_id>")
def api_pipeline_run(run_id):
    _require_auth()
    from app.delivery_store import get_pipeline_run

    run = get_pipeline_run(run_id)
    if not run:
        return _error("PIPELINE_NOT_FOUND", "Pipeline run không tồn tại.", 404)
    from app.modules.applications.service import can_access_application

    from app.modules.applications.service import find_application

    application = find_application(run.get("application_id", ""))
    if not application or not can_access_application(application, current_user):
        return _error("FORBIDDEN", "Không có quyền truy cập pipeline này.", 403)
    return _ok(run)


@api_bp.get("/pipeline/<run_id>/events")
def api_pipeline_events(run_id):
    """Server-Sent Events stream for one pipeline run (see design doc §9)."""
    _require_auth()
    from app.delivery_store import get_pipeline_run
    from app.modules.applications.service import can_access_application, find_application

    run = get_pipeline_run(run_id)
    if not run:
        return _error("PIPELINE_NOT_FOUND", "Pipeline run không tồn tại.", 404)
    application = find_application(run.get("application_id", ""))
    if not application or not can_access_application(application, current_user):
        return _error("FORBIDDEN", "Không có quyền truy cập pipeline này.", 403)

    application_id = run.get("application_id", "")

    def _sse(payload: dict) -> str:
        return "data: " + json.dumps(payload, ensure_ascii=False, default=str) + "\n\n"

    def generate():
        from app.redis_client import is_available, subscribe

        if is_available():
            # Real-time push from the Celery worker via Redis Pub/Sub.
            for message in subscribe(f"platform:pipeline:{run_id}"):
                event = message.get("event") if isinstance(message, dict) else ""
                yield _sse(message)
                if event == "pipeline.status" and message.get("status") in TERMINAL_PIPELINE_STATUSES:
                    break
            return

        # Fallback: poll the durable store while Redis is unavailable.
        last_signature = None
        for _ in range(900):  # up to ~15 minutes at 1s
            current = get_pipeline_run(run_id)
            if not current:
                yield _sse({"event": "pipeline.status", "status": "Interrupted", "ts": _now()})
                break
            stages = current.get("stages", [])
            signature = json.dumps(stages, ensure_ascii=False, default=str)
            if signature != last_signature:
                last_signature = signature
                yield _sse({
                    "event": "pipeline.status",
                    "pipeline_run_id": run_id,
                    "application_id": application_id,
                    "status": current.get("status"),
                    "stages": stages,
                    "ts": _now(),
                })
                if current.get("status") in TERMINAL_PIPELINE_STATUSES:
                    break
            time.sleep(1)

    headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }
    return Response(
        generate(),
        mimetype="text/event-stream",
        headers=headers,
    )


# ---------------------------------------------------------------------------
# monitoring
# ---------------------------------------------------------------------------

@api_bp.get("/monitoring/metrics")
def api_monitoring_metrics():
    _require_auth()
    from app.ui.routes import _get_monitoring_data

    try:
        return _ok(_get_monitoring_data())
    except Exception as exc:  # pragma: no cover - defensive
        return _error("MONITORING_UNAVAILABLE", str(exc), 502)
