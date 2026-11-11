"""Add project namespaces and Kubernetes quota synchronization state.

Revision ID: 0009_project_kubernetes_quotas
Revises: 0008_subscription_plans
Create Date: 2026-09-28
"""
from __future__ import annotations

import json
import re
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0009_project_kubernetes_quotas"
down_revision: Union[str, None] = "0008_subscription_plans"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


NAMING_CONVENTION = {"uq": "uq_%(table_name)s_%(column_0_name)s"}


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", value.lower().strip())
    return re.sub(r"-+", "-", slug).strip("-") or "tenant"


def _namespace(project_id: int, slug: str) -> str:
    prefix = f"project-{project_id}-"
    suffix = _slug(slug)[: max(1, 63 - len(prefix))].rstrip("-") or "tenant"
    return f"{prefix}{suffix}"


def _payload_dict(payload) -> dict:
    if isinstance(payload, str):
        payload = json.loads(payload)
    return dict(payload or {})


def _payload_assignment(connection) -> str:
    return "CAST(:payload AS JSONB)" if connection.dialect.name == "postgresql" else ":payload"


def _drop_namespace_unique(connection) -> None:
    constraints = sa.inspect(connection).get_unique_constraints("applications")
    constraint = next(
        (item for item in constraints if item.get("column_names") == ["namespace"]),
        None,
    )
    if constraint is None:
        return
    name = constraint.get("name") or "uq_applications_namespace"
    with op.batch_alter_table(
        "applications", naming_convention=NAMING_CONVENTION
    ) as batch_op:
        batch_op.drop_constraint(name, type_="unique")


def upgrade() -> None:
    op.add_column("projects", sa.Column("kubernetes_namespace", sa.String(63), nullable=True))
    op.add_column(
        "projects",
        sa.Column("quota_sync_status", sa.String(20), nullable=False, server_default="Pending"),
    )
    op.add_column(
        "projects",
        sa.Column("quota_sync_message", sa.String(500), nullable=False, server_default=""),
    )
    op.add_column(
        "projects",
        sa.Column("quota_desired_hash", sa.String(64), nullable=False, server_default=""),
    )
    op.add_column(
        "projects",
        sa.Column("quota_observed_hash", sa.String(64), nullable=False, server_default=""),
    )
    op.add_column(
        "projects", sa.Column("quota_synced_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_index(
        "ix_projects_kubernetes_namespace",
        "projects",
        ["kubernetes_namespace"],
        unique=True,
    )
    op.create_index("ix_projects_quota_sync_status", "projects", ["quota_sync_status"])

    connection = op.get_bind()
    projects = connection.execute(
        sa.text("SELECT id, slug FROM projects ORDER BY id")
    ).mappings().all()
    namespaces: dict[int, str] = {}
    for project in projects:
        namespace = _namespace(project["id"], project["slug"])
        namespaces[project["id"]] = namespace
        connection.execute(
            sa.text(
                "UPDATE projects SET kubernetes_namespace=:namespace, "
                "quota_sync_status='Pending', "
                "quota_sync_message='Chờ đồng bộ ResourceQuota và LimitRange.' "
                "WHERE id=:id"
            ),
            {"namespace": namespace, "id": project["id"]},
        )

    _drop_namespace_unique(connection)
    rows = connection.execute(
        sa.text("SELECT id, project_id, namespace, payload FROM applications ORDER BY id")
    ).mappings().all()
    payload_assignment = _payload_assignment(connection)
    for row in rows:
        namespace = namespaces.get(row["project_id"])
        if not namespace:
            continue
        payload = _payload_dict(row["payload"])
        old_namespace = row["namespace"]
        if old_namespace != namespace:
            payload["legacy_namespace"] = old_namespace
        payload["namespace"] = namespace
        connection.execute(
            sa.text(
                f"UPDATE applications SET namespace=:namespace, "
                f"payload={payload_assignment} WHERE id=:id"
            ),
            {
                "namespace": namespace,
                "payload": json.dumps(payload, ensure_ascii=False),
                "id": row["id"],
            },
        )


def downgrade() -> None:
    connection = op.get_bind()
    rows = connection.execute(
        sa.text("SELECT id, namespace, payload FROM applications ORDER BY id")
    ).mappings().all()
    payload_assignment = _payload_assignment(connection)
    for row in rows:
        payload = _payload_dict(row["payload"])
        restored_namespace = payload.pop("legacy_namespace", None) or row["id"]
        payload["namespace"] = restored_namespace
        connection.execute(
            sa.text(
                f"UPDATE applications SET namespace=:namespace, "
                f"payload={payload_assignment} WHERE id=:id"
            ),
            {
                "namespace": restored_namespace,
                "payload": json.dumps(payload, ensure_ascii=False),
                "id": row["id"],
            },
        )

    with op.batch_alter_table("applications") as batch_op:
        batch_op.create_unique_constraint(
            "uq_applications_namespace", ["namespace"]
        )

    op.drop_index("ix_projects_quota_sync_status", table_name="projects")
    op.drop_index("ix_projects_kubernetes_namespace", table_name="projects")
    with op.batch_alter_table("projects") as batch_op:
        batch_op.drop_column("quota_synced_at")
        batch_op.drop_column("quota_observed_hash")
        batch_op.drop_column("quota_desired_hash")
        batch_op.drop_column("quota_sync_message")
        batch_op.drop_column("quota_sync_status")
        batch_op.drop_column("kubernetes_namespace")
