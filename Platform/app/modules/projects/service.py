from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from flask import has_request_context, session
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError

from app.db import db
from app.models import Project, ProjectMembership, User


DEFAULT_PROJECT_SLUG = "default-project"


def slugify_project(value: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", value.lower().strip())
    return re.sub(r"-+", "-", slug).strip("-") or "project"


def ensure_default_project() -> Project:
    """Finish bootstrap after Alembic runs before the first user is seeded."""
    project = Project.query.filter_by(slug=DEFAULT_PROJECT_SLUG).first()
    admin = User.query.filter_by(role="Admin").order_by(User.id).first()
    if project is None:
        project = Project(
            name="Default Project",
            slug=DEFAULT_PROJECT_SLUG,
            description="Project mặc định của Platform.",
            owner_user_id=admin.id if admin else None,
        )
        db.session.add(project)
        db.session.flush()
    elif project.owner_user_id is None and admin:
        project.owner_user_id = admin.id

    # A fresh database is migrated before bootstrap users exist. If the default
    # project has no members, attach every bootstrap user to preserve the legacy
    # Admin/Developer/Viewer development experience.
    if not ProjectMembership.query.filter_by(project_id=project.id).first():
        for user in User.query.order_by(User.id):
            db.session.add(
                ProjectMembership(
                    project_id=project.id,
                    user_id=user.id,
                    status=ProjectMembership.STATUS_ACTIVE,
                    invited_by_user_id=admin.id if admin else None,
                )
            )
    db.session.commit()
    return project


def list_accessible_projects(user: Any, *, include_archived: bool = False) -> list[Project]:
    query = Project.query
    if not include_archived:
        query = query.filter(Project.status == Project.STATUS_ACTIVE)
    if getattr(user, "is_admin", False):
        return query.order_by(Project.name, Project.id).all()
    return (
        query.join(ProjectMembership)
        .filter(
            ProjectMembership.user_id == getattr(user, "id", None),
            ProjectMembership.status == ProjectMembership.STATUS_ACTIVE,
        )
        .order_by(Project.name, Project.id)
        .all()
    )


def can_access_project(user: Any, project_id: int) -> bool:
    if getattr(user, "is_admin", False):
        return Project.query.filter_by(
            id=project_id, status=Project.STATUS_ACTIVE
        ).first() is not None
    return (
        ProjectMembership.query.join(Project)
        .filter(
            ProjectMembership.project_id == project_id,
            ProjectMembership.user_id == getattr(user, "id", None),
            ProjectMembership.status == ProjectMembership.STATUS_ACTIVE,
            Project.status == Project.STATUS_ACTIVE,
        )
        .first()
        is not None
    )


def can_manage_project(user: Any, project: Project) -> bool:
    return bool(
        getattr(user, "is_admin", False)
        or project.owner_user_id == getattr(user, "id", None)
    )


def get_accessible_project(user: Any, project_id: int) -> Project | None:
    project = db.session.get(Project, project_id)
    if project and can_access_project(user, project.id):
        return project
    return None


def get_active_project(
    user: Any, *, projects: list[Project] | None = None
) -> Project | None:
    projects = projects if projects is not None else list_accessible_projects(user)
    if not projects:
        if has_request_context():
            session.pop("active_project_id", None)
        return None
    selected_id = session.get("active_project_id") if has_request_context() else None
    selected = next((project for project in projects if project.id == selected_id), None)
    project = selected or projects[0]
    if has_request_context():
        session["active_project_id"] = project.id
    return project


def select_active_project(user: Any, project_id: int) -> Project | None:
    project = get_accessible_project(user, project_id)
    if project and project.status == Project.STATUS_ACTIVE and has_request_context():
        session["active_project_id"] = project.id
        return project
    return None


def create_project(name: str, description: str, creator: User) -> Project:
    clean_name = name.strip()
    if len(clean_name) < 3 or len(clean_name) > 120:
        raise ValueError("Tên project phải có từ 3 đến 120 ký tự.")
    slug = slugify_project(clean_name)
    if Project.query.filter_by(slug=slug).first():
        raise ValueError("Tên/slug project đã tồn tại.")
    project = Project(
        name=clean_name,
        slug=slug,
        description=description.strip()[:500],
        owner_user_id=creator.id,
    )
    try:
        db.session.add(project)
        db.session.flush()
        db.session.add(
            ProjectMembership(
                project_id=project.id,
                user_id=creator.id,
                status=ProjectMembership.STATUS_ACTIVE,
                invited_by_user_id=creator.id,
            )
        )
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        raise ValueError("Tên/slug project đã tồn tại.") from exc
    return project


def add_project_member(project: Project, identity: str, actor: User) -> ProjectMembership:
    normalized = identity.strip().lower()
    user = User.query.filter(
        or_(User.username == identity.strip(), User.email == normalized)
    ).first()
    if not user:
        raise ValueError("Không tìm thấy tài khoản với username/email đã nhập.")
    membership = ProjectMembership.query.filter_by(
        project_id=project.id, user_id=user.id
    ).first()
    try:
        if membership:
            if membership.status == ProjectMembership.STATUS_ACTIVE:
                raise ValueError("Người dùng đã là thành viên của project.")
            membership.status = ProjectMembership.STATUS_ACTIVE
            membership.invited_by_user_id = actor.id
            membership.updated_at = datetime.now(timezone.utc)
        else:
            membership = ProjectMembership(
                project_id=project.id,
                user_id=user.id,
                status=ProjectMembership.STATUS_ACTIVE,
                invited_by_user_id=actor.id,
            )
            db.session.add(membership)
        db.session.commit()
    except IntegrityError as exc:
        db.session.rollback()
        raise ValueError("Người dùng đã là thành viên của project.") from exc
    return membership


def set_membership_status(
    project: Project, membership: ProjectMembership, status: str
) -> None:
    if membership.project_id != project.id:
        raise ValueError("Membership không thuộc project.")
    if membership.user_id == project.owner_user_id:
        raise ValueError("Không thể vô hiệu hóa owner của project.")
    if status not in ProjectMembership.VALID_STATUSES:
        raise ValueError("Trạng thái membership không hợp lệ.")
    membership.status = status
    membership.updated_at = datetime.now(timezone.utc)
    db.session.commit()


def remove_project_member(project: Project, membership: ProjectMembership) -> None:
    if membership.project_id != project.id:
        raise ValueError("Membership không thuộc project.")
    if membership.user_id == project.owner_user_id:
        raise ValueError("Không thể xóa owner khỏi project.")
    db.session.delete(membership)
    db.session.commit()


def project_to_dict(project: Project) -> dict[str, Any]:
    return {
        "id": project.id,
        "name": project.name,
        "slug": project.slug,
        "description": project.description,
        "status": project.status,
        "owner_user_id": project.owner_user_id,
        "created_at": project.created_at.isoformat() if project.created_at else None,
    }
