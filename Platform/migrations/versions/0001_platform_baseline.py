"""Baseline the existing Platform schema.

Revision ID: 0001_platform_baseline
Revises: None
Create Date: 2026-08-27
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0001_platform_baseline"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

payload_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
received_at_type = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "delivery_migrations",
        sa.Column("name", sa.Text(), primary_key=True),
        sa.Column("applied_at", received_at_type, nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "applications",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("owner", sa.Text(), nullable=False, server_default=""),
        sa.Column("user_id", sa.Integer()),
        sa.Column("namespace", sa.Text(), nullable=False, unique=True),
        sa.Column("source_type", sa.Text(), nullable=False),
        sa.Column("repository_url", sa.Text(), nullable=False, server_default=""),
        sa.Column("default_branch", sa.Text(), nullable=False, server_default="main"),
        sa.Column("requested_ref", sa.Text(), nullable=False, server_default=""),
        sa.Column("build_context", sa.Text(), nullable=False, server_default="."),
        sa.Column("dockerfile_path", sa.Text(), nullable=False, server_default="Dockerfile"),
        sa.Column("current_deployment_id", sa.Text()),
        sa.Column("status", sa.Text(), nullable=False, server_default="Draft"),
        sa.Column("url", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.Text(), nullable=False, server_default=""),
        sa.Column("updated_at", sa.Text(), nullable=False, server_default=""),
        sa.Column("payload", payload_type, nullable=False),
    )
    op.create_table(
        "application_services",
        sa.Column("application_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("image", sa.Text(), nullable=False, server_default=""),
        sa.Column("required", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("container_port", sa.Integer(), nullable=False, server_default="80"),
        sa.Column("service_type", sa.Text(), nullable=False, server_default="ClusterIP"),
        sa.Column("health_path", sa.Text(), nullable=False, server_default=""),
        sa.Column("payload", payload_type, nullable=False),
        sa.ForeignKeyConstraint(["application_id"], ["applications.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("application_id", "name"),
    )
    op.create_table(
        "pipeline_runs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("application_id", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("trigger_type", sa.Text(), nullable=False, server_default="Manual"),
        sa.Column("repository_url", sa.Text(), nullable=False, server_default=""),
        sa.Column("branch", sa.Text(), nullable=False, server_default=""),
        sa.Column("commit_sha", sa.Text(), nullable=False, server_default=""),
        sa.Column("commit_author", sa.Text(), nullable=False, server_default=""),
        sa.Column("commit_message", sa.Text(), nullable=False, server_default=""),
        sa.Column("actor", sa.Text(), nullable=False, server_default="system"),
        sa.Column("actor_id", sa.Integer()),
        sa.Column("actor_role", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.Text(), nullable=False, server_default=""),
        sa.Column("updated_at", sa.Text(), nullable=False, server_default=""),
        sa.Column("finished_at", sa.Text(), nullable=False, server_default=""),
        sa.Column("payload", payload_type, nullable=False),
        sa.ForeignKeyConstraint(["application_id"], ["applications.id"], ondelete="CASCADE"),
    )
    op.create_index("ix_pipeline_runs_application_created", "pipeline_runs", ["application_id", sa.text("created_at DESC")])
    op.create_table(
        "pipeline_stages",
        sa.Column("pipeline_run_id", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("output", sa.Text(), nullable=False, server_default=""),
        sa.Column("started_at", sa.Text(), nullable=False, server_default=""),
        sa.Column("finished_at", sa.Text(), nullable=False, server_default=""),
        sa.Column("payload", payload_type, nullable=False),
        sa.ForeignKeyConstraint(["pipeline_run_id"], ["pipeline_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("pipeline_run_id", "name"),
    )
    op.create_table(
        "deployments",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("application_id", sa.Text(), nullable=False),
        sa.Column("pipeline_run_id", sa.Text()),
        sa.Column("previous_deployment_id", sa.Text()),
        sa.Column("rollback_of_deployment_id", sa.Text()),
        sa.Column("repository_url", sa.Text(), nullable=False, server_default=""),
        sa.Column("branch", sa.Text(), nullable=False, server_default=""),
        sa.Column("commit_sha", sa.Text(), nullable=False, server_default=""),
        sa.Column("manifest", sa.Text(), nullable=False, server_default=""),
        sa.Column("deployment_mode", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False, server_default="system"),
        sa.Column("actor_id", sa.Integer()),
        sa.Column("namespace", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False, server_default=""),
        sa.Column("verify_status", sa.Text(), nullable=False, server_default="Pending"),
        sa.Column("status", sa.Text(), nullable=False, server_default="Pending"),
        sa.Column("started_at", sa.Text(), nullable=False, server_default=""),
        sa.Column("finished_at", sa.Text(), nullable=False, server_default=""),
        sa.Column("payload", payload_type, nullable=False),
        sa.ForeignKeyConstraint(["application_id"], ["applications.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["pipeline_run_id"], ["pipeline_runs.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("application_id", "version"),
    )
    op.create_index("ix_deployments_application_version", "deployments", ["application_id", sa.text("version DESC")])
    op.create_table(
        "deployment_services",
        sa.Column("deployment_id", sa.Text(), nullable=False),
        sa.Column("service_name", sa.Text(), nullable=False),
        sa.Column("image_tag", sa.Text(), nullable=False),
        sa.Column("image_digest", sa.Text(), nullable=False, server_default=""),
        sa.Column("required", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("payload", payload_type, nullable=False),
        sa.ForeignKeyConstraint(["deployment_id"], ["deployments.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("deployment_id", "service_name"),
    )
    op.create_table(
        "jobs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("job_type", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("target", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("actor", sa.Text(), nullable=False, server_default="system"),
        sa.Column("actor_id", sa.Integer()),
        sa.Column("actor_role", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.Text(), nullable=False, server_default=""),
        sa.Column("updated_at", sa.Text(), nullable=False, server_default=""),
        sa.Column("finished_at", sa.Text(), nullable=False, server_default=""),
        sa.Column("payload", payload_type, nullable=False),
    )
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Text(), primary_key=True),
        sa.Column("actor", sa.Text(), nullable=False, server_default="anonymous"),
        sa.Column("actor_id", sa.Integer()),
        sa.Column("role", sa.Text(), nullable=False, server_default=""),
        sa.Column("ip", sa.Text(), nullable=False, server_default=""),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("target", sa.Text(), nullable=False),
        sa.Column("result", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False, server_default=""),
        sa.Column("payload", payload_type, nullable=False),
    )
    op.create_table(
        "webhook_deliveries",
        sa.Column("delivery_id", sa.Text(), primary_key=True),
        sa.Column("application_id", sa.Text(), nullable=False),
        sa.Column("event_type", sa.Text(), nullable=False),
        sa.Column("commit_sha", sa.Text(), nullable=False, server_default=""),
        sa.Column("received_at", received_at_type, nullable=False, server_default=sa.func.now()),
        sa.Column("status", sa.Text(), nullable=False, server_default="Accepted"),
        sa.Column("payload", payload_type, nullable=False),
        sa.ForeignKeyConstraint(["application_id"], ["applications.id"], ondelete="CASCADE"),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("username", sa.String(length=64), nullable=False, unique=True),
        sa.Column("password_hash", sa.String(length=256), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False, server_default="Developer"),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index("ix_users_username", "users", ["username"], unique=True)


def downgrade() -> None:
    # Baseline downgrade is intentionally explicit and destructive. Production
    # operators must restore from backup instead of downgrading below baseline.
    op.drop_index("ix_users_username", table_name="users")
    op.drop_table("users")
    op.drop_table("webhook_deliveries")
    op.drop_table("audit_logs")
    op.drop_table("jobs")
    op.drop_table("deployment_services")
    op.drop_index("ix_deployments_application_version", table_name="deployments")
    op.drop_table("deployments")
    op.drop_table("pipeline_stages")
    op.drop_index("ix_pipeline_runs_application_created", table_name="pipeline_runs")
    op.drop_table("pipeline_runs")
    op.drop_table("application_services")
    op.drop_table("applications")
    op.drop_table("delivery_migrations")
