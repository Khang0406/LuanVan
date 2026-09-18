from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.db import db
from app.models import ProjectMembership
from app.modules.audit.service import record_audit
from app.modules.auth.routes import role_required
from app.modules.projects.service import (
    add_project_member,
    can_manage_project,
    create_project,
    get_accessible_project,
    list_accessible_projects,
    remove_project_member,
    select_active_project,
    set_membership_status,
)


projects_bp = Blueprint("projects", __name__, url_prefix="/projects")


@projects_bp.get("")
@login_required
def project_list():
    return render_template(
        "projects/list.html", projects=list_accessible_projects(current_user)
    )


@projects_bp.route("/new", methods=["GET", "POST"])
@login_required
@role_required("Admin", "Developer")
def project_create():
    if request.method == "POST":
        try:
            project = create_project(
                request.form.get("name", ""),
                request.form.get("description", ""),
                current_user,
            )
        except ValueError as exc:
            record_audit("PROJECT_CREATE", request.form.get("name", ""), "FAILED", str(exc))
            flash(str(exc), "danger")
            return render_template("projects/form.html", form=request.form), 409
        select_active_project(current_user, project.id)
        record_audit(
            "PROJECT_CREATE",
            project.slug,
            "SUCCESS",
            f"Đã tạo project {project.name}.",
            metadata={"project_id": project.id},
        )
        flash(f"Đã tạo project {project.name}.", "success")
        return redirect(url_for("projects.project_detail", project_id=project.id))
    return render_template("projects/form.html", form={})


@projects_bp.post("/select")
@login_required
def project_select():
    try:
        project_id = int(request.form.get("project_id", ""))
    except (TypeError, ValueError):
        abort(400)
    project = select_active_project(current_user, project_id)
    if not project:
        record_audit("PROJECT_SELECT", str(project_id), "FAILED", "Project không thuộc phạm vi user.")
        abort(403)
    record_audit(
        "PROJECT_SELECT",
        project.slug,
        "SUCCESS",
        f"Chuyển project hiện tại sang {project.name}.",
        metadata={"project_id": project.id},
    )
    return redirect(url_for("ui.applications"))


@projects_bp.get("/<int:project_id>")
@login_required
def project_detail(project_id: int):
    project = get_accessible_project(current_user, project_id)
    if not project:
        abort(404)
    return render_template(
        "projects/detail.html",
        project=project,
        can_manage=can_manage_project(current_user, project),
    )


@projects_bp.post("/<int:project_id>/members")
@login_required
def project_member_add(project_id: int):
    project = get_accessible_project(current_user, project_id)
    if not project:
        abort(404)
    if not can_manage_project(current_user, project):
        abort(403)
    try:
        membership = add_project_member(
            project, request.form.get("identity", ""), current_user
        )
    except ValueError as exc:
        record_audit("PROJECT_MEMBER_ADD", project.slug, "FAILED", str(exc))
        flash(str(exc), "danger")
    else:
        record_audit(
            "PROJECT_MEMBER_ADD",
            project.slug,
            "SUCCESS",
            f"Đã thêm {membership.user.username} vào project.",
            metadata={"project_id": project.id, "member_user_id": membership.user_id},
        )
        flash(f"Đã thêm {membership.user.username} vào project.", "success")
    return redirect(url_for("projects.project_detail", project_id=project.id))


@projects_bp.post("/<int:project_id>/members/<int:membership_id>/status")
@login_required
def project_member_status(project_id: int, membership_id: int):
    project = get_accessible_project(current_user, project_id)
    if not project:
        abort(404)
    if not can_manage_project(current_user, project):
        abort(403)
    membership = db.session.get(ProjectMembership, membership_id)
    if not membership or membership.project_id != project.id:
        abort(404)
    status = request.form.get("status", "")
    try:
        set_membership_status(project, membership, status)
    except ValueError as exc:
        record_audit(
            "PROJECT_MEMBER_STATUS",
            project.slug,
            "FAILED",
            str(exc),
            metadata={"project_id": project.id, "member_user_id": membership.user_id},
        )
        flash(str(exc), "danger")
    else:
        record_audit(
            "PROJECT_MEMBER_STATUS",
            project.slug,
            "SUCCESS",
            f"Đổi trạng thái {membership.user.username} thành {status}.",
            metadata={"project_id": project.id, "member_user_id": membership.user_id},
        )
        flash("Đã cập nhật trạng thái thành viên.", "success")
    return redirect(url_for("projects.project_detail", project_id=project.id))


@projects_bp.post("/<int:project_id>/members/<int:membership_id>/remove")
@login_required
def project_member_remove(project_id: int, membership_id: int):
    project = get_accessible_project(current_user, project_id)
    if not project:
        abort(404)
    if not can_manage_project(current_user, project):
        abort(403)
    membership = db.session.get(ProjectMembership, membership_id)
    if not membership or membership.project_id != project.id:
        abort(404)
    username = membership.user.username
    user_id = membership.user_id
    try:
        remove_project_member(project, membership)
    except ValueError as exc:
        record_audit(
            "PROJECT_MEMBER_REMOVE",
            project.slug,
            "FAILED",
            str(exc),
            metadata={"project_id": project.id, "member_user_id": user_id},
        )
        flash(str(exc), "danger")
    else:
        record_audit(
            "PROJECT_MEMBER_REMOVE",
            project.slug,
            "SUCCESS",
            f"Đã xóa {username} khỏi project.",
            metadata={"project_id": project.id, "member_user_id": user_id},
        )
        flash(f"Đã xóa {username} khỏi project.", "success")
    return redirect(url_for("projects.project_detail", project_id=project.id))
