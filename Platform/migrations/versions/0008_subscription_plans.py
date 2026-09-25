"""Add project subscription plans and upgrade workflow.

Revision ID: 0008_subscription_plans
Revises: 0007_project_api_tokens
Create Date: 2026-09-25
"""
from __future__ import annotations

import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0008_subscription_plans"
down_revision: Union[str, None] = "0007_project_api_tokens"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


PLANS = {
    "basic": (
        "Basic",
        "Gói cơ bản cho nhóm nhỏ và môi trường thử nghiệm.",
        False,
        {
            "max_applications": 3, "max_services": 10, "max_replicas": 10,
            "cpu_request_millicores": 2000, "cpu_limit_millicores": 4000,
            "memory_request_mib": 4096, "memory_limit_mib": 8192,
            "max_ingresses": 3, "max_pvcs": 3, "storage_mib": 10240,
        },
    ),
    "pro": (
        "Pro/VIP",
        "Gói mở rộng cho workload nhiều service.",
        False,
        {
            "max_applications": 10, "max_services": 40, "max_replicas": 40,
            "cpu_request_millicores": 8000, "cpu_limit_millicores": 16000,
            "memory_request_mib": 16384, "memory_limit_mib": 32768,
            "max_ingresses": 10, "max_pvcs": 10, "storage_mib": 102400,
        },
    ),
    "custom": (
        "Custom",
        "Hạn mức riêng do Platform Admin phê duyệt.",
        True,
        {},
    ),
}


def upgrade() -> None:
    op.create_table(
        "subscription_plans",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("limits_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("is_system", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("is_custom", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("name", name="uq_subscription_plans_name"),
    )
    op.create_index("ix_subscription_plans_key", "subscription_plans", ["key"], unique=True)

    op.create_table(
        "project_subscriptions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("plan_id", sa.Integer(), nullable=False),
        sa.Column("effective_limits_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("assigned_by_user_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["plan_id"], ["subscription_plans.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["assigned_by_user_id"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index(
        "ix_project_subscriptions_project_id",
        "project_subscriptions",
        ["project_id"],
        unique=True,
    )
    op.create_index("ix_project_subscriptions_plan_id", "project_subscriptions", ["plan_id"])

    op.create_table(
        "subscription_upgrade_requests",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("requested_plan_id", sa.Integer(), nullable=False),
        sa.Column("requested_limits_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("reason", sa.String(length=1000), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="Pending"),
        sa.Column("requested_by_user_id", sa.Integer(), nullable=True),
        sa.Column("reviewed_by_user_id", sa.Integer(), nullable=True),
        sa.Column("admin_note", sa.String(length=1000), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["requested_plan_id"], ["subscription_plans.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["requested_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["reviewed_by_user_id"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_subscription_upgrade_requests_project_id", "subscription_upgrade_requests", ["project_id"])
    op.create_index("ix_subscription_upgrade_requests_requested_plan_id", "subscription_upgrade_requests", ["requested_plan_id"])
    op.create_index("ix_subscription_upgrade_requests_status", "subscription_upgrade_requests", ["status"])
    op.create_index("ix_subscription_upgrade_requests_created_at", "subscription_upgrade_requests", ["created_at"])

    op.create_table(
        "subscription_history",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("from_plan_id", sa.Integer(), nullable=True),
        sa.Column("to_plan_id", sa.Integer(), nullable=False),
        sa.Column("from_limits_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("to_limits_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("action", sa.String(length=40), nullable=False),
        sa.Column("actor_user_id", sa.Integer(), nullable=True),
        sa.Column("request_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["from_plan_id"], ["subscription_plans.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["to_plan_id"], ["subscription_plans.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["actor_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["request_id"], ["subscription_upgrade_requests.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_subscription_history_project_id", "subscription_history", ["project_id"])
    op.create_index("ix_subscription_history_created_at", "subscription_history", ["created_at"])

    connection = op.get_bind()
    for key, (name, description, is_custom, limits) in PLANS.items():
        connection.execute(sa.text(
            "INSERT INTO subscription_plans "
            "(key, name, description, limits_json, is_system, is_custom, active) "
            "VALUES (:key, :name, :description, :limits, :is_system, :is_custom, :active)"
        ), {
            "key": key, "name": name, "description": description,
            "limits": json.dumps(limits, sort_keys=True, separators=(",", ":")),
            "is_system": True, "is_custom": is_custom, "active": True,
        })
    basic_id = connection.execute(
        sa.text("SELECT id FROM subscription_plans WHERE key = 'basic'")
    ).scalar_one()
    basic_limits = json.dumps(PLANS["basic"][3], sort_keys=True, separators=(",", ":"))
    projects = connection.execute(
        sa.text("SELECT id, owner_user_id FROM projects ORDER BY id")
    ).mappings().all()
    for project in projects:
        connection.execute(sa.text(
            "INSERT INTO project_subscriptions "
            "(project_id, plan_id, effective_limits_json, assigned_by_user_id) "
            "VALUES (:project_id, :plan_id, :limits, :actor_id)"
        ), {
            "project_id": project["id"], "plan_id": basic_id,
            "limits": basic_limits, "actor_id": project["owner_user_id"],
        })
        connection.execute(sa.text(
            "INSERT INTO subscription_history "
            "(project_id, from_plan_id, to_plan_id, from_limits_json, "
            "to_limits_json, action, actor_user_id) "
            "VALUES (:project_id, NULL, :plan_id, '{}', :limits, "
            "'INITIAL_ASSIGNMENT', :actor_id)"
        ), {
            "project_id": project["id"], "plan_id": basic_id,
            "limits": basic_limits, "actor_id": project["owner_user_id"],
        })


def downgrade() -> None:
    op.drop_index("ix_subscription_history_created_at", table_name="subscription_history")
    op.drop_index("ix_subscription_history_project_id", table_name="subscription_history")
    op.drop_table("subscription_history")
    op.drop_index("ix_subscription_upgrade_requests_created_at", table_name="subscription_upgrade_requests")
    op.drop_index("ix_subscription_upgrade_requests_status", table_name="subscription_upgrade_requests")
    op.drop_index("ix_subscription_upgrade_requests_requested_plan_id", table_name="subscription_upgrade_requests")
    op.drop_index("ix_subscription_upgrade_requests_project_id", table_name="subscription_upgrade_requests")
    op.drop_table("subscription_upgrade_requests")
    op.drop_index("ix_project_subscriptions_plan_id", table_name="project_subscriptions")
    op.drop_index("ix_project_subscriptions_project_id", table_name="project_subscriptions")
    op.drop_table("project_subscriptions")
    op.drop_index("ix_subscription_plans_key", table_name="subscription_plans")
    op.drop_table("subscription_plans")
