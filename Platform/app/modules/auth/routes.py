from functools import wraps

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user

from app.db import db
from app.models import User

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")


def role_required(role: str):
    def decorator(f):
        @wraps(f)
        @login_required
        def decorated_function(*args, **kwargs):
            if current_user.role != role:
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
            login_user(user, remember=True)
            next_page = request.args.get("next")
            flash(f"Đăng nhập thành công. Xin chào {user.username} ({user.role}).", "success")
            return redirect(next_page or url_for("dashboard"))

        flash("Tên đăng nhập hoặc mật khẩu không đúng.", "danger")

    return render_template("auth/login.html")


@auth_bp.route("/logout")
@login_required
def logout():
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
        elif len(new_pw) < 4:
            flash("Mật khẩu mới phải có ít nhất 4 ký tự.", "danger")
        elif new_pw != confirm_pw:
            flash("Mật khẩu xác nhận không khớp.", "danger")
        else:
            current_user.set_password(new_pw)
            db.session.commit()
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
        elif len(password) < 4:
            flash("Mật khẩu phải có ít nhất 4 ký tự.", "danger")
        elif password != confirm:
            flash("Mật khẩu xác nhận không khớp.", "danger")
        else:
            user = User(username=username, role="Developer")
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            flash(f"Đăng ký thành công! Tài khoản {username} (Developer) đã được tạo.", "success")
            return redirect(url_for("auth.login"))

    return render_template("auth/register.html")
