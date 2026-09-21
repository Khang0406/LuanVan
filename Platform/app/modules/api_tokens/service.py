from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from flask import current_app

from app.db import db
from app.models import ApiToken, Project, ProjectMembership, User
from app.modules.authorization.service import PERMISSIONS, permission_keys


TOKEN_MARKER = "cict"


class ApiTokenError(ValueError):
    pass


class ApiTokenRateLimitExceeded(ApiTokenError):
    def __init__(self, retry_after: int):
        super().__init__("API token đã vượt giới hạn yêu cầu.")
        self.retry_after = max(1, retry_after)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is not None:
        return value
    return value.replace(tzinfo=timezone.utc)


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def _decode_scopes(value: str) -> frozenset[str]:
    try:
        decoded = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return frozenset()
    if not isinstance(decoded, list):
        return frozenset()
    return frozenset(item for item in decoded if isinstance(item, str) and item in PERMISSIONS)


@dataclass(frozen=True)
class ApiTokenPrincipal:
    """User identity with an additional project/scope boundary."""

    id: int
    username: str
    role: str
    token_id: int
    token_name: str
    token_project_id: int
    api_token_scopes: frozenset[str]
    platform_admin: bool = False
    is_authenticated: bool = True
    is_admin: bool = False


def create_api_token(
    user: User,
    project: Project,
    name: str,
    scopes: list[str] | tuple[str, ...] | set[str],
    *,
    expires_in_days: int = 90,
) -> tuple[ApiToken, str]:
    clean_name = name.strip()
    if not 3 <= len(clean_name) <= 100:
        raise ApiTokenError("Tên token phải có từ 3 đến 100 ký tự.")
    if expires_in_days not in {7, 30, 90, 180, 365}:
        raise ApiTokenError("Thời hạn token không hợp lệ.")

    requested = {scope.strip() for scope in scopes if scope.strip()}
    if not requested or not requested <= set(PERMISSIONS):
        raise ApiTokenError("Phải chọn ít nhất một scope hợp lệ.")
    granted = permission_keys(user, project.id)
    if not requested <= granted:
        raise ApiTokenError("Không thể cấp scope vượt quá quyền hiện tại của bạn.")

    prefix = secrets.token_hex(6)
    raw_token = f"{TOKEN_MARKER}_{prefix}_{secrets.token_urlsafe(32)}"
    model = ApiToken(
        name=clean_name,
        token_prefix=prefix,
        token_hash=_hash_token(raw_token),
        user_id=user.id,
        project_id=project.id,
        scopes_json=json.dumps(sorted(requested), separators=(",", ":")),
        expires_at=_utcnow() + timedelta(days=expires_in_days),
        rate_limit_per_minute=max(
            1, int(current_app.config.get("API_TOKEN_RATE_LIMIT_PER_MINUTE", 60))
        ),
    )
    db.session.add(model)
    db.session.commit()
    return model, raw_token


def list_user_api_tokens(user: User, project_id: int) -> list[ApiToken]:
    return (
        ApiToken.query.filter_by(user_id=user.id, project_id=project_id)
        .order_by(ApiToken.created_at.desc(), ApiToken.id.desc())
        .all()
    )


def token_scopes(token: ApiToken) -> frozenset[str]:
    return _decode_scopes(token.scopes_json)


def revoke_api_token(user: User, project_id: int, token_id: int) -> ApiToken:
    token = ApiToken.query.filter_by(
        id=token_id, user_id=user.id, project_id=project_id
    ).first()
    if token is None:
        raise ApiTokenError("API token không tồn tại.")
    if token.revoked_at is None:
        token.revoked_at = _utcnow()
        db.session.commit()
    return token


def _token_prefix(raw_token: str) -> str | None:
    parts = raw_token.split("_", 2)
    if len(parts) != 3 or parts[0] != TOKEN_MARKER or len(parts[1]) != 12:
        return None
    return parts[1]


def authenticate_api_token(raw_token: str, request_ip: str = "") -> ApiTokenPrincipal | None:
    prefix = _token_prefix(raw_token)
    if prefix is None:
        return None
    token = (
        ApiToken.query.filter_by(token_prefix=prefix)
        .with_for_update()
        .first()
    )
    if token is None or not hmac.compare_digest(token.token_hash, _hash_token(raw_token)):
        db.session.rollback()
        return None

    now = _utcnow()
    expires_at = _aware(token.expires_at)
    if token.revoked_at is not None or expires_at is None or expires_at <= now:
        db.session.rollback()
        return None
    user = token.user
    project = token.project
    if not user or user.status != User.STATUS_ACTIVE or not user.active:
        db.session.rollback()
        return None
    if not project or project.status != Project.STATUS_ACTIVE:
        db.session.rollback()
        return None
    if not user.is_admin:
        membership = ProjectMembership.query.filter_by(
            user_id=user.id,
            project_id=project.id,
            status=ProjectMembership.STATUS_ACTIVE,
        ).first()
        if membership is None:
            db.session.rollback()
            return None

    window_start = _aware(token.rate_window_started_at)
    if window_start is None or (now - window_start).total_seconds() >= 60:
        token.rate_window_started_at = now
        token.rate_window_count = 0
        window_start = now
    if token.rate_window_count >= token.rate_limit_per_minute:
        retry_after = 60 - int((now - window_start).total_seconds())
        db.session.rollback()
        raise ApiTokenRateLimitExceeded(retry_after)

    token.rate_window_count += 1
    token.last_used_at = now
    token.last_used_ip = request_ip[:45]
    scopes = token_scopes(token)
    principal = ApiTokenPrincipal(
        id=user.id,
        username=user.username,
        role=user.role,
        token_id=token.id,
        token_name=token.name,
        token_project_id=token.project_id,
        api_token_scopes=scopes,
        platform_admin=user.is_admin,
    )
    db.session.commit()
    return principal
