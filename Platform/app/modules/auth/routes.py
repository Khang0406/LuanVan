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
    PasswordResetRateLimited,
    VerificationRateLimited,
    clear_login_failures,
    create_password_reset_token,
    create_verification_token,
    inspect_password_reset_token,
    mask_email,
    normalize_email,
    register_login_failure,
    reset_password_with_token,
    revoke_user_sessions,
    send_password_reset_email,
    send_verification_email,
    temporary_lock_remaining,
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


def _password_reset_url(raw_token: str) -> str:
    path = url_for("auth.reset_password", token=raw_token)
    public_url = str(current_app.config.get("PLATFORM_PUBLIC_URL", "")).rstrip("/")
    return f"{public_url}{path}" if public_url else url_for(
        "auth.reset_password", token=raw_token, _external=True
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
@limiter.limit("30/minute", methods=["POST"])
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

        lock_remaining = temporary_lock_remaining(user) if user else 0
        if user and lock_remaining:
            record_audit(
                "AUTH_LOGIN",
                user.username,
                "FAILED",
                "Đăng nhập bị từ chối do tài khoản đang bị khóa tạm thời.",
                user=user,
                metadata={"retry_after_seconds": lock_remaining},
            )
            flash(
                "Tài khoản đang bị khóa tạm thời do đăng nhập sai nhiều lần. "
                "Vui lòng thử lại sau.",
                "danger",
            )
            return render_template("auth/login.html"), 429

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
            if user.failed_login_count:
                clear_login_failures(user)
            session.clear()
            login_user(user, remember=True)
            record_audit("AUTH_LOGIN", user.username, "SUCCESS", f"User {user.username} đăng nhập thành công.", user=user)
            next_page = _safe_next_url(request.args.get("next"))
            flash(f"Đăng nhập thành công. Xin chào {user.username} ({user.role}).", "success")
            return redirect(next_page or url_for("dashboard"))

        failure_count = 0
        newly_locked_for = 0
        if user and user.status not in {User.STATUS_LOCKED, User.STATUS_DISABLED}:
            failure_count, newly_locked_for = register_login_failure(user)
        record_audit(
            "AUTH_LOGIN",
            getattr(user, "username", username or "unknown"),
            "FAILED",
            "Tên đăng nhập hoặc mật khẩu không đúng.",
            user=user or username or "anonymous",
            metadata={
                "failed_attempts": failure_count,
                "temporarily_locked": bool(newly_locked_for),
            },
        )
        if newly_locked_for:
            record_audit(
                "AUTH_ACCOUNT_TEMP_LOCKED",
                user.username,
                "SUCCESS",
                "Tài khoản bị khóa tạm thời do đăng nhập sai liên tiếp.",
                user=user,
                metadata={"lockout_seconds": newly_locked_for},
            )
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


@auth_bp.route("/forgot-password", methods=["GET", "POST"])
@limiter.limit("10/hour", methods=["POST"])
def forgot_password():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    if request.method == "POST":
        email = normalize_email(request.form.get("email", ""))
        user = User.query.filter_by(email=email).first() if valid_email(email) else None
        if user and user.status == User.STATUS_ACTIVE:
            try:
                raw_token = create_password_reset_token(user, _request_ip())
                sent, reason = send_password_reset_email(
                    user, raw_token, _password_reset_url(raw_token)
                )
            except PasswordResetRateLimited as exc:
                sent, reason = False, str(exc)
            record_audit(
                "AUTH_PASSWORD_RESET_REQUESTED",
                user.username,
                "SUCCESS" if sent else "FAILED",
                reason,
                user=user,
                metadata={"email_domain": email.rsplit("@", 1)[-1]},
            )
        else:
            record_audit(
                "AUTH_PASSWORD_RESET_REQUESTED",
                "unknown",
                "SUCCESS",
                "Phản hồi trung tính cho email không tồn tại/không hoạt động.",
            )
        flash(
            "Nếu email thuộc tài khoản đang hoạt động và chưa vượt giới hạn, "
            "hệ thống đã gửi liên kết đặt lại mật khẩu.",
            "info",
        )
        return redirect(url_for("auth.login"))
    return render_template("auth/forgot_password.html")


@auth_bp.route("/reset-password", methods=["GET", "POST"])
@limiter.limit("20/hour", methods=["POST"])
def reset_password():
    if current_user.is_authenticated:
        return redirect(url_for("dashboard"))
    raw_token = request.values.get("token", "")
    user, outcome = inspect_password_reset_token(raw_token)
    if outcome != "valid" or not user:
        record_audit(
            "AUTH_PASSWORD_RESET",
            getattr(user, "username", "unknown"),
            "FAILED",
            f"Token đặt lại mật khẩu không hợp lệ: {outcome}.",
            user=user or "anonymous",
        )
        flash("Liên kết đặt lại mật khẩu không hợp lệ, đã dùng hoặc hết hạn.", "danger")
        return redirect(url_for("auth.forgot_password"))

    if request.method == "POST":
        new_password = request.form.get("new_password", "")
        confirm = request.form.get("confirm_password", "")
        if len(new_password) < 8:
            flash("Mật khẩu mới phải có ít nhất 8 ký tự.", "danger")
        elif new_password != confirm:
            flash("Mật khẩu xác nhận không khớp.", "danger")
        else:
            reset_user, reset_outcome = reset_password_with_token(
                raw_token, new_password
            )
            if reset_outcome == "reset" and reset_user:
                record_audit(
                    "AUTH_PASSWORD_RESET",
                    reset_user.username,
                    "SUCCESS",
                    "Mật khẩu được đặt lại; toàn bộ phiên cũ đã bị thu hồi.",
                    user=reset_user,
                )
                flash("Đặt lại mật khẩu thành công. Hãy đăng nhập lại.", "success")
                return redirect(url_for("auth.login"))
            flash("Liên kết đặt lại mật khẩu không còn hiệu lực.", "danger")
            return redirect(url_for("auth.forgot_password"))
    return render_template("auth/reset_password.html", reset_token=raw_token)


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
            user = current_user._get_current_object()
            username = user.username
            current_user.set_password(new_pw)
            revoke_user_sessions(user)
            clear_login_failures(user, commit=False)
            db.session.commit()
            record_audit(
                "AUTH_CHANGE_PASSWORD",
                username,
                "SUCCESS",
                "User đổi mật khẩu; toàn bộ phiên cũ đã bị thu hồi.",
                user=user,
            )
            logout_user()
            session.clear()
            flash("Đổi mật khẩu thành công. Hãy đăng nhập lại.", "success")
            return redirect(url_for("auth.login"))

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
