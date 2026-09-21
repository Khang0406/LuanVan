"""Phase A.4.1 acceptance tests for project tenancy and memberships."""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from werkzeug.datastructures import MultiDict


def test_config(root: Path):
    class TestConfig:
        TESTING = True
        SECRET_KEY = "phase-a41-test"
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{root / 'users.db'}"
        SQLALCHEMY_TRACK_MODIFICATIONS = False

    return TestConfig


def application(application_id: str, project_id: int, user_id: int) -> dict:
    return {
        "id": application_id,
        "name": application_id,
        "project_id": project_id,
        "owner": "team",
        "user_id": user_id,
        "namespace": application_id,
        "source_type": "docker",
        "docker_image": "nginx:stable-alpine",
        "services": [{
            "name": "web", "image": "nginx:stable-alpine",
            "container_port": 80, "replicas": 1, "service_type": "ClusterIP",
        }],
        "status": "Draft",
        "created_at": "2026-09-18 00:00:00",
        "updated_at": "2026-09-18 00:00:00",
    }


class ProjectTenancyTests(unittest.TestCase):
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
        from app.models import Project, ProjectMembership, RBACRole, User

        self.app = create_app(test_config(self.root))
        with self.app.app_context():
            alice = User(
                username="alice", email="alice@example.com", role="Developer",
                status=User.STATUS_ACTIVE, active=True,
            )
            alice.set_password("password-123")
            bob = User(
                username="bob", email="bob@example.com", role="Developer",
                status=User.STATUS_ACTIVE, active=True,
            )
            bob.set_password("password-123")
            db.session.add_all([alice, bob])
            db.session.flush()
            alpha = Project(name="Alpha", slug="alpha", owner_user_id=alice.id)
            beta = Project(name="Beta", slug="beta", owner_user_id=bob.id)
            db.session.add_all([alpha, beta])
            db.session.flush()
            owner_role = RBACRole.query.filter_by(key="project_admin").one()
            db.session.add_all([
                ProjectMembership(
                    project_id=alpha.id, user_id=alice.id,
                    invited_by_user_id=alice.id, role_id=owner_role.id,
                ),
                ProjectMembership(
                    project_id=beta.id, user_id=bob.id,
                    invited_by_user_id=bob.id, role_id=owner_role.id,
                ),
            ])
            db.session.commit()
            self.alice_id, self.alice_session = alice.id, alice.fs_uniquifier
            self.bob_id, self.bob_session = bob.id, bob.fs_uniquifier
            self.alpha_id, self.beta_id = alpha.id, beta.id

        from app.delivery_store import replace_applications

        replace_applications([
            application("alpha-app", self.alpha_id, self.alice_id),
            application("beta-app", self.beta_id, self.bob_id),
        ])

    def tearDown(self):
        self.environment.stop()
        self.directory.cleanup()

    def client_for(self, session_id: str, project_id: int | None = None):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["_user_id"] = session_id
            session["_fresh"] = True
            if project_id is not None:
                session["active_project_id"] = project_id
        return client

    @staticmethod
    def csrf(client, path: str) -> str:
        client.get(path)
        with client.session_transaction() as session:
            return session["_csrf_token"]

    def test_member_sees_only_own_project_apps_on_web_and_api(self):
        client = self.client_for(self.alice_session, self.alpha_id)
        page = client.get("/applications")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"alpha-app", page.data)
        self.assertNotIn(b"beta-app", page.data)
        self.assertEqual(client.get("/applications/beta-app").status_code, 404)

        api = client.get("/api/v1/applications")
        self.assertEqual(api.status_code, 200)
        ids = {item["id"] for item in api.get_json()["data"]}
        self.assertEqual(ids, {"alpha-app"})
        forbidden = client.get(f"/api/v1/applications?project_id={self.beta_id}")
        self.assertEqual(forbidden.status_code, 403)

    def test_one_user_can_join_multiple_projects_and_disabled_membership_revokes_access(self):
        from app.db import db
        from app.models import Project, ProjectMembership, User
        from app.modules.projects.service import (
            add_project_member,
            list_accessible_projects,
            remove_project_member,
            set_membership_status,
        )
        from app.delivery_store import list_applications

        with self.app.app_context():
            alice = db.session.get(User, self.alice_id)
            beta = db.session.get(Project, self.beta_id)
            bob = db.session.get(User, self.bob_id)
            membership = add_project_member(beta, "alice@example.com", bob)
            self.assertEqual(
                {project.slug for project in list_accessible_projects(alice)},
                {"alpha", "beta"},
            )
            set_membership_status(beta, membership, ProjectMembership.STATUS_DISABLED)
            self.assertEqual(
                {project.slug for project in list_accessible_projects(alice)},
                {"alpha"},
            )
            remove_project_member(beta, membership)
            self.assertIsNone(ProjectMembership.query.filter_by(
                project_id=beta.id, user_id=alice.id
            ).first())
        self.assertIn("beta-app", {app["id"] for app in list_applications()})

    def test_non_owner_cannot_manage_members_and_owner_cannot_be_removed(self):
        from app.db import db
        from app.models import Project, ProjectMembership, User
        from app.modules.projects.service import add_project_member, remove_project_member

        with self.app.app_context():
            alpha = db.session.get(Project, self.alpha_id)
            alice = db.session.get(User, self.alice_id)
            bob_membership = add_project_member(alpha, "bob", alice)
            owner_membership = ProjectMembership.query.filter_by(
                project_id=alpha.id, user_id=alice.id
            ).one()
            with self.assertRaisesRegex(ValueError, "owner"):
                remove_project_member(alpha, owner_membership)
            bob_membership_id = bob_membership.id

        bob_client = self.client_for(self.bob_session, self.alpha_id)
        csrf = self.csrf(bob_client, f"/projects/{self.alpha_id}")
        response = bob_client.post(
            f"/projects/{self.alpha_id}/members",
            data={"csrf_token": csrf, "identity": "viewer"},
        )
        self.assertEqual(response.status_code, 403)
        with self.app.app_context():
            self.assertIsNotNone(db.session.get(ProjectMembership, bob_membership_id))

    def test_new_application_is_bound_to_active_project(self):
        from app.delivery_store import list_applications

        client = self.client_for(self.alice_session, self.alpha_id)
        csrf = self.csrf(client, "/applications/new")
        form = MultiDict([
            ("csrf_token", csrf),
            ("name", "new-alpha-app"),
            ("owner", "alpha-team"),
            ("source_type", "docker"),
            ("docker_image", "nginx:stable-alpine"),
            ("service_names", "web"),
            ("service_images", "nginx:stable-alpine"),
            ("service_ports", "80"),
            ("service_replicas", "1"),
            ("service_types", "ClusterIP"),
            ("service_node_ports", ""),
            ("service_envs", ""),
            ("service_commands", ""),
            ("service_args", ""),
            ("service_publics", "on"),
        ])
        response = client.post("/applications/new", data=form)
        self.assertEqual(response.status_code, 302)
        created = next(app for app in list_applications() if app["id"] == "new-alpha-app")
        self.assertEqual(created["project_id"], self.alpha_id)

    def test_archived_project_is_excluded_for_members_and_platform_admin(self):
        from app.db import db
        from app.models import Project, User
        from app.modules.applications.service import load_accessible_applications

        with self.app.app_context():
            alpha = db.session.get(Project, self.alpha_id)
            alice = db.session.get(User, self.alice_id)
            admin = User.query.filter_by(role="Admin").first()
            alpha.status = Project.STATUS_ARCHIVED
            db.session.commit()

            self.assertEqual(load_accessible_applications(alice), [])
            self.assertNotIn(
                "alpha-app", {app["id"] for app in load_accessible_applications(admin)}
            )

    def test_project_job_scope_includes_team_jobs_and_excludes_other_projects(self):
        from app.db import db
        from app.delivery_store import replace_jobs
        from app.models import User
        from app.modules.jobs.service import load_accessible_jobs

        def job(job_id, actor_id, application_id=""):
            return {
                "id": job_id,
                "status": "Running",
                "created_at": "2026-09-18 00:00:00",
                "actor_id": actor_id,
                "metadata": {"application_id": application_id},
            }

        replace_jobs([
            job("team-alpha", self.bob_id, "alpha-app"),
            job("own-beta", self.alice_id, "beta-app"),
            job("own-infra", self.alice_id),
            job("other-infra", self.bob_id),
        ])
        with self.app.app_context():
            alice = db.session.get(User, self.alice_id)
            visible = load_accessible_jobs(
                alice, limit=None, application_ids={"alpha-app"}
            )
        self.assertEqual(
            {item["id"] for item in visible}, {"team-alpha", "own-infra"}
        )

    def test_duplicate_project_and_membership_return_domain_errors(self):
        from app.db import db
        from app.models import Project, User
        from app.modules.projects.service import add_project_member, create_project

        with self.app.app_context():
            alice = db.session.get(User, self.alice_id)
            alpha = db.session.get(Project, self.alpha_id)
            with self.assertRaisesRegex(ValueError, "tồn tại"):
                create_project("Alpha", "duplicate", alice)

            add_project_member(alpha, "bob", alice)
            with self.assertRaisesRegex(ValueError, "đã là thành viên"):
                add_project_member(alpha, "bob", alice)

    def test_project_member_cannot_read_cluster_node_charts(self):
        client = self.client_for(self.alice_session, self.alpha_id)
        response = client.get("/monitoring/api/charts")
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["cpu"], [])
        self.assertEqual(payload["memory"], [])
        self.assertEqual(payload["network"], [])


if __name__ == "__main__":
    unittest.main()
