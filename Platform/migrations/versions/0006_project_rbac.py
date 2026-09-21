"""Add project-scoped roles and permissions.

Revision ID: 0006_project_rbac
Revises: 0005_projects_memberships
Create Date: 2026-09-18
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0006_project_rbac"
down_revision: Union[str, None] = "0005_projects_memberships"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


PERMISSIONS = (
    ("project:manage", "Cập nhật cấu hình project."),
    ("member:manage", "Quản lý thành viên và role."),
    ("application:create", "Tạo application."),
    ("application:read", "Xem application."),
    ("application:update", "Cập nhật application và secret."),
    ("application:delete", "Xóa application hoặc workload."),
    ("deployment:execute", "Chạy deploy, pipeline và restart."),
    ("deployment:rollback", "Rollback deployment."),
    ("service:scale", "Scale service."),
    ("monitoring:read", "Xem monitoring và log."),
    ("audit:read", "Xem audit project."),
    ("quota:manage", "Quản lý quota project."),
)

ROLES = {
    "project_admin": (
        "Project Admin",
        "Quản lý project và workload.",
        {key for key, _description in PERMISSIONS if key != "quota:manage"},
    ),
    "developer": (
        "Developer",
        "Phát triển và triển khai application.",
        {
            "application:create", "application:read", "application:update",
            "application:delete", "deployment:execute", "deployment:rollback",
            "service:scale", "monitoring:read",
        },
    ),
    "operator": (
        "Operator",
        "Vận hành workload.",
        {
            "application:read", "deployment:execute", "deployment:rollback",
            "service:scale", "monitoring:read",
        },
    ),
    "viewer": (
        "Viewer",
        "Chỉ xem application và monitoring.",
        {"application:read", "monitoring:read"},
    ),
    "auditor": (
        "Auditor",
        "Xem application, monitoring và audit.",
        {"application:read", "monitoring:read", "audit:read"},
    ),
}


def upgrade() -> None:
    op.create_table(
        "rbac_permissions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(length=80), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=False, server_default=""),
    )
    op.create_index("ix_rbac_permissions_key", "rbac_permissions", ["key"], unique=True)
    op.create_table(
        "rbac_roles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("key", sa.String(length=40), nullable=False),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=False, server_default=""),
        sa.Column("is_system", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.UniqueConstraint("name", name="uq_rbac_roles_name"),
    )
    op.create_index("ix_rbac_roles_key", "rbac_roles", ["key"], unique=True)
    op.create_table(
        "rbac_role_permissions",
        sa.Column("role_id", sa.Integer(), nullable=False),
        sa.Column("permission_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["role_id"], ["rbac_roles.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["permission_id"], ["rbac_permissions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("role_id", "permission_id"),
    )

    connection = op.get_bind()
    for key, description in PERMISSIONS:
        connection.execute(
            sa.text(
                "INSERT INTO rbac_permissions (key, description) "
                "VALUES (:key, :description)"
            ),
            {"key": key, "description": description},
        )
    for key, (name, description, permission_keys) in ROLES.items():
        connection.execute(
            sa.text(
                "INSERT INTO rbac_roles (key, name, description, is_system) "
                "VALUES (:key, :name, :description, :is_system)"
            ),
            {"key": key, "name": name, "description": description, "is_system": True},
        )
        role_id = connection.execute(
            sa.text("SELECT id FROM rbac_roles WHERE key = :key"), {"key": key}
        ).scalar_one()
        for permission_key in sorted(permission_keys):
            permission_id = connection.execute(
                sa.text("SELECT id FROM rbac_permissions WHERE key = :key"),
                {"key": permission_key},
            ).scalar_one()
            connection.execute(
                sa.text(
                    "INSERT INTO rbac_role_permissions (role_id, permission_id) "
                    "VALUES (:role_id, :permission_id)"
                ),
                {"role_id": role_id, "permission_id": permission_id},
            )

    op.add_column("project_memberships", sa.Column("role_id", sa.Integer(), nullable=True))
    connection.execute(
        sa.text(
            "UPDATE project_memberships SET role_id = ("
            "SELECT r.id FROM rbac_roles r WHERE r.key = CASE "
            "WHEN project_memberships.user_id = ("
            "SELECT p.owner_user_id FROM projects p "
            "WHERE p.id = project_memberships.project_id"
            ") THEN 'project_admin' "
            "WHEN (SELECT u.role FROM users u WHERE u.id = project_memberships.user_id) = 'Admin' "
            "THEN 'project_admin' "
            "WHEN (SELECT u.role FROM users u WHERE u.id = project_memberships.user_id) = 'Viewer' "
            "THEN 'viewer' ELSE 'developer' END)"
        )
    )
    with op.batch_alter_table("project_memberships") as batch_op:
        batch_op.alter_column("role_id", nullable=False)
        batch_op.create_foreign_key(
            "fk_project_memberships_role_id_rbac_roles",
            "rbac_roles",
            ["role_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    op.create_index(
        "ix_project_memberships_role_id", "project_memberships", ["role_id"]
    )


def downgrade() -> None:
    op.drop_index("ix_project_memberships_role_id", table_name="project_memberships")
    with op.batch_alter_table("project_memberships") as batch_op:
        batch_op.drop_constraint(
            "fk_project_memberships_role_id_rbac_roles", type_="foreignkey"
        )
        batch_op.drop_column("role_id")
    op.drop_table("rbac_role_permissions")
    op.drop_index("ix_rbac_roles_key", table_name="rbac_roles")
    op.drop_table("rbac_roles")
    op.drop_index("ix_rbac_permissions_key", table_name="rbac_permissions")
    op.drop_table("rbac_permissions")
