"""Add projects, memberships and project ownership for applications.

Revision ID: 0005_projects_memberships
Revises: 0004_account_recovery_security
Create Date: 2026-09-18
"""
from __future__ import annotations

import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0005_projects_memberships"
down_revision: Union[str, None] = "0004_account_recovery_security"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("description", sa.String(length=500), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="Active"),
        sa.Column("owner_user_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_projects_slug", "projects", ["slug"], unique=True)
    op.create_index("ix_projects_owner_user_id", "projects", ["owner_user_id"])

    op.create_table(
        "project_memberships",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="Active"),
        sa.Column("invited_by_user_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["invited_by_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("project_id", "user_id", name="uq_project_membership"),
    )
    op.create_index(
        "ix_project_memberships_project_id", "project_memberships", ["project_id"]
    )
    op.create_index(
        "ix_project_memberships_user_id", "project_memberships", ["user_id"]
    )

    connection = op.get_bind()
    owner_id = connection.execute(
        sa.text(
            "SELECT id FROM users ORDER BY CASE WHEN role = 'Admin' THEN 0 ELSE 1 END, id LIMIT 1"
        )
    ).scalar()
    connection.execute(
        sa.text(
            "INSERT INTO projects "
            "(name, slug, description, status, owner_user_id, created_at, updated_at) "
            "VALUES ('Default Project', 'default-project', "
            "'Project mặc định chứa dữ liệu trước A.4.1.', 'Active', :owner_id, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ),
        {"owner_id": owner_id},
    )
    project_id = connection.execute(
        sa.text("SELECT id FROM projects WHERE slug = 'default-project'")
    ).scalar_one()
    user_ids = connection.execute(
        sa.text("SELECT id FROM users ORDER BY id")
    ).scalars().all()
    for user_id in user_ids:
        connection.execute(
            sa.text(
                "INSERT INTO project_memberships "
                "(project_id, user_id, status, invited_by_user_id, created_at, updated_at) "
                "VALUES (:project_id, :user_id, 'Active', :owner_id, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            ),
            {"project_id": project_id, "user_id": user_id, "owner_id": owner_id},
        )

    op.add_column("applications", sa.Column("project_id", sa.Integer(), nullable=True))
    connection.execute(
        sa.text("UPDATE applications SET project_id = :project_id"),
        {"project_id": project_id},
    )
    # Materialize before issuing UPDATE statements on the same connection.
    # This avoids invalidating a streaming cursor on PostgreSQL drivers.
    rows = connection.execute(
        sa.text("SELECT id, payload FROM applications")
    ).mappings().all()
    for row in rows:
        payload = row["payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        payload = dict(payload)
        payload["project_id"] = project_id
        payload_assignment = (
            "CAST(:payload AS JSONB)"
            if connection.dialect.name == "postgresql"
            else ":payload"
        )
        connection.execute(
            sa.text(f"UPDATE applications SET payload = {payload_assignment} WHERE id = :id"),
            {"payload": json.dumps(payload, ensure_ascii=False), "id": row["id"]},
        )
    with op.batch_alter_table("applications") as batch_op:
        batch_op.alter_column("project_id", nullable=False)
        batch_op.create_foreign_key(
            "fk_applications_project_id_projects",
            "projects",
            ["project_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    op.create_index("ix_applications_project_id", "applications", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_applications_project_id", table_name="applications")
    with op.batch_alter_table("applications") as batch_op:
        batch_op.drop_constraint(
            "fk_applications_project_id_projects", type_="foreignkey"
        )
        batch_op.drop_column("project_id")
    op.drop_index("ix_project_memberships_user_id", table_name="project_memberships")
    op.drop_index("ix_project_memberships_project_id", table_name="project_memberships")
    op.drop_table("project_memberships")
    op.drop_index("ix_projects_owner_user_id", table_name="projects")
    op.drop_index("ix_projects_slug", table_name="projects")
    op.drop_table("projects")
