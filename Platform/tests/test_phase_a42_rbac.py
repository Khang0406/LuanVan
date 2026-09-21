"""Acceptance tests for project-scoped RBAC shared by Web and API."""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


def test_config(root: Path):
    class TestConfig:
        TESTING = True
        SECRET_KEY = "phase-a42-test"
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{root / 'users.db'}"
        SQLALCHEMY_TRACK_MODIFICATIONS = False

    return TestConfig


def application(project_id: int) -> dict:
    return {
        "id": "alpha-app",
        "name": "alpha-app",
        "project_id": project_id,
        "owner": "alpha",
        "namespace": "alpha-app",
        "source_type": "docker",
        "docker_image": "nginx:stable-alpine",
        "services": [{
            "name": "web", "image": "nginx:stable-alpine",
            "container_port": 80, "replicas": 1, "service_type": "ClusterIP",
        }],
        "status": "Running",
        "created_at": "2026-09-18 00:00:00",
        "updated_at": "2026-09-18 00:00:00",
    }


class ProjectRBACTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.environment = patch.dict(os.environ, {
            "DELIVERY_DATABASE_PATH": str(self.root / "delivery.db"),
            "PLATFORM_DATA_DIR": str(self.root),
        })
        self.environment.start()

        from app import create_app
        from app.db import db
        from app.models import Project, ProjectMembership, RBACRole, User

        self.app = create_app(test_config(self.root))
        with self.app.app_context():
            users = {}
            for username in ("owner", "developer", "operator", "viewer", "auditor"):
                user = User(
                    username=f"rbac-{username}",
                    email=f"rbac-{username}@example.com",
                    role="Developer",
                    status=User.STATUS_ACTIVE,
                    active=True,
                )
                user.set_password("password-123")
                db.session.add(user)
                users[username] = user
            db.session.flush()
            alpha = Project(name="Alpha RBAC", slug="alpha-rbac", owner_user_id=users["owner"].id)
            beta = Project(name="Beta RBAC", slug="beta-rbac", owner_user_id=users["owner"].id)
            db.session.add_all([alpha, beta])
            db.session.flush()
            roles = {role.key: role for role in RBACRole.query.all()}
            assignments = {
                "owner": "project_admin",
                "developer": "developer",
                "operator": "operator",
                "viewer": "viewer",
                "auditor": "auditor",
            }
            for username, role_key in assignments.items():
                db.session.add(ProjectMembership(
                    project_id=alpha.id,
                    user_id=users[username].id,
                    role_id=roles[role_key].id,
                    invited_by_user_id=users["owner"].id,
                ))
            # One identity can be Operator in Alpha but Viewer in Beta.
            db.session.add(ProjectMembership(
                project_id=beta.id,
                user_id=users["operator"].id,
                role_id=roles["viewer"].id,
                invited_by_user_id=users["owner"].id,
            ))
            db.session.commit()
            self.project_id = alpha.id
            self.beta_id = beta.id
            self.memberships = {
                item.user.username.removeprefix("rbac-"): item.id
                for item in ProjectMembership.query.filter_by(project_id=alpha.id)
            }
            self.sessions = {name: user.fs_uniquifier for name, user in users.items()}

        from app.delivery_store import replace_applications
        replace_applications([application(self.project_id)])

    def tearDown(self):
        self.environment.stop()
        self.directory.cleanup()

    def client_for(self, username: str):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["_user_id"] = self.sessions[username]
            session["_fresh"] = True
            session["active_project_id"] = self.project_id
        return client

    @staticmethod
    def csrf(client) -> str:
        client.get("/applications")
        with client.session_transaction() as session:
            return session["_csrf_token"]

    def test_permission_matrix_and_different_role_per_project(self):
        from app.db import db
        from app.models import User
        from app.modules.authorization.service import (
            APPLICATION_CREATE, APPLICATION_READ, AUDIT_READ,
            DEPLOYMENT_EXECUTE, MEMBER_MANAGE, SERVICE_SCALE, has_permission,
        )

        with self.app.app_context():
            users = {user.username.removeprefix("rbac-"): user for user in User.query.filter(
                User.username.in_(("rbac-owner", "rbac-developer", "rbac-operator", "rbac-viewer", "rbac-auditor"))
            )}
            self.assertTrue(has_permission(users["owner"], MEMBER_MANAGE, self.project_id))
            self.assertTrue(has_permission(users["developer"], APPLICATION_CREATE, self.project_id))
            self.assertFalse(has_permission(users["developer"], MEMBER_MANAGE, self.project_id))
            self.assertTrue(has_permission(users["operator"], DEPLOYMENT_EXECUTE, self.project_id))
            self.assertTrue(has_permission(users["operator"], SERVICE_SCALE, self.project_id))
            self.assertFalse(has_permission(users["operator"], APPLICATION_CREATE, self.project_id))
            self.assertTrue(has_permission(users["operator"], APPLICATION_READ, self.beta_id))
            self.assertFalse(has_permission(users["operator"], SERVICE_SCALE, self.beta_id))
            self.assertTrue(has_permission(users["auditor"], AUDIT_READ, self.project_id))
            self.assertFalse(has_permission(users["viewer"], AUDIT_READ, self.project_id))

    def test_operator_can_scale_but_cannot_delete_or_manage_members(self):
        client = self.client_for("operator")
        csrf = self.csrf(client)
        with patch("app.ui.routes.scale_application", return_value=(True, "scaled")):
            scaled = client.post(
                "/applications/alpha-app/scale",
                data={"csrf_token": csrf, "replicas": "2"},
            )
        self.assertEqual(scaled.status_code, 302)
        denied_delete = client.post(
            "/applications/alpha-app/delete", data={"csrf_token": csrf}
        )
        self.assertEqual(denied_delete.status_code, 403)
        denied_member = client.post(
            f"/projects/{self.project_id}/members",
            data={"csrf_token": csrf, "identity": "rbac-viewer", "role": "viewer"},
        )
        self.assertEqual(denied_member.status_code, 403)

    def test_viewer_cannot_bypass_permission_through_api(self):
        client = self.client_for("viewer")
        projects = client.get("/api/v1/projects").get_json()["data"]
        alpha = next(item for item in projects if item["id"] == self.project_id)
        self.assertEqual(alpha["my_role"], "Viewer")
        self.assertIn("application:read", alpha["permissions"])
        self.assertNotIn("deployment:execute", alpha["permissions"])
        readable = client.get("/api/v1/applications/alpha-app")
        self.assertEqual(readable.status_code, 200)
        denied = client.post("/api/v1/applications/alpha-app/pipeline")
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.get_json()["error"]["code"], "FORBIDDEN")

    def test_project_admin_changes_role_and_owner_cannot_be_demoted(self):
        client = self.client_for("owner")
        csrf = self.csrf(client)
        changed = client.post(
            f"/projects/{self.project_id}/members/{self.memberships['developer']}/role",
            data={"csrf_token": csrf, "role": "operator"},
        )
        self.assertEqual(changed.status_code, 302)
        blocked = client.post(
            f"/projects/{self.project_id}/members/{self.memberships['owner']}/role",
            data={"csrf_token": csrf, "role": "viewer"},
        )
        self.assertEqual(blocked.status_code, 302)
        from app.db import db
        from app.models import ProjectMembership
        with self.app.app_context():
            developer = db.session.get(ProjectMembership, self.memberships["developer"])
            owner = db.session.get(ProjectMembership, self.memberships["owner"])
            self.assertEqual(developer.role.key, "operator")
            self.assertEqual(owner.role.key, "project_admin")

    def test_auditor_reads_only_current_project_audit(self):
        from app.delivery_store import replace_audit_logs

        replace_audit_logs([
            {"id": "alpha-event", "action": "alpha-event", "created_at": "2026-09-18 01:00:00", "metadata": {"project_id": self.project_id}},
            {"id": "beta-event", "action": "beta-event", "created_at": "2026-09-18 02:00:00", "metadata": {"project_id": self.beta_id}},
        ])
        from app.modules.audit.service import record_audit
        with self.app.app_context():
            generated = record_audit(
                "PIPELINE_TRIGGER", "alpha-app", "SUCCESS", "project scoped"
            )
            self.assertEqual(generated["metadata"]["project_id"], self.project_id)
        client = self.client_for("auditor")
        page = client.get("/audit")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"alpha-event", page.data)
        self.assertIn(b"PIPELINE_TRIGGER", page.data)
        self.assertNotIn(b"beta-event", page.data)


if __name__ == "__main__":
    unittest.main()
