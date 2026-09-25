"""Acceptance tests for subscription plans and the admin approval workflow."""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


def test_config(root: Path):
    class TestConfig:
        TESTING = True
        SECRET_KEY = "phase-a51-test"
        SQLALCHEMY_DATABASE_URI = f"sqlite:///{root / 'users.db'}"
        SQLALCHEMY_TRACK_MODIFICATIONS = False

    return TestConfig


class SubscriptionPlanTests(unittest.TestCase):
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
        from app.models import ProjectMembership, RBACRole, User
        from app.modules.projects.service import create_project

        self.app = create_app(test_config(self.root))
        with self.app.app_context():
            admin = User.query.filter_by(username="admin").one()
            owner = User(
                username="plan-owner", email="plan-owner@example.com",
                role="Developer", status=User.STATUS_ACTIVE, active=True,
            )
            member = User(
                username="plan-member", email="plan-member@example.com",
                role="Developer", status=User.STATUS_ACTIVE, active=True,
            )
            outsider = User(
                username="plan-outsider", email="plan-outsider@example.com",
                role="Developer", status=User.STATUS_ACTIVE, active=True,
            )
            for user in (owner, member, outsider):
                user.set_password("password-123")
                db.session.add(user)
            db.session.commit()
            alpha = create_project("Subscription Alpha", "", owner)
            beta = create_project("Subscription Beta", "", outsider)
            developer = RBACRole.query.filter_by(key="developer").one()
            db.session.add(ProjectMembership(
                project_id=alpha.id,
                user_id=member.id,
                role_id=developer.id,
                invited_by_user_id=owner.id,
            ))
            db.session.commit()
            self.alpha_id = alpha.id
            self.beta_id = beta.id
            self.sessions = {
                "admin": admin.fs_uniquifier,
                "owner": owner.fs_uniquifier,
                "member": member.fs_uniquifier,
                "outsider": outsider.fs_uniquifier,
            }

    def tearDown(self):
        self.environment.stop()
        self.directory.cleanup()

    def client_for(self, name: str):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["_user_id"] = self.sessions[name]
            session["_fresh"] = True
            session["active_project_id"] = self.alpha_id
        return client

    @staticmethod
    def csrf(client, path: str) -> str:
        client.get(path)
        with client.session_transaction() as session:
            return session["_csrf_token"]

    def request_plan(self, client, plan: str = "pro", **extra):
        path = f"/projects/{self.alpha_id}/subscription"
        data = {
            "csrf_token": self.csrf(client, path),
            "plan": plan,
            "reason": "Cần thêm tài nguyên để chạy workload production.",
            **extra,
        }
        return client.post(path, data=data)

    def test_catalog_and_new_projects_receive_basic_snapshot(self):
        from app.db import db
        from app.models import Project, SubscriptionHistory, SubscriptionPlan
        from app.modules.subscriptions.service import subscription_limits

        with self.app.app_context():
            self.assertEqual(
                {item.key for item in SubscriptionPlan.query.all()},
                {"basic", "pro", "custom"},
            )
            alpha = db.session.get(Project, self.alpha_id)
            self.assertEqual(alpha.subscription.plan.key, "basic")
            self.assertEqual(subscription_limits(alpha.subscription)["max_applications"], 3)
            history = SubscriptionHistory.query.filter_by(project_id=alpha.id).all()
            self.assertEqual(len(history), 1)
            self.assertEqual(history[0].action, "INITIAL_ASSIGNMENT")

    def test_project_admin_requests_pro_and_duplicate_pending_is_blocked(self):
        client = self.client_for("owner")
        created = self.request_plan(client)
        duplicate = self.request_plan(client)
        self.assertEqual(created.status_code, 302)
        self.assertEqual(duplicate.status_code, 200)
        self.assertIn("đang chờ xử lý".encode(), duplicate.data)

        from app.models import SubscriptionUpgradeRequest
        with self.app.app_context():
            items = SubscriptionUpgradeRequest.query.filter_by(project_id=self.alpha_id).all()
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].requested_plan.key, "pro")
            self.assertEqual(items[0].status, "Pending")

    def test_regular_member_cannot_request_and_outsider_cannot_view(self):
        member = self.client_for("member")
        path = f"/projects/{self.alpha_id}/subscription"
        denied_request = member.post(path, data={
            "csrf_token": self.csrf(member, path),
            "plan": "pro", "reason": "Đây là lý do đủ dài.",
        })
        outsider = self.client_for("outsider")
        denied_view = outsider.get(path)
        member_api = member.get(f"/api/v1/projects/{self.alpha_id}/subscription")
        outsider_api = outsider.get(f"/api/v1/projects/{self.alpha_id}/subscription")
        self.assertEqual(denied_request.status_code, 403)
        self.assertEqual(denied_view.status_code, 404)
        self.assertEqual(member_api.status_code, 200)
        self.assertEqual(member_api.get_json()["data"]["plan"]["key"], "basic")
        self.assertEqual(outsider_api.status_code, 404)

    def test_admin_approves_request_and_records_history_and_audit(self):
        owner = self.client_for("owner")
        self.request_plan(owner)
        from app.models import SubscriptionUpgradeRequest
        with self.app.app_context():
            request_id = SubscriptionUpgradeRequest.query.filter_by(
                project_id=self.alpha_id
            ).one().id

        admin = self.client_for("admin")
        path = "/admin/subscriptions"
        approved = admin.post(
            f"/admin/subscriptions/requests/{request_id}/review",
            data={
                "csrf_token": self.csrf(admin, path),
                "decision": "approve",
                "admin_note": "Đủ điều kiện nâng cấp.",
            },
        )
        self.assertEqual(approved.status_code, 302)

        from app.db import db
        from app.models import Project, SubscriptionHistory
        from app.modules.audit.service import load_audit_logs
        from app.modules.subscriptions.service import subscription_limits
        with self.app.app_context():
            project = db.session.get(Project, self.alpha_id)
            self.assertEqual(project.subscription.plan.key, "pro")
            self.assertEqual(subscription_limits(project.subscription)["max_replicas"], 40)
            history = SubscriptionHistory.query.filter_by(project_id=self.alpha_id).all()
            self.assertEqual(history[-1].action, "REQUEST_APPROVED")
            self.assertEqual(history[-1].request_id, request_id)
        actions = [item["action"] for item in load_audit_logs(limit=None)]
        self.assertIn("SUBSCRIPTION_REQUEST_CREATE", actions)
        self.assertIn("SUBSCRIPTION_REQUEST_REVIEW", actions)

    def test_rejection_requires_note_and_does_not_change_plan(self):
        owner = self.client_for("owner")
        self.request_plan(owner)
        from app.models import SubscriptionUpgradeRequest
        with self.app.app_context():
            request_id = SubscriptionUpgradeRequest.query.filter_by(
                project_id=self.alpha_id
            ).one().id
        admin = self.client_for("admin")
        csrf = self.csrf(admin, "/admin/subscriptions")
        invalid = admin.post(
            f"/admin/subscriptions/requests/{request_id}/review",
            data={"csrf_token": csrf, "decision": "reject", "admin_note": ""},
        )
        rejected = admin.post(
            f"/admin/subscriptions/requests/{request_id}/review",
            data={
                "csrf_token": csrf,
                "decision": "reject",
                "admin_note": "Chưa đủ thông tin.",
            },
        )
        self.assertEqual(invalid.status_code, 302)
        self.assertEqual(rejected.status_code, 302)
        from app.db import db
        from app.models import Project
        with self.app.app_context():
            project = db.session.get(Project, self.alpha_id)
            self.assertEqual(project.subscription.plan.key, "basic")
            self.assertEqual(
                db.session.get(SubscriptionUpgradeRequest, request_id).status,
                "Rejected",
            )

    def test_admin_assigns_valid_custom_limits_and_cancels_pending(self):
        owner = self.client_for("owner")
        self.request_plan(owner)
        admin = self.client_for("admin")
        data = {
            "csrf_token": self.csrf(admin, "/admin/subscriptions"),
            "plan": "custom",
            "max_applications": "20", "max_services": "60",
            "max_replicas": "80", "cpu_request_millicores": "12000",
            "cpu_limit_millicores": "24000", "memory_request_mib": "24576",
            "memory_limit_mib": "49152", "max_ingresses": "20",
            "max_pvcs": "20", "storage_mib": "204800",
        }
        response = admin.post(
            "/admin/subscriptions/projects/assign",
            data={**data, "project_id": str(self.alpha_id)},
        )
        self.assertEqual(response.status_code, 302)
        from app.db import db
        from app.models import Project, SubscriptionUpgradeRequest
        from app.modules.subscriptions.service import subscription_limits
        with self.app.app_context():
            project = db.session.get(Project, self.alpha_id)
            self.assertEqual(project.subscription.plan.key, "custom")
            self.assertEqual(subscription_limits(project.subscription)["max_replicas"], 80)
            pending = SubscriptionUpgradeRequest.query.filter_by(
                project_id=self.alpha_id
            ).one()
            self.assertEqual(pending.status, "Cancelled")


if __name__ == "__main__":
    unittest.main()
