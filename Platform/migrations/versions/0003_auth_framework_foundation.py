"""Add Flask-Security identity fields and compatibility role tables.

Revision ID: 0003_auth_framework_foundation
Revises: 0002_account_email_verification
Create Date: 2026-08-27
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import uuid


revision: str = "0003_auth_framework_foundation"
down_revision: Union[str, None] = "0002_account_email_verification"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("fs_uniquifier", sa.String(64), nullable=True))
    op.add_column(
        "users", sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true())
    )
    op.add_column("users", sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True))
    connection = op.get_bind()
    user_rows = connection.execute(sa.text("SELECT id FROM users")).fetchall()
    for row in user_rows:
        connection.execute(
            sa.text("UPDATE users SET fs_uniquifier = :value WHERE id = :user_id"),
            {"value": uuid.uuid4().hex, "user_id": row.id},
        )
    op.execute("UPDATE users SET active = (status = 'Active')")
    op.execute(
        "UPDATE users SET confirmed_at = "
        "COALESCE(email_verified_at, created_at, CURRENT_TIMESTAMP) "
        "WHERE status = 'Active'"
    )
    with op.batch_alter_table("users") as batch_op:
        batch_op.alter_column("fs_uniquifier", nullable=False)
        batch_op.create_index(
            "ix_users_fs_uniquifier", ["fs_uniquifier"], unique=True
        )

    op.create_table(
        "security_roles",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("description", sa.String(length=255), nullable=True),
        sa.UniqueConstraint("name"),
    )
    op.create_table(
        "security_user_roles",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("role_id", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["role_id"], ["security_roles.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id", "role_id"),
    )


def downgrade() -> None:
    op.drop_table("security_user_roles")
    op.drop_table("security_roles")
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_index("ix_users_fs_uniquifier")
        batch_op.drop_column("confirmed_at")
        batch_op.drop_column("active")
        batch_op.drop_column("fs_uniquifier")
