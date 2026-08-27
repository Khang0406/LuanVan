"""SMTP transport for account and other transactional email."""
from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage

from flask import current_app


def smtp_configured() -> bool:
    return bool(
        current_app.config.get("SMTP_HOST")
        and current_app.config.get("SMTP_FROM")
    )


def send_email(
    recipient: str,
    subject: str,
    text_body: str,
    html_body: str | None = None,
) -> tuple[bool, str]:
    """Send a message without exposing connection details or credentials."""
    if not smtp_configured():
        return False, "SMTP chưa được cấu hình."

    message = EmailMessage()
    message["From"] = current_app.config["SMTP_FROM"]
    message["To"] = recipient
    message["Subject"] = subject
    message.set_content(text_body)
    if html_body:
        message.add_alternative(html_body, subtype="html")

    use_ssl = bool(current_app.config.get("SMTP_SSL"))
    client_class = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
    try:
        with client_class(
            current_app.config["SMTP_HOST"],
            int(current_app.config.get("SMTP_PORT", 587)),
            timeout=int(current_app.config.get("SMTP_TIMEOUT", 10)),
        ) as client:
            if not use_ssl and current_app.config.get("SMTP_STARTTLS", True):
                client.starttls(context=ssl.create_default_context())
            username = current_app.config.get("SMTP_USERNAME", "")
            if username:
                client.login(username, current_app.config.get("SMTP_PASSWORD", ""))
            client.send_message(message)
        return True, "Email đã được gửi."
    except (OSError, smtplib.SMTPException, ValueError):
        current_app.logger.warning(
            "Transactional SMTP delivery failed",
            extra={"recipient_domain": recipient.rsplit("@", 1)[-1]},
        )
        return False, "Không gửi được email; thông tin kết nối đã được ẩn."
