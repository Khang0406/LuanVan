from functools import wraps
from urllib.parse import urlsplit

from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user

from app.db import db
from app.models import User
from app.modules.audit.service import record_audit

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")


def _safe_next_url(target: str | None) -> str | None:
    if not target:
        return None
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc or not target.startswith("/") or target.startswith("//"):
        return None
    return target


def role_required(*roles: str):
    allowed = set(roles)
    if not allowed:
        raise ValueError("At least one role is required")

    def decorator(f):
        @wraps(f)
        @login_required
        def decorated_function(*args, **kwargs):
            if current_user.role not in allowed:
                required = ", ".join(sorted(allowed))
                record_audit(
                    "ACCESS_DENIED",
                    request.path,
                    "FAILED",
                    f"Yêu cầu role {required}, user role {current_user.role}.",
                )
                flash("Bạn không có quyền truy cập trang này.", "danger")
                return redirect(url_for("dashboard"))
            return f(*args, **kwargs)

        return decorated_function

    return decorator


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = User.query.filter_by(username=username).first()

        if user and user.check_password(password):
            session.clear()
            login_user(user, remember=True)
            record_audit("AUTH_LOGIN", user.username, "SUCCESS", f"User {user.username} đăng nhập thành công.", user=user)
            next_page = _safe_next_url(request.args.get("next"))
            flash(f"Đăng nhập thành công. Xin chào {user.username} ({user.role}).", "success")
            return redirect(next_page or url_for("dashboard"))

        record_audit("AUTH_LOGIN", username or "unknown", "FAILED", "Tên đăng nhập hoặc mật khẩu không đúng.", user=username or "anonymous")
        flash("Tên đăng nhập hoặc mật khẩu không đúng.", "danger")

    return render_template("auth/login.html")


@auth_bp.post("/logout")
@login_required
def logout():
    username = current_user.username
    record_audit("AUTH_LOGOUT", username, "SUCCESS", f"User {username} đăng xuất.")
    logout_user()
    flash("Đã đăng xuất.", "info")
    return redirect(url_for("auth.login"))


@auth_bp.route("/forgot-password")
def forgot_password():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    return render_template("auth/forgot_password.html")


@auth_bp.route("/change-password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        current_pw = request.form.get("current_password", "")
        new_pw = request.form.get("new_password", "")
        confirm_pw = request.form.get("confirm_password", "")

        if not current_user.check_password(current_pw):
            flash("Mật khẩu hiện tại không đúng.", "danger")
        elif len(new_pw) < 8:
            flash("Mật khẩu mới phải có ít nhất 8 ký tự.", "danger")
        elif new_pw != confirm_pw:
            flash("Mật khẩu xác nhận không khớp.", "danger")
        else:
            current_user.set_password(new_pw)
            db.session.commit()
            record_audit("AUTH_CHANGE_PASSWORD", current_user.username, "SUCCESS", "User đổi mật khẩu.")
            flash("Đổi mật khẩu thành công.", "success")
            return redirect(url_for("dashboard"))

    return render_template("auth/change_password.html")


@auth_bp.route("/register", methods=["GET", "POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")

        if not username or len(username) < 3:
            flash("Tên đăng nhập phải có ít nhất 3 ký tự.", "danger")
        elif User.query.filter_by(username=username).first():
            flash("Tên đăng nhập đã tồn tại.", "danger")
        elif len(password) < 8:
            flash("Mật khẩu phải có ít nhất 8 ký tự.", "danger")
        elif password != confirm:
            flash("Mật khẩu xác nhận không khớp.", "danger")
        else:
            user = User(username=username, role="Developer")
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            record_audit("AUTH_REGISTER", username, "SUCCESS", f"Tài khoản {username} (Developer) đã được tạo.", user=username)
            flash(f"Đăng ký thành công! Tài khoản {username} (Developer) đã được tạo.", "success")
            return redirect(url_for("auth.login"))

    return render_template("auth/register.html")
