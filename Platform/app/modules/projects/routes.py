from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.db import db
from app.models import ProjectMembership
from app.modules.audit.service import record_audit
from app.modules.auth.routes import role_required
from app.modules.authorization.service import (
    MEMBER_MANAGE,
    PERMISSIONS,
    PROJECT_MANAGE,
    has_permission,
    permission_keys,
    role_options,
)
from app.modules.projects.service import (
    add_project_member,
    can_manage_project,
    create_project,
    get_accessible_project,
    list_accessible_projects,
    remove_project_member,
    select_active_project,
    set_membership_role,
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
        roles=role_options(),
    )


@projects_bp.route("/<int:project_id>/subscription", methods=["GET", "POST"])
@login_required
def project_subscription(project_id: int):
    project = get_accessible_project(current_user, project_id)
    if not project:
        abort(404)
    from app.modules.subscriptions.service import (
        LIMIT_FIELDS,
        SubscriptionError,
        decode_limits,
        get_project_subscription,
        list_plans,
        list_project_requests,
        list_subscription_history,
        plan_limits,
        submit_upgrade_request,
        subscription_limits,
    )

    can_request = has_permission(current_user, PROJECT_MANAGE, project.id)
    if request.method == "POST":
        if not can_request:
            abort(403)
        try:
            item = submit_upgrade_request(
                project,
                current_user,
                request.form.get("plan", ""),
                request.form.get("reason", ""),
                request.form,
            )
        except SubscriptionError as exc:
            record_audit(
                "SUBSCRIPTION_REQUEST_CREATE",
                project.slug,
                "FAILED",
                str(exc),
                metadata={"project_id": project.id},
            )
            flash(str(exc), "danger")
        else:
            record_audit(
                "SUBSCRIPTION_REQUEST_CREATE",
                str(item.id),
                "SUCCESS",
                f"Đã gửi yêu cầu gói {item.requested_plan.name}.",
                metadata={
                    "project_id": project.id,
                    "request_id": item.id,
                    "requested_plan": item.requested_plan.key,
                },
            )
            flash("Đã gửi yêu cầu thay đổi gói để Platform Admin xem xét.", "success")
            return redirect(url_for("projects.project_subscription", project_id=project.id))

    subscription = get_project_subscription(project)
    return render_template(
        "projects/subscription.html",
        project=project,
        subscription=subscription,
        current_limits=subscription_limits(subscription),
        plans=list_plans(),
        plan_limits=plan_limits,
        decode_limits=decode_limits,
        limit_fields=LIMIT_FIELDS,
        requests=list_project_requests(project.id),
        history=list_subscription_history(project.id),
        can_request=can_request,
    )


@projects_bp.route("/<int:project_id>/tokens", methods=["GET", "POST"])
@login_required
def project_tokens(project_id: int):
    project = get_accessible_project(current_user, project_id)
    if not project:
        abort(404)
    from app.modules.api_tokens.service import (
        ApiTokenError,
        create_api_token,
        list_user_api_tokens,
        token_scopes,
    )

    new_token = None
    if request.method == "POST":
        try:
            token, new_token = create_api_token(
                current_user,
                project,
                request.form.get("name", ""),
                request.form.getlist("scopes"),
                expires_in_days=int(request.form.get("expires_in_days", "90")),
            )
        except (ApiTokenError, ValueError) as exc:
            record_audit(
                "API_TOKEN_CREATE",
                project.slug,
                "FAILED",
                str(exc),
                metadata={"project_id": project.id},
            )
            flash(str(exc), "danger")
        else:
            record_audit(
                "API_TOKEN_CREATE",
                str(token.id),
                "SUCCESS",
                f"Đã tạo API token {token.name}.",
                metadata={
                    "project_id": project.id,
                    "token_id": token.id,
                    "scopes": sorted(token_scopes(token)),
                    "expires_at": token.expires_at.isoformat(),
                },
            )
            flash("Đã tạo API token. Hãy sao chép ngay vì token chỉ hiển thị một lần.", "success")

    granted = permission_keys(current_user, project.id)
    return render_template(
        "projects/tokens.html",
        project=project,
        tokens=list_user_api_tokens(current_user, project.id),
        token_scopes=token_scopes,
        scope_options=[
            (key, description) for key, description in PERMISSIONS.items() if key in granted
        ],
        new_token=new_token,
    )


@projects_bp.post("/<int:project_id>/tokens/<int:token_id>/revoke")
@login_required
def project_token_revoke(project_id: int, token_id: int):
    project = get_accessible_project(current_user, project_id)
    if not project:
        abort(404)
    from app.modules.api_tokens.service import ApiTokenError, revoke_api_token

    try:
        token = revoke_api_token(current_user, project.id, token_id)
    except ApiTokenError:
        abort(404)
    record_audit(
        "API_TOKEN_REVOKE",
        str(token.id),
        "SUCCESS",
        f"Đã thu hồi API token {token.name}.",
        metadata={"project_id": project.id, "token_id": token.id},
    )
    flash(f"Đã thu hồi API token {token.name}.", "success")
    return redirect(url_for("projects.project_tokens", project_id=project.id))


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
            project,
            request.form.get("identity", ""),
            current_user,
            request.form.get("role", "viewer"),
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
            metadata={
                "project_id": project.id,
                "member_user_id": membership.user_id,
                "role": membership.role.key,
            },
        )
        flash(f"Đã thêm {membership.user.username} vào project.", "success")
    return redirect(url_for("projects.project_detail", project_id=project.id))


@projects_bp.post("/<int:project_id>/members/<int:membership_id>/role")
@login_required
def project_member_role(project_id: int, membership_id: int):
    project = get_accessible_project(current_user, project_id)
    if not project:
        abort(404)
    if not has_permission(current_user, MEMBER_MANAGE, project.id):
        abort(403)
    membership = db.session.get(ProjectMembership, membership_id)
    if not membership or membership.project_id != project.id:
        abort(404)
    try:
        old_role, new_role = set_membership_role(
            project, membership, request.form.get("role", "")
        )
    except ValueError as exc:
        record_audit(
            "PROJECT_MEMBER_ROLE",
            project.slug,
            "FAILED",
            str(exc),
            metadata={"project_id": project.id, "member_user_id": membership.user_id},
        )
        flash(str(exc), "danger")
    else:
        record_audit(
            "PROJECT_MEMBER_ROLE",
            project.slug,
            "SUCCESS",
            f"Đổi role {membership.user.username}: {old_role} → {new_role}.",
            metadata={
                "project_id": project.id,
                "member_user_id": membership.user_id,
                "old_role": old_role,
                "new_role": new_role,
            },
        )
        flash(f"Đã đổi role thành {new_role}.", "success")
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
