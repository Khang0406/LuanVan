"""Phase A.3.2 acceptance tests for account email verification."""
from __future__ import annotations

import os
import unittest
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch


def test_config(root: Path):
    class TestConfig:
        TESTING = True
        SECRET_KEY = "phase-a32-test"
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{root / 'users.db'}"
        SQLALCHEMY_TRACK_MODIFICATIONS = False
        PLATFORM_PUBLIC_URL = "https://platform.test"
        EMAIL_VERIFICATION_TOKEN_TTL_SECONDS = 3600
        EMAIL_VERIFICATION_RESEND_COOLDOWN_SECONDS = 60
        EMAIL_VERIFICATION_MAX_SENDS_PER_HOUR = 5
        EMAIL_VERIFICATION_MAX_SENDS_PER_IP_HOUR = 20
        SMTP_HOST = ""
        SMTP_FROM = ""

    return TestConfig


class EmailVerificationWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.environment = patch.dict(
            os.environ,
            {
                "DELIVERY_DATABASE_PATH": str(self.root / "delivery.db"),
                "PLATFORM_DATA_DIR": str(self.root),
            },
        )
        self.environment.start()
        from app import create_app

        self.app = create_app(test_config(self.root))
        self.client = self.app.test_client()

    def tearDown(self):
        self.environment.stop()
        self.directory.cleanup()

    def csrf(self, path: str) -> str:
        self.client.get(path)
        with self.client.session_transaction() as session:
            return session["_csrf_token"]

    def register(self, email="New.User@Example.COM", smtp_result=(True, "sent")):
        token = self.csrf("/auth/register")
        with patch(
            "app.modules.auth.routes.send_verification_email",
            return_value=smtp_result,
        ) as sender:
            response = self.client.post(
                "/auth/register",
                data={
                    "csrf_token": token,
                    "username": "new-user",
                    "email": email,
                    "password": "strong-password",
                    "confirm_password": "strong-password",
                },
            )
        return response, sender

    def test_register_verify_one_time_then_login_by_email(self):
        from app.models import EmailVerificationToken, User

        response, sender = self.register()
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Ki\xe1\xbb\x83m tra email", response.data)
        raw_token = sender.call_args.args[1]
        verification_url = sender.call_args.args[2]
        self.assertIn(raw_token, verification_url)
        self.assertTrue(verification_url.startswith("https://platform.test/"))

        with self.app.app_context():
            user = User.query.filter_by(username="new-user").one()
            stored = EmailVerificationToken.query.filter_by(user_id=user.id).one()
            self.assertEqual(user.email, "new.user@example.com")
            self.assertEqual(user.status, User.STATUS_PENDING)
            self.assertNotEqual(stored.token_hash, raw_token)
            self.assertEqual(len(stored.token_hash), 64)

        pending_csrf = self.csrf("/auth/login")
        pending_login = self.client.post(
            "/auth/login",
            data={"csrf_token": pending_csrf, "username": "new-user", "password": "strong-password"},
        )
        self.assertEqual(pending_login.status_code, 302)
        self.assertIn("/auth/resend-verification", pending_login.location)

        verified = self.client.get(f"/auth/verify-email?token={raw_token}")
        self.assertEqual(verified.status_code, 302)
        self.assertIn("/auth/login", verified.location)
        with self.app.app_context():
            user = User.query.filter_by(username="new-user").one()
            self.assertEqual(user.status, User.STATUS_ACTIVE)
            self.assertIsNotNone(user.email_verified_at)
            self.assertIsNotNone(EmailVerificationToken.query.one().used_at)

        reused = self.client.get(f"/auth/verify-email?token={raw_token}")
        self.assertIn("/auth/resend-verification", reused.location)

        login_csrf = self.csrf("/auth/login")
        logged_in = self.client.post(
            "/auth/login",
            data={
                "csrf_token": login_csrf,
                "username": "NEW.USER@EXAMPLE.COM",
                "password": "strong-password",
            },
        )
        self.assertEqual(logged_in.status_code, 302)
        self.assertTrue(logged_in.location.endswith("/dashboard"))

    def test_smtp_failure_preserves_pending_account_and_token(self):
        from app.models import EmailVerificationToken, User

        response, _sender = self.register(smtp_result=(False, "smtp unavailable"))
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"T\xc3\xa0i kho\xe1\xba\xa3n \xc4\x91\xc3\xa3 \xc4\x91\xc6\xb0\xe1\xbb\xa3c t\xe1\xba\xa1o", response.data)
        with self.app.app_context():
            self.assertEqual(User.query.filter_by(username="new-user").one().status, User.STATUS_PENDING)
            self.assertEqual(EmailVerificationToken.query.count(), 1)

    def test_expired_token_cannot_activate_account(self):
        from app.db import db
        from app.models import EmailVerificationToken, User
        from app.modules.auth.service import utcnow

        _response, sender = self.register()
        raw_token = sender.call_args.args[1]
        with self.app.app_context():
            token = EmailVerificationToken.query.one()
            token.expires_at = utcnow() - timedelta(seconds=1)
            db.session.commit()
        response = self.client.get(f"/auth/verify-email?token={raw_token}")
        self.assertIn("/auth/resend-verification", response.location)
        with self.app.app_context():
            self.assertEqual(User.query.filter_by(username="new-user").one().status, User.STATUS_PENDING)

    def test_resend_is_rate_limited_and_unknown_email_response_is_neutral(self):
        from app.models import EmailVerificationToken

        self.register()
        csrf = self.csrf("/auth/resend-verification")
        with patch("app.modules.auth.routes.send_verification_email") as sender:
            response = self.client.post(
                "/auth/resend-verification",
                data={"csrf_token": csrf, "email": "new.user@example.com"},
                follow_redirects=True,
            )
        self.assertEqual(response.status_code, 200)
        sender.assert_not_called()
        with self.app.app_context():
            self.assertEqual(EmailVerificationToken.query.count(), 1)

        csrf = self.csrf("/auth/resend-verification")
        unknown = self.client.post(
            "/auth/resend-verification",
            data={"csrf_token": csrf, "email": "missing@example.com"},
            follow_redirects=True,
        )
        self.assertIn(b"N\xe1\xba\xbfu email thu\xe1\xbb\x99c t\xc3\xa0i kho\xe1\xba\xa3n", unknown.data)

    def test_locked_or_disabled_account_cannot_login(self):
        from app.db import db
        from app.models import User

        with self.app.app_context():
            user = User(username="locked-user", email="locked@example.com", status=User.STATUS_LOCKED)
            user.set_password("strong-password")
            db.session.add(user)
            db.session.commit()
            user_id = user.id
        token = self.csrf("/auth/login")
        response = self.client.post(
            "/auth/login",
            data={"csrf_token": token, "username": "locked-user", "password": "strong-password"},
        )
        self.assertEqual(response.status_code, 403)

        with self.client.session_transaction() as session:
            session["_user_id"] = str(user_id)
            session["_fresh"] = True
        rejected_session = self.client.get("/dashboard")
        self.assertEqual(rejected_session.status_code, 302)
        self.assertIn("/auth/login", rejected_session.location)


class TransactionalEmailTests(unittest.TestCase):
    def test_production_requires_https_public_url_and_smtp(self):
        from app.config import Config

        with patch.object(Config, "PLATFORM_ENV", "production"), \
             patch.object(Config, "SECRET_KEY", "strong-secret"), \
             patch.object(Config, "SESSION_COOKIE_SECURE", True), \
             patch.object(Config, "REMEMBER_COOKIE_SECURE", True), \
             patch.object(Config, "SQLALCHEMY_DATABASE_URI", "postgresql://db/platform"), \
             patch.object(Config, "PLATFORM_PUBLIC_URL", "http://platform.example"), \
             patch.dict(os.environ, {"REDIS_URL": "redis://redis/0"}):
            with self.assertRaisesRegex(RuntimeError, "HTTPS"):
                Config.validate_production()

        with patch.object(Config, "PLATFORM_ENV", "production"), \
             patch.object(Config, "SECRET_KEY", "strong-secret"), \
             patch.object(Config, "SESSION_COOKIE_SECURE", True), \
             patch.object(Config, "REMEMBER_COOKIE_SECURE", True), \
             patch.object(Config, "SQLALCHEMY_DATABASE_URI", "postgresql://db/platform"), \
             patch.object(Config, "PLATFORM_PUBLIC_URL", "https://platform.example"), \
             patch.object(Config, "SMTP_HOST", ""), \
             patch.object(Config, "SMTP_FROM", ""), \
             patch.dict(os.environ, {"REDIS_URL": "redis://redis/0"}):
            with self.assertRaisesRegex(RuntimeError, "SMTP_HOST"):
                Config.validate_production()

    def test_smtp_uses_tls_login_and_keeps_password_out_of_message(self):
        from flask import Flask
        from app.email_service import send_email

        app = Flask(__name__)
        app.config.update(
            SMTP_HOST="smtp.example",
            SMTP_PORT=587,
            SMTP_FROM="platform@example.com",
            SMTP_USERNAME="mailer",
            SMTP_PASSWORD="smtp-secret-value",
            SMTP_STARTTLS=True,
            SMTP_SSL=False,
            SMTP_TIMEOUT=5,
        )
        smtp = MagicMock()
        smtp.return_value.__enter__.return_value = smtp.return_value
        with app.app_context(), patch("app.email_service.smtplib.SMTP", smtp):
            success, _reason = send_email(
                "user@example.com", "Verify", "https://platform.test/verify?token=abc"
            )
        self.assertTrue(success)
        client = smtp.return_value
        client.starttls.assert_called_once()
        client.login.assert_called_once_with("mailer", "smtp-secret-value")
        message = client.send_message.call_args.args[0]
        self.assertEqual(message["To"], "user@example.com")
        self.assertNotIn("smtp-secret-value", message.as_string())


if __name__ == "__main__":
    unittest.main()
