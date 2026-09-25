"""REST Control Plane API — ``/api/v1/*``.

Reuses the existing service layer so the API stays a thin, authenticated facade
over the same code paths the web UI uses. Authentication:

- Project-scoped API tokens stored as hashes, constrained by RBAC scope.
- Legacy ``PLATFORM_API_TOKEN`` is available only when explicitly enabled
  (disabled by default in production) and should be retired after migration.
- Otherwise the Flask-Login session (used by the browser UI).

Bearer-authenticated mutations are exempt from CSRF. Session-authenticated API
mutations require ``X-CSRF-Token`` like Web forms.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from datetime import datetime, timezone

from flask import Blueprint, Response, abort, current_app, g, jsonify, request
from flask_login import current_user

api_bp = Blueprint("api", __name__, url_prefix="/api/v1")

TERMINAL_PIPELINE_STATUSES = {"Success", "Failed", "Interrupted", "DevelopmentFallback"}


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ok(data, status: int = 200):
    return jsonify({
        "data": data,
        "error": None,
        "meta": {"request_id": getattr(g, "request_id", "")},
    }), status


def _error(code: str, message: str, status: int, details=None):
    error = {"code": code, "message": message}
    if details is not None:
        error["details"] = details
    return jsonify({
        "data": None,
        "error": error,
        "meta": {"request_id": getattr(g, "request_id", "")},
    }), status


def _token_principal() -> bool:
    """Return True only for the legacy environment bearer token."""
    if not current_app.config.get("ALLOW_LEGACY_API_TOKEN", True):
        return False
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        return False
    expected = os.getenv("PLATFORM_API_TOKEN", "")
    if not expected:
        return False
    supplied = header[len("Bearer "):].strip()
    return secrets.compare_digest(supplied, expected)


class _ApiPrincipal:
    """Synthetic admin principal used for bearer-token (machine) requests."""

    role = "Admin"
    is_authenticated = True
    is_admin = True
    id = None
    username = "api-token"


class _InvalidApiPrincipal:
    is_authenticated = False
    is_admin = False


def _request_ip() -> str:
    # ProxyFix normalizes this only when TRUST_PROXY_COUNT is configured.
    return request.remote_addr or ""


@api_bp.before_request
def authenticate_bearer_token():
    """Resolve a bearer credential once and attach its constrained principal."""
    header = request.headers.get("Authorization", "")
    if not header:
        return None
    if not header.startswith("Bearer "):
        g.api_auth_failed = True
        return None
    if _token_principal():
        principal = _ApiPrincipal()
        g.api_principal = principal
        from app.modules.audit.service import record_audit

        record_audit(
            "API_TOKEN_LEGACY_USE",
            "PLATFORM_API_TOKEN",
            "SUCCESS",
            "Legacy platform API token được sử dụng.",
            user=principal,
            metadata={"endpoint": request.endpoint},
        )
        return None

    raw_token = header[len("Bearer "):].strip()
    from app.modules.api_tokens.service import (
        ApiTokenRateLimitExceeded,
        authenticate_api_token,
    )

    try:
        principal = authenticate_api_token(raw_token, _request_ip())
    except ApiTokenRateLimitExceeded as exc:
        response, status = _error(
            "RATE_LIMIT_EXCEEDED",
            str(exc),
            429,
            {"retry_after_seconds": exc.retry_after},
        )
        response.headers["Retry-After"] = str(exc.retry_after)
        return response, status
    if principal is None:
        g.api_auth_failed = True
        return None
    g.api_principal = principal
    from app.modules.audit.service import record_audit

    record_audit(
        "API_TOKEN_USE",
        str(principal.token_id),
        "SUCCESS",
        f"API token {principal.token_name} được sử dụng.",
        user=principal,
        metadata={
            "project_id": principal.token_project_id,
            "token_id": principal.token_id,
            "endpoint": request.endpoint,
        },
    )
    return None


def _principal():
    """Return the effective principal for scope checks.

    Bearer-token requests carry no Flask-Login session, so ``current_user`` is
    anonymous; substituting an admin principal lets token clients pass the
    ownership/role checks that the web UI relies on.
    """
    if getattr(g, "api_auth_failed", False):
        return _InvalidApiPrincipal()
    return getattr(g, "api_principal", current_user)


def _is_authenticated() -> bool:
    return bool(getattr(_principal(), "is_authenticated", False))


def _is_admin() -> bool:
    principal = _principal()
    return bool(
        getattr(principal, "is_authenticated", False)
        and getattr(principal, "is_admin", False)
    )


def _require_auth():
    if not _is_authenticated():
        abort(401, description="Yêu cầu đăng nhập hoặc API token hợp lệ.")


def _require_admin():
    _require_auth()
    if not _is_admin():
        abort(403, description="Cần quyền Admin.")


def _application_project_id(application: dict, principal) -> int | None:
    project_id = application.get("project_id")
    if project_id is not None:
        return int(project_id)
    # Compatibility for isolated pre-A.4 test records. Runtime migration 0005
    # guarantees project_id on every persisted application.
    from app.modules.projects.service import get_active_project

    project = get_active_project(principal)
    return project.id if project else None


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
# projects
# ---------------------------------------------------------------------------

@api_bp.get("/projects")
def api_projects():
    _require_auth()
    from app.modules.projects.service import list_accessible_projects, project_to_dict

    principal = _principal()
    return _ok([
        project_to_dict(project, principal)
        for project in list_accessible_projects(principal)
    ])


@api_bp.get("/projects/<int:project_id>/subscription")
def api_project_subscription(project_id: int):
    _require_auth()
    from app.models import Project
    from app.modules.projects.service import can_access_project
    from app.modules.subscriptions.service import (
        get_project_subscription,
        subscription_limits,
    )

    principal = _principal()
    if not can_access_project(principal, project_id):
        return _error("NOT_FOUND", "Project không tồn tại.", 404)
    project = Project.query.filter_by(id=project_id, status=Project.STATUS_ACTIVE).first()
    if project is None:
        return _error("NOT_FOUND", "Project không tồn tại.", 404)
    subscription = get_project_subscription(project)
    return _ok({
        "project_id": project.id,
        "plan": {
            "key": subscription.plan.key,
            "name": subscription.plan.name,
        },
        "limits": subscription_limits(subscription),
        "updated_at": (
            subscription.updated_at.isoformat() if subscription.updated_at else None
        ),
    })


# ---------------------------------------------------------------------------
# applications
# ---------------------------------------------------------------------------

@api_bp.get("/applications")
def api_applications():
    _require_auth()
    from app.modules.applications.service import load_accessible_applications
    from app.modules.projects.service import can_access_project
    from app.modules.authorization.service import APPLICATION_READ, has_permission

    principal = _principal()
    project_id = request.args.get("project_id", type=int)
    if project_id is not None and not can_access_project(principal, project_id):
        return _error("FORBIDDEN", "Không có quyền truy cập project này.", 403)
    applications = load_accessible_applications(principal, project_id=project_id)
    return _ok([
        application
        for application in applications
        if has_permission(
            principal,
            APPLICATION_READ,
            _application_project_id(application, principal),
        )
    ])


@api_bp.get("/applications/<application_id>")
def api_application(application_id):
    _require_auth()
    from app.modules.applications.service import find_accessible_application

    principal = _principal()
    application = find_accessible_application(application_id, principal)
    if not application:
        return _error("APPLICATION_NOT_FOUND", "Application không tồn tại.", 404)
    from app.modules.authorization.service import APPLICATION_READ, has_permission
    if not has_permission(
        principal, APPLICATION_READ, _application_project_id(application, principal)
    ):
        return _error("FORBIDDEN", "Thiếu permission application:read.", 403)
    return _ok(application)


@api_bp.post("/applications/<application_id>/pipeline")
def api_trigger_pipeline(application_id):
    _require_auth()
    from app.modules.applications.service import find_accessible_application
    from app.modules.pipeline.engine import trigger_pipeline

    principal = _principal()
    application = find_accessible_application(application_id, principal)
    if not application:
        return _error("APPLICATION_NOT_FOUND", "Application không tồn tại.", 404)
    from app.modules.authorization.service import DEPLOYMENT_EXECUTE, has_permission
    if not has_permission(
        principal, DEPLOYMENT_EXECUTE, _application_project_id(application, principal)
    ):
        return _error("FORBIDDEN", "Thiếu permission deployment:execute.", 403)
    try:
        run = trigger_pipeline(application_id, actor=principal)
    except ValueError as exc:
        return _error("PIPELINE_CONFLICT", str(exc), 409)
    return _ok(run, 202)


@api_bp.get("/applications/<application_id>/pipeline")
def api_application_pipeline(application_id):
    _require_auth()
    from app.modules.applications.service import find_accessible_application
    from app.modules.pipeline.engine import load_pipeline_runs

    principal = _principal()
    application = find_accessible_application(application_id, principal)
    if not application:
        return _error("APPLICATION_NOT_FOUND", "Application không tồn tại.", 404)
    from app.modules.authorization.service import APPLICATION_READ, has_permission
    if not has_permission(
        principal, APPLICATION_READ, _application_project_id(application, principal)
    ):
        return _error("FORBIDDEN", "Thiếu permission application:read.", 403)
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
    if not application or not can_access_application(application, _principal()):
        return _error("FORBIDDEN", "Không có quyền truy cập pipeline này.", 403)
    principal = _principal()
    from app.modules.authorization.service import APPLICATION_READ, has_permission
    if not has_permission(
        principal, APPLICATION_READ, _application_project_id(application, principal)
    ):
        return _error("FORBIDDEN", "Thiếu permission application:read.", 403)
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
    if not application or not can_access_application(application, _principal()):
        return _error("FORBIDDEN", "Không có quyền truy cập pipeline này.", 403)
    principal = _principal()
    from app.modules.authorization.service import APPLICATION_READ, has_permission
    if not has_permission(
        principal, APPLICATION_READ, _application_project_id(application, principal)
    ):
        return _error("FORBIDDEN", "Thiếu permission application:read.", 403)

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
    from app.modules.authorization.service import MONITORING_READ, has_permission
    from app.modules.projects.service import get_active_project
    from app.ui.routes import _get_monitoring_data

    try:
        principal = _principal()
        project = get_active_project(principal)
        if not project or not has_permission(principal, MONITORING_READ, project.id):
            return _error("FORBIDDEN", "Thiếu permission monitoring:read.", 403)
        return _ok(_get_monitoring_data(principal))
    except Exception as exc:  # pragma: no cover - defensive
        return _error("MONITORING_UNAVAILABLE", str(exc), 502)
