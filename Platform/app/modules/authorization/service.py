from __future__ import annotations

from typing import Any

from app.db import db
from app.models import (
    Project,
    ProjectMembership,
    RBACPermission,
    RBACRole,
)


PROJECT_MANAGE = "project:manage"
MEMBER_MANAGE = "member:manage"
APPLICATION_CREATE = "application:create"
APPLICATION_READ = "application:read"
APPLICATION_UPDATE = "application:update"
APPLICATION_DELETE = "application:delete"
DEPLOYMENT_EXECUTE = "deployment:execute"
DEPLOYMENT_ROLLBACK = "deployment:rollback"
SERVICE_SCALE = "service:scale"
MONITORING_READ = "monitoring:read"
AUDIT_READ = "audit:read"
QUOTA_MANAGE = "quota:manage"

PERMISSIONS = {
    PROJECT_MANAGE: "Cập nhật cấu hình project.",
    MEMBER_MANAGE: "Thêm, đổi role, vô hiệu hóa và xóa thành viên.",
    APPLICATION_CREATE: "Tạo application trong project.",
    APPLICATION_READ: "Xem application và lịch sử triển khai.",
    APPLICATION_UPDATE: "Cập nhật application, registry và secret.",
    APPLICATION_DELETE: "Xóa application hoặc workload.",
    DEPLOYMENT_EXECUTE: "Chạy deploy, pipeline và restart.",
    DEPLOYMENT_ROLLBACK: "Rollback deployment.",
    SERVICE_SCALE: "Thay đổi replica của service.",
    MONITORING_READ: "Xem metric và log của application.",
    AUDIT_READ: "Xem audit log thuộc project.",
    QUOTA_MANAGE: "Quản lý quota project (dùng ở A.5).",
}

ROLE_PROJECT_ADMIN = "project_admin"
ROLE_DEVELOPER = "developer"
ROLE_OPERATOR = "operator"
ROLE_VIEWER = "viewer"
ROLE_AUDITOR = "auditor"

ROLE_DEFINITIONS = {
    ROLE_PROJECT_ADMIN: {
        "name": "Project Admin",
        "description": "Quản lý project, thành viên và toàn bộ workload trong project.",
        "permissions": set(PERMISSIONS) - {QUOTA_MANAGE},
    },
    ROLE_DEVELOPER: {
        "name": "Developer",
        "description": "Phát triển, cấu hình và triển khai application.",
        "permissions": {
            APPLICATION_CREATE,
            APPLICATION_READ,
            APPLICATION_UPDATE,
            APPLICATION_DELETE,
            DEPLOYMENT_EXECUTE,
            DEPLOYMENT_ROLLBACK,
            SERVICE_SCALE,
            MONITORING_READ,
        },
    },
    ROLE_OPERATOR: {
        "name": "Operator",
        "description": "Vận hành deployment, rollback, scale và monitoring.",
        "permissions": {
            APPLICATION_READ,
            DEPLOYMENT_EXECUTE,
            DEPLOYMENT_ROLLBACK,
            SERVICE_SCALE,
            MONITORING_READ,
        },
    },
    ROLE_VIEWER: {
        "name": "Viewer",
        "description": "Chỉ xem application và monitoring.",
        "permissions": {APPLICATION_READ, MONITORING_READ},
    },
    ROLE_AUDITOR: {
        "name": "Auditor",
        "description": "Xem application, monitoring và audit của project.",
        "permissions": {APPLICATION_READ, MONITORING_READ, AUDIT_READ},
    },
}


def legacy_role_key(user: Any, *, owner: bool = False) -> str:
    if owner or getattr(user, "role", "") == "Admin":
        return ROLE_PROJECT_ADMIN
    if getattr(user, "role", "") == "Viewer":
        return ROLE_VIEWER
    return ROLE_DEVELOPER


def ensure_rbac_catalog() -> None:
    """Create/synchronize immutable system roles for local/test bootstrap."""
    permission_models: dict[str, RBACPermission] = {}
    for key, description in PERMISSIONS.items():
        permission = RBACPermission.query.filter_by(key=key).first()
        if permission is None:
            permission = RBACPermission(key=key, description=description)
            db.session.add(permission)
        else:
            permission.description = description
        permission_models[key] = permission
    db.session.flush()

    roles: dict[str, RBACRole] = {}
    for key, definition in ROLE_DEFINITIONS.items():
        role = RBACRole.query.filter_by(key=key).first()
        if role is None:
            role = RBACRole(key=key)
            db.session.add(role)
        role.name = definition["name"]
        role.description = definition["description"]
        role.is_system = True
        role.permissions = [
            permission_models[permission_key]
            for permission_key in sorted(definition["permissions"])
        ]
        roles[key] = role
    db.session.flush()

    for membership in ProjectMembership.query.filter_by(role_id=None).all():
        membership.role = roles[
            legacy_role_key(
                membership.user,
                owner=membership.project.owner_user_id == membership.user_id,
            )
        ]
    db.session.commit()


def get_role(role_key: str) -> RBACRole:
    if role_key not in ROLE_DEFINITIONS:
        raise ValueError("Role project không hợp lệ.")
    role = RBACRole.query.filter_by(key=role_key).first()
    if role is None:
        raise RuntimeError("RBAC catalog chưa được khởi tạo.")
    return role


def get_membership(user: Any, project_id: int) -> ProjectMembership | None:
    if not getattr(user, "is_authenticated", False):
        return None
    return (
        ProjectMembership.query.join(Project)
        .filter(
            ProjectMembership.project_id == project_id,
            ProjectMembership.user_id == getattr(user, "id", None),
            ProjectMembership.status == ProjectMembership.STATUS_ACTIVE,
            Project.status == Project.STATUS_ACTIVE,
        )
        .first()
    )


def has_permission(user: Any, permission: str, project_id: int | None) -> bool:
    if permission not in PERMISSIONS:
        return False
    token_project_id = getattr(user, "token_project_id", None)
    token_scopes = getattr(user, "api_token_scopes", None)
    if token_project_id is not None:
        if project_id != token_project_id or permission not in (token_scopes or set()):
            return False
        if getattr(user, "platform_admin", False):
            return True
        membership = get_membership(user, project_id)
        return bool(
            membership
            and membership.role
            and any(item.key == permission for item in membership.role.permissions)
        )
    if getattr(user, "is_admin", False):
        return True
    if project_id is None:
        return False
    membership = get_membership(user, project_id)
    if not membership or not membership.role:
        return False
    return any(item.key == permission for item in membership.role.permissions)


def permission_keys(user: Any, project_id: int | None) -> set[str]:
    token_project_id = getattr(user, "token_project_id", None)
    token_scopes = set(getattr(user, "api_token_scopes", set()))
    if token_project_id is not None:
        if project_id != token_project_id:
            return set()
        if getattr(user, "platform_admin", False):
            return token_scopes & set(PERMISSIONS)
        membership = get_membership(user, project_id)
        role_permissions = (
            {item.key for item in membership.role.permissions}
            if membership and membership.role
            else set()
        )
        return token_scopes & role_permissions
    if getattr(user, "is_admin", False):
        return set(PERMISSIONS)
    if project_id is None:
        return set()
    membership = get_membership(user, project_id)
    return {item.key for item in membership.role.permissions} if membership and membership.role else set()


def role_options() -> list[RBACRole]:
    return RBACRole.query.order_by(RBACRole.name).all()
