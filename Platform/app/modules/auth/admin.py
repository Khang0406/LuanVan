import secrets

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.db import db
from app.models import User
from app.modules.audit.service import record_audit
from app.modules.auth.routes import role_required

admin_bp = Blueprint("admin", __name__, url_prefix="/admin")


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

    new_password = secrets.token_hex(4)
    user.set_password(new_password)
    db.session.commit()
    record_audit("USER_RESET_PASSWORD", user.username, "SUCCESS", f"Admin reset mật khẩu cho {user.username}.")
    flash(f"Đã reset mật khẩu của {user.username}. Mật khẩu mới: {new_password}", "success")
    return redirect(url_for("admin.user_list"))
