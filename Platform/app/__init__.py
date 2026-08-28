import os
from datetime import datetime, timezone

from flask import Flask, current_app, flash, jsonify, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required, logout_user
from flask_security import SQLAlchemyUserDatastore
from sqlalchemy import text
from werkzeug.middleware.proxy_fix import ProxyFix

from .config import Config
from .db import assert_database_schema_current, db
from .extensions import limiter, mail, security as identity_security
from .models import SecurityRole, User
from .observability import register_http_observability
from .security import generate_csrf_token, validate_csrf
from .modules.auth.admin import admin_bp
from .modules.auth.routes import auth_bp
from .modules.audit.service import record_audit
from .ui.mock_data import dashboard_stats
from .ui.routes import ui_bp
from .modules.pipeline.webhook import github_webhook_bp
from .modules.api.routes import api_bp

def create_app(config_class=Config):
    validate_production = getattr(config_class, "validate_production", None)
    if callable(validate_production):
        validate_production()

    app = Flask(__name__)
    app.config.from_object(config_class)
    # Test/local config classes from older modules don't know about the new
    # framework settings. Safe defaults keep those isolated apps self-contained.
    app.config.setdefault(
        "SECURITY_PASSWORD_SALT", f"{app.config['SECRET_KEY']}-email-confirmation"
    )
    if not app.config.get("SECURITY_PASSWORD_SALT"):
        app.config["SECURITY_PASSWORD_SALT"] = (
            f"{app.config['SECRET_KEY']}-email-confirmation"
        )
    app.config.setdefault("SECURITY_CONFIRMABLE", True)
    app.config.setdefault("SECURITY_REGISTERABLE", True)
    app.config.setdefault("SECURITY_USERNAME_ENABLE", False)
    app.config.setdefault("SECURITY_EMAIL_VALIDATOR_ARGS", {"check_deliverability": False})
    app.config.setdefault("SECURITY_RETURN_GENERIC_RESPONSES", True)
    app.config.setdefault("SECURITY_AUTO_LOGIN_AFTER_CONFIRM", False)
    app.config.setdefault("SECURITY_SEND_REGISTER_EMAIL", False)
    app.config.setdefault("SECURITY_JOIN_USER_ROLES", False)
    app.config.setdefault("MAIL_BACKEND", "locmem" if app.config.get("TESTING") else "console")
    app.config.setdefault("RATELIMIT_STORAGE_URI", "memory://")
    proxy_count = int(app.config.get("TRUST_PROXY_COUNT", 0) or 0)
    if proxy_count:
        app.wsgi_app = ProxyFix(
            app.wsgi_app,
            x_for=proxy_count,
            x_proto=proxy_count,
            x_host=proxy_count,
            x_port=proxy_count,
        )

    db.init_app(app)
    mail.init_app(app)
    limiter.init_app(app)
    identity_security.init_app(
        app,
        SQLAlchemyUserDatastore(db, User, SecurityRole),
        register_blueprint=False,
    )
    identity_security.login_manager.login_view = "auth.login"
    identity_security.login_manager.login_message = "Vui lòng đăng nhập để truy cập."
    identity_security.login_manager.login_message_category = "warning"

    @identity_security.login_manager.unauthorized_handler
    def unauthorized():
        flash("Vui lòng đăng nhập để truy cập.", "warning")
        return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))

    @identity_security.login_manager.user_loader
    def load_user(identifier: str):
        """Load framework sessions and preserve numeric sessions from A.3.1."""
        user = User.query.filter_by(fs_uniquifier=identifier).first()
        if user is None and identifier.isdigit():
            user = db.session.get(User, int(identifier))
        return user
    register_http_observability(app)
    app.before_request(validate_csrf)
    app.jinja_env.globals["csrf_token"] = generate_csrf_token

    app.register_blueprint(ui_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(github_webhook_bp)
    app.register_blueprint(api_bp)

    @app.before_request
    def enforce_active_account():
        if current_user.is_authenticated and not current_user.is_active:
            username = current_user.username
            logout_user()
            session.clear()
            record_audit(
                "AUTH_SESSION_REJECTED",
                username,
                "FAILED",
                "Phiên bị từ chối vì tài khoản không còn Active.",
                user=username,
            )
            flash("Phiên đăng nhập không còn hiệu lực vì trạng thái tài khoản.", "warning")
            if request.endpoint != "auth.login":
                return redirect(url_for("auth.login"))

    with app.app_context():
        from pathlib import Path

        Path(app.instance_path).mkdir(parents=True, exist_ok=True)
        # Test databases remain self-contained. Runtime databases are managed
        # exclusively by Alembic; silently creating partial production schema
        # here would bypass migration history and make upgrades unsafe.
        database_url = str(app.config.get("SQLALCHEMY_DATABASE_URI", ""))
        if app.config.get("TESTING") or database_url.startswith("sqlite:"):
            db.create_all()
        else:
            assert_database_schema_current()
        from .delivery_store import migrate_default_json_state
        migrate_default_json_state()
        _seed_default_users()

    @app.route("/")
    @login_required
    def index():
        return redirect(url_for("dashboard"))

    @app.route("/dashboard")
    @login_required
    def dashboard():
        return render_template("dashboard.html", stats=dashboard_stats(current_user))

    @app.get("/healthz")
    def healthz():
        """Process liveness probe; deliberately does not call cluster services."""
        return jsonify({"status": "ok"}), 200

    @app.get("/readyz")
    def readyz():
        """Check databases, writable data directory and essential configuration."""
        try:
            from pathlib import Path

            db.session.execute(text("SELECT 1"))
            assert_database_schema_current()
            from .delivery_store import check_database, database_path

            check_database()
            redis_status = "disabled"
            if os.getenv("REDIS_URL", "").strip():
                from .redis_client import ping

                if not ping():
                    raise RuntimeError("Redis is configured but unavailable")
                redis_status = "ok"
            data_dir = Path(os.getenv("PLATFORM_DATA_DIR", database_path().parent))
            data_dir.mkdir(parents=True, exist_ok=True)
            probe = data_dir / ".readyz-write-probe"
            probe.touch(exist_ok=True)
            probe.unlink(missing_ok=True)
            if app.config.get("PLATFORM_ENV") == "production":
                required = ("SECRET_KEY", "SQLALCHEMY_DATABASE_URI")
                if any(not app.config.get(name) for name in required):
                    raise RuntimeError("essential production configuration missing")
        except Exception:
            app.logger.warning("Readiness dependency check failed")
            return jsonify({
                "status": "not_ready",
                "database": "failed",
                "data_directory": "failed",
            }), 503
        return jsonify({
            "status": "ready",
            "database": "ok",
            "data_directory": "ok",
            "configuration": "ok",
            "redis": redis_status,
        }), 200

    return app


def _seed_default_users() -> None:
    production = (
        str(current_app.config.get("PLATFORM_ENV", "development")).lower()
        == "production"
    )
    if User.query.count() > 0:
        if production:
            _rotate_known_default_passwords()
        return

    if production:
        username = os.getenv("PLATFORM_ADMIN_USERNAME", "admin").strip()
        password = os.getenv("PLATFORM_ADMIN_PASSWORD", "")
        if len(password) < 12:
            raise RuntimeError(
                "PLATFORM_ADMIN_PASSWORD must contain at least 12 characters "
                "when bootstrapping a production database."
            )
        admin = User(
            username=username,
            role="Admin",
            confirmed_at=datetime.now(timezone.utc),
        )
        admin.set_password(password)
        db.session.add(admin)
        db.session.commit()
        return

    confirmed_at = datetime.now(timezone.utc)
    admin = User(username="admin", role="Admin", confirmed_at=confirmed_at)
    admin.set_password("admin123")
    db.session.add(admin)

    dev = User(username="dev", role="Developer", confirmed_at=confirmed_at)
    dev.set_password("dev123")
    db.session.add(dev)

    viewer = User(username="viewer", role="Viewer", confirmed_at=confirmed_at)
    viewer.set_password("viewer123")
    db.session.add(viewer)

    db.session.commit()


def _rotate_known_default_passwords() -> None:
    """Rotate legacy prototype accounts during the first production start."""
    defaults = {
        "admin": ("admin123", "PLATFORM_ADMIN_PASSWORD"),
        "dev": ("dev123", "PLATFORM_DEV_PASSWORD"),
    }
    changed = False
    for username, (known_password, environment_name) in defaults.items():
        user = User.query.filter_by(username=username).first()
        if not user or not user.check_password(known_password):
            continue
        replacement = os.getenv(environment_name, "")
        if len(replacement) < 12:
            raise RuntimeError(
                f"Legacy account {username!r} still uses a prototype password. "
                f"Set {environment_name} to at least 12 characters for the "
                "first production start."
            )
        user.set_password(replacement)
        changed = True
    if changed:
        db.session.commit()
