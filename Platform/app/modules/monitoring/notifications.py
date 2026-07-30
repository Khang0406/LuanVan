"""Transition-based SMTP notifications for monitoring alerts."""

from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage
from typing import Any

from app.json_store import mask_secrets


def smtp_configured() -> bool:
    return bool(
        os.getenv("SMTP_HOST", "").strip()
        and os.getenv("SMTP_FROM", "").strip()
        and os.getenv("SMTP_TO", "").strip()
    )


def send_alert_email(alert: dict[str, Any]) -> tuple[bool, str]:
    """Send one sanitized firing/resolved transition; credentials never leave env."""
    if not smtp_configured():
        return False, "SMTP chưa được cấu hình bằng biến môi trường."

    host = os.environ["SMTP_HOST"].strip()
    port = int(os.getenv("SMTP_PORT", "587"))
    sender = os.environ["SMTP_FROM"].strip()
    recipients = [value.strip() for value in os.environ["SMTP_TO"].split(",") if value.strip()]
    state = alert.get("state", "Firing")
    safe_message = str(mask_secrets(alert.get("message", "")))

    message = EmailMessage()
    message["From"] = sender
    message["To"] = ", ".join(recipients)
    message["Subject"] = f"[Platform] {state}: {alert.get('type', 'Alert')}"
    message.set_content(
        f"State: {state}\nSeverity: {alert.get('severity', 'WARNING')}\n"
        f"Application: {alert.get('application_id', '-')}\n"
        f"Namespace: {alert.get('namespace', '-')}\nMessage: {safe_message}\n"
    )

    timeout = int(os.getenv("SMTP_TIMEOUT", "10"))
    use_ssl = os.getenv("SMTP_SSL", "").lower() in {"1", "true", "yes"}
    client_class = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
    try:
        with client_class(host, port, timeout=timeout) as client:
            if not use_ssl and os.getenv("SMTP_STARTTLS", "true").lower() in {"1", "true", "yes"}:
                client.starttls(context=ssl.create_default_context())
            username = os.getenv("SMTP_USERNAME", "")
            password = os.getenv("SMTP_PASSWORD", "")
            if username:
                client.login(username, password)
            client.send_message(message)
        return True, f"Đã gửi SMTP transition {state}."
    except (OSError, smtplib.SMTPException, ValueError):
        return False, "Không gửi được SMTP; thông tin kết nối đã được ẩn."
