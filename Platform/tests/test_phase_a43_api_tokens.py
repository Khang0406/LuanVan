"""Acceptance and security regression tests for project-scoped API tokens."""
from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


def test_config(root: Path):
    class TestConfig:
        TESTING = True
        SECRET_KEY = "phase-a43-test"
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{root / 'users.db'}"
        SQLALCHEMY_TRACK_MODIFICATIONS = False
        API_TOKEN_RATE_LIMIT_PER_MINUTE = 60

    return TestConfig


def application(application_id: str, project_id: int) -> dict:
    return {
        "id": application_id,
        "name": application_id,
        "project_id": project_id,
        "owner": "token-user",
        "namespace": application_id,
        "source_type": "docker",
        "docker_image": "nginx:stable-alpine",
        "services": [{
            "name": "web", "image": "nginx:stable-alpine",
            "container_port": 80, "replicas": 1, "service_type": "ClusterIP",
        }],
        "status": "Running",
        "created_at": "2026-09-21 00:00:00",
        "updated_at": "2026-09-21 00:00:00",
    }


class ProjectApiTokenTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.environment = patch.dict(os.environ, {
            "DELIVERY_DATABASE_PATH": str(self.root / "delivery.db"),
            "PLATFORM_DATA_DIR": str(self.root),
            "PLATFORM_API_TOKEN": "",
        })
        self.environment.start()

        from app import create_app
        from app.db import db
        from app.models import Project, ProjectMembership, RBACRole, User

        self.app = create_app(test_config(self.root))
        with self.app.app_context():
            user = User(
                username="token-developer",
                email="token-developer@example.com",
                role="Developer",
                status=User.STATUS_ACTIVE,
                active=True,
            )
            user.set_password("password-123")
            db.session.add(user)
            db.session.flush()
            alpha = Project(name="Token Alpha", slug="token-alpha", owner_user_id=user.id)
            beta = Project(name="Token Beta", slug="token-beta", owner_user_id=user.id)
            db.session.add_all([alpha, beta])
            db.session.flush()
            developer = RBACRole.query.filter_by(key="developer").one()
            db.session.add_all([
                ProjectMembership(project_id=alpha.id, user_id=user.id, role_id=developer.id),
                ProjectMembership(project_id=beta.id, user_id=user.id, role_id=developer.id),
            ])
            db.session.commit()
            self.user_id = user.id
            self.alpha_id = alpha.id
            self.beta_id = beta.id
            self.session_id = user.fs_uniquifier

        from app.delivery_store import replace_applications
        replace_applications([
            application("alpha-token-app", self.alpha_id),
            application("beta-token-app", self.beta_id),
        ])

    def tearDown(self):
        self.environment.stop()
        self.directory.cleanup()

    def create_token(self, scopes=("application:read",), **kwargs):
        from app.db import db
        from app.models import Project, User
        from app.modules.api_tokens.service import create_api_token

        with self.app.app_context():
            user = db.session.get(User, self.user_id)
            project = db.session.get(Project, self.alpha_id)
            token, raw = create_api_token(
                user, project, kwargs.get("name", "CI pipeline"), scopes,
                expires_in_days=kwargs.get("expires_in_days", 90),
            )
            return token.id, raw

    @staticmethod
    def authorization(raw: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {raw}"}

    def test_raw_token_is_returned_once_and_only_hash_is_persisted(self):
        token_id, raw = self.create_token()
        from app.db import db
        from app.models import ApiToken

        with self.app.app_context():
            token = db.session.get(ApiToken, token_id)
            self.assertNotEqual(token.token_hash, raw)
            self.assertEqual(len(token.token_hash), 64)
            self.assertNotIn(raw, token.scopes_json)
            self.assertTrue(raw.startswith(f"cict_{token.token_prefix}_"))

    def test_scope_and_project_are_both_enforced(self):
        _token_id, raw = self.create_token()
        client = self.app.test_client()
        readable = client.get(
            "/api/v1/applications/alpha-token-app", headers=self.authorization(raw)
        )
        denied_scope = client.post(
            "/api/v1/applications/alpha-token-app/pipeline", headers=self.authorization(raw)
        )
        denied_project = client.get(
            "/api/v1/applications/beta-token-app", headers=self.authorization(raw)
        )
        projects = client.get("/api/v1/projects", headers=self.authorization(raw))

        self.assertEqual(readable.status_code, 200)
        self.assertEqual(denied_scope.status_code, 403)
        self.assertIn(denied_project.status_code, {403, 404})
        self.assertEqual([item["id"] for item in projects.get_json()["data"]], [self.alpha_id])
        from app.modules.audit.service import load_audit_logs
        audit_logs = load_audit_logs(limit=None)
        self.assertTrue(any(item["action"] == "API_TOKEN_USE" for item in audit_logs))
        self.assertNotIn(raw, repr(audit_logs))

    def test_revoked_and_expired_tokens_return_401(self):
        revoked_id, revoked_raw = self.create_token(name="Revoked token")
        expired_id, expired_raw = self.create_token(name="Expired token")
        from app.db import db
        from app.models import ApiToken, User
        from app.modules.api_tokens.service import revoke_api_token

        with self.app.app_context():
            user = db.session.get(User, self.user_id)
            revoke_api_token(user, self.alpha_id, revoked_id)
            expired = db.session.get(ApiToken, expired_id)
            expired.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            db.session.commit()

        client = self.app.test_client()
        for raw in (revoked_raw, expired_raw):
            response = client.get("/api/v1/projects", headers=self.authorization(raw))
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.get_json()["error"]["code"], "UNAUTHORIZED")

    def test_role_demotion_immediately_reduces_existing_token(self):
        _token_id, raw = self.create_token(scopes=("application:read", "service:scale"))
        from app.db import db
        from app.models import ProjectMembership, RBACRole

        with self.app.app_context():
            membership = ProjectMembership.query.filter_by(
                user_id=self.user_id, project_id=self.alpha_id
            ).one()
            membership.role = RBACRole.query.filter_by(key="viewer").one()
            db.session.commit()

        response = self.app.test_client().get(
            "/api/v1/projects", headers=self.authorization(raw)
        )
        self.assertEqual(response.status_code, 200)
        permissions = response.get_json()["data"][0]["permissions"]
        self.assertIn("application:read", permissions)
        self.assertNotIn("service:scale", permissions)

    def test_per_token_rate_limit_returns_retry_after(self):
        token_id, raw = self.create_token()
        from app.db import db
        from app.models import ApiToken

        with self.app.app_context():
            token = db.session.get(ApiToken, token_id)
            token.rate_limit_per_minute = 1
            db.session.commit()

        client = self.app.test_client()
        first = client.get("/api/v1/projects", headers=self.authorization(raw))
        limited = client.get("/api/v1/projects", headers=self.authorization(raw))
        self.assertEqual(first.status_code, 200)
        self.assertEqual(limited.status_code, 429)
        self.assertEqual(limited.get_json()["error"]["code"], "RATE_LIMIT_EXCEEDED")
        self.assertGreaterEqual(int(limited.headers["Retry-After"]), 1)

    def test_web_creation_validates_scope_and_audits_lifecycle(self):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["_user_id"] = self.session_id
            session["_fresh"] = True
            session["active_project_id"] = self.alpha_id
        client.get(f"/projects/{self.alpha_id}/tokens")
        with client.session_transaction() as session:
            csrf = session["_csrf_token"]
        created = client.post(
            f"/projects/{self.alpha_id}/tokens",
            data={
                "csrf_token": csrf,
                "name": "Automation token",
                "expires_in_days": "30",
                "scopes": ["application:read"],
            },
        )
        self.assertEqual(created.status_code, 200)
        self.assertIn(b"cict_", created.data)

        from app.models import ApiToken
        with self.app.app_context():
            token_id = ApiToken.query.filter_by(name="Automation token").one().id
        revoked = client.post(
            f"/projects/{self.alpha_id}/tokens/{token_id}/revoke",
            data={"csrf_token": csrf},
        )
        self.assertEqual(revoked.status_code, 302)
        from app.modules.audit.service import load_audit_logs
        actions = [item["action"] for item in load_audit_logs(limit=None)]
        self.assertIn("API_TOKEN_CREATE", actions)
        self.assertIn("API_TOKEN_REVOKE", actions)


if __name__ == "__main__":
    unittest.main()
