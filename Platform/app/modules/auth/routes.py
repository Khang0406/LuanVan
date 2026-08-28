from functools import wraps
from urllib.parse import urlsplit

from flask import Blueprint, current_app, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, login_user, logout_user
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError

from app.db import db
from app.extensions import limiter
from app.models import User
from app.modules.audit.service import record_audit
from app.modules.auth.service import (
    VerificationRateLimited,
    create_verification_token,
    mask_email,
    normalize_email,
    send_verification_email,
    valid_email,
    verify_token,
)

auth_bp = Blueprint("auth", __name__, url_prefix="/auth")


def _safe_next_url(target: str | None) -> str | None:
    if not target:
        return None
    parsed = urlsplit(target)
    if parsed.scheme or parsed.netloc or not target.startswith("/") or target.startswith("//"):
        return None
    return target


def _verification_url(raw_token: str) -> str:
    path = url_for("auth.verify_email", token=raw_token)
    public_url = str(current_app.config.get("PLATFORM_PUBLIC_URL", "")).rstrip("/")
    return f"{public_url}{path}" if public_url else url_for(
        "auth.verify_email", token=raw_token, _external=True
    )


def _request_ip() -> str:
    # ProxyFix has already normalized remote_addr when TRUST_PROXY_COUNT is set.
    return (request.remote_addr or "")[:45]


def _dispatch_verification(user: User) -> tuple[bool, str]:
    raw_token = create_verification_token(user, _request_ip())
    return send_verification_email(user, raw_token, _verification_url(raw_token))


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
        normalized = normalize_email(username)
        user = User.query.filter(
            or_(User.username == username, User.email == normalized)
        ).first()

        if user and user.check_password(password):
            if user.status == User.STATUS_PENDING:
                session["verification_email_hint"] = user.email
                record_audit(
                    "AUTH_LOGIN",
                    user.username,
                    "FAILED",
                    "Tài khoản chưa xác minh email.",
                    user=user,
                )
                flash("Tài khoản chưa được xác minh email. Hãy gửi lại liên kết xác minh.", "warning")
                return redirect(url_for("auth.resend_verification"))
            if user.status in {User.STATUS_LOCKED, User.STATUS_DISABLED}:
                record_audit(
                    "AUTH_LOGIN", user.username, "FAILED",
                    f"Tài khoản ở trạng thái {user.status}.", user=user,
                )
                flash("Tài khoản hiện không được phép đăng nhập.", "danger")
                return render_template("auth/login.html"), 403
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
@limiter.limit("10/hour", methods=["POST"])
def register():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        email = normalize_email(request.form.get("email", ""))
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")

        if not username or len(username) < 3:
            flash("Tên đăng nhập phải có ít nhất 3 ký tự.", "danger")
        elif not valid_email(email):
            flash("Địa chỉ email không hợp lệ.", "danger")
        elif User.query.filter_by(username=username).first():
            flash("Tên đăng nhập đã tồn tại.", "danger")
        elif User.query.filter_by(email=email).first():
            flash("Địa chỉ email đã được sử dụng.", "danger")
        elif len(password) < 8:
            flash("Mật khẩu phải có ít nhất 8 ký tự.", "danger")
        elif password != confirm:
            flash("Mật khẩu xác nhận không khớp.", "danger")
        else:
            user = User(
                username=username,
                email=email,
                role="Developer",
                status=User.STATUS_PENDING,
                active=False,
            )
            user.set_password(password)
            db.session.add(user)
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
                flash("Tên đăng nhập hoặc email đã được sử dụng.", "danger")
                return render_template("auth/register.html"), 409
            record_audit("AUTH_REGISTER", username, "SUCCESS", f"Tài khoản {username} (Developer) đã được tạo.", user=username)
            try:
                sent, reason = _dispatch_verification(user)
            except VerificationRateLimited as exc:
                sent, reason = False, str(exc)
            record_audit(
                "AUTH_EMAIL_VERIFICATION_SENT",
                username,
                "SUCCESS" if sent else "FAILED",
                reason,
                user=user,
                metadata={"email_domain": email.rsplit("@", 1)[-1]},
            )
            return render_template(
                "auth/verification_sent.html",
                masked_email=mask_email(email),
                sent=sent,
            )

    return render_template("auth/register.html")


@auth_bp.route("/resend-verification", methods=["GET", "POST"])
@limiter.limit("20/hour", methods=["POST"])
def resend_verification():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))

    hinted_email = session.pop("verification_email_hint", "")
    if request.method == "POST":
        email = normalize_email(request.form.get("email", ""))
        user = User.query.filter_by(email=email).first() if valid_email(email) else None
        sent = False
        reason = "Yêu cầu được tiếp nhận."
        if user and user.status == User.STATUS_PENDING:
            try:
                sent, reason = _dispatch_verification(user)
            except VerificationRateLimited as exc:
                reason = str(exc)
            record_audit(
                "AUTH_EMAIL_VERIFICATION_RESEND",
                user.username,
                "SUCCESS" if sent else "FAILED",
                reason,
                user=user,
            )
        else:
            record_audit(
                "AUTH_EMAIL_VERIFICATION_RESEND",
                "unknown",
                "SUCCESS",
                "Phản hồi trung tính cho email không tồn tại/đã xác minh.",
            )
        flash(
            "Nếu email thuộc tài khoản đang chờ xác minh và chưa vượt giới hạn, "
            "hệ thống đã gửi một liên kết mới.",
            "info",
        )
        return redirect(url_for("auth.login"))

    return render_template("auth/resend_verification.html", email=hinted_email)


@auth_bp.get("/verify-email")
def verify_email():
    user, outcome = verify_token(request.args.get("token", ""))
    if outcome == "verified" and user:
        record_audit(
            "AUTH_EMAIL_VERIFIED",
            user.username,
            "SUCCESS",
            "Email được xác minh bằng token một lần.",
            user=user,
        )
        flash("Xác minh email thành công. Bạn có thể đăng nhập.", "success")
        return redirect(url_for("auth.login"))

    record_audit(
        "AUTH_EMAIL_VERIFIED",
        getattr(user, "username", "unknown"),
        "FAILED",
        f"Token xác minh không hợp lệ: {outcome}.",
        user=user or "anonymous",
    )
    flash("Liên kết xác minh không hợp lệ, đã sử dụng hoặc đã hết hạn.", "danger")
    return redirect(url_for("auth.resend_verification"))
