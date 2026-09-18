"""Phase A.3.3 acceptance tests for recovery, lockout and session revocation."""
from __future__ import annotations

import os
import unittest
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


def test_config(root: Path):
    class TestConfig:
        TESTING = True
        SECRET_KEY = "phase-a33-test"
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{root / 'users.db'}"
        SQLALCHEMY_TRACK_MODIFICATIONS = False
        PLATFORM_PUBLIC_URL = "https://platform.test"
        PASSWORD_RESET_TOKEN_TTL_SECONDS = 3600
        PASSWORD_RESET_COOLDOWN_SECONDS = 60
        PASSWORD_RESET_MAX_SENDS_PER_HOUR = 5
        PASSWORD_RESET_MAX_SENDS_PER_IP_HOUR = 20
        LOGIN_MAX_FAILED_ATTEMPTS = 3
        LOGIN_FAILURE_WINDOW_SECONDS = 300
        LOGIN_LOCKOUT_SECONDS = 120

    return TestConfig


class AccountSecurityWorkflowTests(unittest.TestCase):
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
        from app.db import db
        from app.models import User

        self.app = create_app(test_config(self.root))
        self.client = self.app.test_client()
        with self.app.app_context():
            user = User(
                username="member",
                email="member@example.com",
                role="Developer",
                status=User.STATUS_ACTIVE,
                active=True,
            )
            user.set_password("old-password")
            db.session.add(user)
            db.session.commit()

    def tearDown(self):
        self.environment.stop()
        self.directory.cleanup()

    def csrf(self, client, path: str) -> str:
        client.get(path)
        with client.session_transaction() as session:
            return session["_csrf_token"]

    def login(self, client, password="old-password"):
        token = self.csrf(client, "/auth/login")
        return client.post(
            "/auth/login",
            data={
                "csrf_token": token,
                "username": "member",
                "password": password,
            },
        )

    def request_reset(self, smtp_result=(True, "sent")):
        token = self.csrf(self.client, "/auth/forgot-password")
        with patch(
            "app.modules.auth.routes.send_password_reset_email",
            return_value=smtp_result,
        ) as sender:
            response = self.client.post(
                "/auth/forgot-password",
                data={"csrf_token": token, "email": "MEMBER@example.com"},
            )
        return response, sender

    def test_forgot_reset_is_one_time_and_revokes_old_password(self):
        from app.models import PasswordResetToken, User

        response, sender = self.request_reset()
        self.assertEqual(response.status_code, 302)
        raw_token = sender.call_args.args[1]
        reset_url = sender.call_args.args[2]
        self.assertIn(raw_token, reset_url)
        self.assertTrue(reset_url.startswith("https://platform.test/"))

        with self.app.app_context():
            user = User.query.filter_by(username="member").one()
            stored = PasswordResetToken.query.filter_by(user_id=user.id).one()
            old_session_identity = user.fs_uniquifier
            self.assertEqual(len(stored.token_hash), 64)
            self.assertNotEqual(stored.token_hash, raw_token)

        page = self.client.get(f"/auth/reset-password?token={raw_token}")
        self.assertEqual(page.status_code, 200)
        with self.client.session_transaction() as session:
            csrf = session["_csrf_token"]
        changed = self.client.post(
            "/auth/reset-password",
            data={
                "csrf_token": csrf,
                "token": raw_token,
                "new_password": "new-strong-password",
                "confirm_password": "new-strong-password",
            },
        )
        self.assertEqual(changed.status_code, 302)
        self.assertIn("/auth/login", changed.location)

        with self.app.app_context():
            user = User.query.filter_by(username="member").one()
            self.assertNotEqual(user.fs_uniquifier, old_session_identity)
            self.assertTrue(user.check_password("new-strong-password"))
            self.assertFalse(user.check_password("old-password"))
            self.assertIsNotNone(PasswordResetToken.query.one().used_at)

        reused = self.client.get(f"/auth/reset-password?token={raw_token}")
        self.assertEqual(reused.status_code, 302)
        self.assertIn("/auth/forgot-password", reused.location)

    def test_unknown_email_has_same_neutral_response_and_creates_no_token(self):
        from app.models import PasswordResetToken

        token = self.csrf(self.client, "/auth/forgot-password")
        response = self.client.post(
            "/auth/forgot-password",
            data={"csrf_token": token, "email": "missing@example.com"},
            follow_redirects=True,
        )
        self.assertIn(
            b"N\xe1\xba\xbfu email thu\xe1\xbb\x99c t\xc3\xa0i kho\xe1\xba\xa3n",
            response.data,
        )
        with self.app.app_context():
            self.assertEqual(PasswordResetToken.query.count(), 0)

    def test_smtp_failure_preserves_reset_token_without_exposing_error(self):
        from app.models import PasswordResetToken

        response, sender = self.request_reset((False, "smtp unavailable"))
        self.assertEqual(response.status_code, 302)
        sender.assert_called_once()
        with self.app.app_context():
            self.assertEqual(PasswordResetToken.query.count(), 1)

    def test_reset_resend_is_rate_limited_and_does_not_create_second_token(self):
        from app.models import PasswordResetToken

        self.request_reset()
        csrf = self.csrf(self.client, "/auth/forgot-password")
        with patch("app.modules.auth.routes.send_password_reset_email") as sender:
            response = self.client.post(
                "/auth/forgot-password",
                data={"csrf_token": csrf, "email": "member@example.com"},
            )
        self.assertEqual(response.status_code, 302)
        sender.assert_not_called()
        with self.app.app_context():
            self.assertEqual(PasswordResetToken.query.count(), 1)

    def test_tampered_signed_reset_token_is_rejected_even_with_matching_hash(self):
        from app.db import db
        from app.models import PasswordResetToken, User
        from app.modules.auth.service import hash_token

        _response, sender = self.request_reset()
        raw_token = sender.call_args.args[1]
        replacement = "A" if raw_token[0] != "A" else "B"
        tampered = f"{replacement}{raw_token[1:]}"
        with self.app.app_context():
            PasswordResetToken.query.one().token_hash = hash_token(tampered)
            db.session.commit()
        rejected = self.client.get(f"/auth/reset-password?token={tampered}")
        self.assertIn("/auth/forgot-password", rejected.location)
        with self.app.app_context():
            self.assertTrue(User.query.filter_by(username="member").one().check_password("old-password"))

    def test_expired_reset_token_is_rejected(self):
        from app.db import db
        from app.models import PasswordResetToken, User
        from app.modules.auth.service import utcnow

        _response, sender = self.request_reset()
        raw_token = sender.call_args.args[1]
        with self.app.app_context():
            record = PasswordResetToken.query.one()
            record.expires_at = utcnow() - timedelta(seconds=1)
            db.session.commit()
        rejected = self.client.get(f"/auth/reset-password?token={raw_token}")
        self.assertIn("/auth/forgot-password", rejected.location)
        with self.app.app_context():
            self.assertTrue(User.query.filter_by(username="member").one().check_password("old-password"))

    def test_repeated_login_failures_temporarily_lock_then_expire(self):
        from app.db import db
        from app.models import User
        from app.modules.auth.service import utcnow

        token = self.csrf(self.client, "/auth/login")
        for _index in range(3):
            response = self.client.post(
                "/auth/login",
                data={"csrf_token": token, "username": "member", "password": "wrong"},
            )
            self.assertEqual(response.status_code, 200)

        blocked = self.client.post(
            "/auth/login",
            data={"csrf_token": token, "username": "member", "password": "old-password"},
        )
        self.assertEqual(blocked.status_code, 429)
        with self.app.app_context():
            user = User.query.filter_by(username="member").one()
            self.assertIsNotNone(user.locked_until)
            user.locked_until = utcnow() - timedelta(seconds=1)
            db.session.commit()

        allowed = self.client.post(
            "/auth/login",
            data={"csrf_token": token, "username": "member", "password": "old-password"},
        )
        self.assertEqual(allowed.status_code, 302)
        self.assertTrue(allowed.location.endswith("/dashboard"))
        with self.app.app_context():
            user = User.query.filter_by(username="member").one()
            self.assertEqual(user.failed_login_count, 0)
            self.assertIsNone(user.locked_until)

    def test_change_password_revokes_every_existing_session(self):
        first = self.app.test_client()
        second = self.app.test_client()
        self.assertEqual(self.login(first).status_code, 302)
        self.assertEqual(self.login(second).status_code, 302)

        csrf = self.csrf(first, "/auth/change-password")
        changed = first.post(
            "/auth/change-password",
            data={
                "csrf_token": csrf,
                "current_password": "old-password",
                "new_password": "changed-password",
                "confirm_password": "changed-password",
            },
        )
        self.assertEqual(changed.status_code, 302)
        self.assertIn("/auth/login", changed.location)
        stale = second.get("/dashboard")
        self.assertEqual(stale.status_code, 302)
        self.assertIn("/auth/login", stale.location)


class PasswordResetEmailTests(unittest.TestCase):
    def test_flask_mailman_locmem_delivers_text_and_html_reset_email(self):
        from flask import Flask

        from app.extensions import mail
        from app.models import User
        from app.modules.auth.service import send_password_reset_email

        app = Flask(__name__)
        app.config.update(
            MAIL_BACKEND="locmem",
            MAIL_DEFAULT_SENDER="platform@example.com",
            PASSWORD_RESET_TOKEN_TTL_SECONDS=3600,
        )
        mail.init_app(app)
        user = User(username="mail-user", email="user@example.com")
        with app.app_context():
            success, _reason = send_password_reset_email(
                user,
                "raw-reset-token",
                "https://platform.test/auth/reset-password?token=raw-reset-token",
            )
            self.assertTrue(success)
            self.assertEqual(len(app.extensions["mailman"].outbox), 1)
            message = app.extensions["mailman"].outbox[0]
            self.assertEqual(message.to, ["user@example.com"])
            self.assertIn("raw-reset-token", message.body)
            self.assertEqual(len(message.alternatives), 1)


if __name__ == "__main__":
    unittest.main()
