"""Account identity and one-time email verification workflow."""
from __future__ import annotations

import hashlib
import re
import secrets
from datetime import datetime, timedelta, timezone
from html import escape

from flask import current_app

from app.db import db
from app.email_service import send_email
from app.models import EmailVerificationToken, User


EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")


class VerificationRateLimited(RuntimeError):
    def __init__(self, retry_after: int):
        self.retry_after = max(1, retry_after)
        super().__init__(f"Thử lại sau {self.retry_after} giây.")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def normalize_email(value: str) -> str:
    return value.strip().lower()


def valid_email(value: str) -> bool:
    normalized = normalize_email(value)
    return len(normalized) <= 254 and bool(EMAIL_PATTERN.fullmatch(normalized))


def hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def mask_email(email: str) -> str:
    local, domain = email.split("@", 1)
    visible = local[:1]
    return f"{visible}{'*' * max(2, len(local) - 1)}@{domain}"


def create_verification_token(user: User, request_ip: str = "") -> str:
    """Persist a hashed token and return its one-time raw representation."""
    if not user.email:
        raise ValueError("Tài khoản chưa có email.")

    now = utcnow()
    cooldown = int(current_app.config.get("EMAIL_VERIFICATION_RESEND_COOLDOWN_SECONDS", 60))
    one_hour_ago = now - timedelta(hours=1)
    latest = (
        EmailVerificationToken.query.filter_by(user_id=user.id)
        .order_by(EmailVerificationToken.created_at.desc())
        .first()
    )
    if latest:
        elapsed = (now - as_utc(latest.created_at)).total_seconds()
        if elapsed < cooldown:
            raise VerificationRateLimited(int(cooldown - elapsed) + 1)

    per_user_limit = int(current_app.config.get("EMAIL_VERIFICATION_MAX_SENDS_PER_HOUR", 5))
    recent_user_count = EmailVerificationToken.query.filter(
        EmailVerificationToken.user_id == user.id,
        EmailVerificationToken.created_at >= one_hour_ago,
    ).count()
    if recent_user_count >= per_user_limit:
        raise VerificationRateLimited(3600)

    if request_ip:
        per_ip_limit = int(current_app.config.get("EMAIL_VERIFICATION_MAX_SENDS_PER_IP_HOUR", 20))
        recent_ip_count = EmailVerificationToken.query.filter(
            EmailVerificationToken.request_ip == request_ip,
            EmailVerificationToken.created_at >= one_hour_ago,
        ).count()
        if recent_ip_count >= per_ip_limit:
            raise VerificationRateLimited(3600)

    raw_token = secrets.token_urlsafe(32)
    ttl = int(current_app.config.get("EMAIL_VERIFICATION_TOKEN_TTL_SECONDS", 3600))
    EmailVerificationToken.query.filter_by(user_id=user.id, used_at=None).update(
        {EmailVerificationToken.used_at: now}, synchronize_session=False
    )
    db.session.add(
        EmailVerificationToken(
            user_id=user.id,
            token_hash=hash_token(raw_token),
            expires_at=now + timedelta(seconds=ttl),
            request_ip=request_ip[:45],
        )
    )
    db.session.commit()
    return raw_token


def send_verification_email(user: User, raw_token: str, verification_url: str) -> tuple[bool, str]:
    ttl_minutes = int(current_app.config.get("EMAIL_VERIFICATION_TOKEN_TTL_SECONDS", 3600)) // 60
    safe_username = escape(user.username)
    safe_url = escape(verification_url, quote=True)
    text_body = (
        f"Xin chào {user.username},\n\n"
        "Hãy xác minh email để kích hoạt tài khoản CICT Platform:\n"
        f"{verification_url}\n\n"
        f"Liên kết có hiệu lực {ttl_minutes} phút và chỉ dùng được một lần.\n"
        "Nếu bạn không đăng ký tài khoản này, hãy bỏ qua email."
    )
    html_body = (
        f"<p>Xin chào <strong>{safe_username}</strong>,</p>"
        "<p>Hãy xác minh email để kích hoạt tài khoản CICT Platform.</p>"
        f'<p><a href="{safe_url}">Xác minh địa chỉ email</a></p>'
        f"<p>Liên kết có hiệu lực {ttl_minutes} phút và chỉ dùng được một lần.</p>"
    )
    return send_email(
        user.email,
        "[CICT Platform] Xác minh địa chỉ email",
        text_body,
        html_body,
    )


def verify_token(raw_token: str) -> tuple[User | None, str]:
    if not raw_token or len(raw_token) > 256:
        return None, "invalid"
    token = (
        EmailVerificationToken.query.filter_by(token_hash=hash_token(raw_token))
        .with_for_update()
        .first()
    )
    if not token:
        return None, "invalid"
    now = utcnow()
    if token.used_at is not None:
        return token.user, "used"
    if as_utc(token.expires_at) <= now:
        token.used_at = now
        db.session.commit()
        return token.user, "expired"

    user = token.user
    token.used_at = now
    user.email_verified_at = now
    if user.status == User.STATUS_PENDING:
        user.status = User.STATUS_ACTIVE
        user.status_changed_at = now
    EmailVerificationToken.query.filter(
        EmailVerificationToken.user_id == user.id,
        EmailVerificationToken.used_at.is_(None),
    ).update({EmailVerificationToken.used_at: now}, synchronize_session=False)
    db.session.commit()
    return user, "verified"
