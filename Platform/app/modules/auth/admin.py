import secrets

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.db import db
from app.models import Project, SubscriptionUpgradeRequest, User
from app.modules.audit.service import record_audit
from app.modules.auth.routes import role_required
from app.modules.auth.service import clear_login_failures, revoke_user_sessions

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


def _sync_project_quota_after_plan_change(project):
    from app.modules.quotas.kubernetes import (
        quota_sync_enabled,
        sync_project_quota,
    )

    if not quota_sync_enabled():
        return None
    result = sync_project_quota(project)
    record_audit(
        "KUBERNETES_QUOTA_SYNC",
        project.slug,
        "SUCCESS" if result["status"] == "Synced" else "FAILED",
        result["message"],
        metadata={"project_id": project.id, "status": result["status"]},
    )
    return result


@admin_bp.route("/users")
@login_required
@role_required("Admin")
def user_list():
    users = User.query.order_by(User.created_at.desc()).all()
    return render_template("admin/users.html", users=users)


@admin_bp.route("/users/<int:user_id>/reset", methods=["POST"])
@login_required
@role_required("Admin")
def user_reset(user_id):
    user = db.session.get(User, user_id)
    if not user:
        record_audit("USER_RESET_PASSWORD", f"user-{user_id}", "FAILED", "Không tìm thấy user.")
        flash("Không tìm thấy user.", "danger")
        return redirect(url_for("admin.user_list"))

    if user.id == current_user.id:
        record_audit("USER_RESET_PASSWORD", user.username, "FAILED", "Admin cố tự reset mật khẩu chính mình.")
        flash("Không thể tự reset mật khẩu của chính mình. Dùng chức năng Đổi mật khẩu.", "warning")
        return redirect(url_for("admin.user_list"))

    new_password = secrets.token_urlsafe(12)
    user.set_password(new_password)
    revoke_user_sessions(user)
    clear_login_failures(user, commit=False)
    db.session.commit()
    record_audit(
        "USER_RESET_PASSWORD",
        user.username,
        "SUCCESS",
        f"Admin reset mật khẩu cho {user.username}; toàn bộ phiên cũ đã bị thu hồi.",
    )
    flash(f"Đã reset mật khẩu của {user.username}. Mật khẩu mới: {new_password}", "success")
    return redirect(url_for("admin.user_list"))


@admin_bp.get("/subscriptions")
@login_required
@role_required("Admin")
def subscription_requests():
    from app.modules.subscriptions.service import (
        LIMIT_FIELDS,
        list_plans,
        list_upgrade_requests,
    )

    from app.modules.applications.service import load_applications
    from app.modules.quotas.service import build_quota_dashboard

    projects = Project.query.order_by(Project.name).all()
    applications = load_applications()
    project_dashboards = [
        {
            "project": project,
            "dashboard": build_quota_dashboard(project, applications=applications),
        }
        for project in projects
    ]
    project_dashboards.sort(
        key=lambda item: (
            item["dashboard"]["summary"]["exhausted_count"] == 0,
            item["dashboard"]["summary"]["critical_count"] == 0,
            item["project"].name.lower(),
        )
    )

    return render_template(
        "admin/subscriptions.html",
        requests=list_upgrade_requests(),
        projects=projects,
        project_dashboards=project_dashboards,
        plans=list_plans(),
        limit_fields=LIMIT_FIELDS,
    )


@admin_bp.post("/subscriptions/requests/<int:request_id>/review")
@login_required
@role_required("Admin")
def subscription_request_review(request_id: int):
    from app.modules.subscriptions.service import SubscriptionError, review_upgrade_request

    decision = request.form.get("decision", "")
    try:
        item = review_upgrade_request(
            request_id,
            current_user,
            decision,
            request.form.get("admin_note", ""),
        )
    except SubscriptionError as exc:
        record_audit(
            "SUBSCRIPTION_REQUEST_REVIEW",
            str(request_id),
            "FAILED",
            str(exc),
        )
        flash(str(exc), "danger")
    else:
        record_audit(
            "SUBSCRIPTION_REQUEST_REVIEW",
            str(item.id),
            "SUCCESS",
            f"Yêu cầu được {item.status.lower()}.",
            metadata={
                "project_id": item.project_id,
                "request_id": item.id,
                "decision": decision,
                "plan": item.requested_plan.key,
            },
        )
        flash(f"Đã cập nhật yêu cầu thành {item.status}.", "success")
        if item.status == SubscriptionUpgradeRequest.STATUS_APPROVED:
            sync_result = _sync_project_quota_after_plan_change(item.project)
            if sync_result and sync_result["status"] != "Synced":
                flash(sync_result["message"], "warning")
    return redirect(url_for("admin.subscription_requests"))


@admin_bp.post("/subscriptions/projects/assign")
@login_required
@role_required("Admin")
def subscription_assign():
    try:
        project_id = int(request.form.get("project_id", ""))
    except (TypeError, ValueError):
        project_id = 0
    project = db.session.get(Project, project_id)
    if project is None:
        flash("Project không tồn tại.", "danger")
        return redirect(url_for("admin.subscription_requests"))
    from app.modules.subscriptions.service import (
        SubscriptionError,
        assign_plan,
        subscription_limits,
    )

    try:
        subscription = assign_plan(
            project,
            current_user,
            request.form.get("plan", ""),
            request.form,
        )
    except SubscriptionError as exc:
        record_audit(
            "SUBSCRIPTION_ADMIN_ASSIGN",
            project.slug,
            "FAILED",
            str(exc),
            metadata={"project_id": project.id},
        )
        flash(str(exc), "danger")
    else:
        record_audit(
            "SUBSCRIPTION_ADMIN_ASSIGN",
            project.slug,
            "SUCCESS",
            f"Admin gán gói {subscription.plan.name}.",
            metadata={
                "project_id": project.id,
                "plan": subscription.plan.key,
                "limits": subscription_limits(subscription),
            },
        )
        flash(f"Đã gán gói {subscription.plan.name} cho {project.name}.", "success")
        sync_result = _sync_project_quota_after_plan_change(project)
        if sync_result and sync_result["status"] != "Synced":
            flash(sync_result["message"], "warning")
    return redirect(url_for("admin.subscription_requests"))
