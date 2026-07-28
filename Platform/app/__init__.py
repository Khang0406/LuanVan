import os

from flask import Flask, current_app, jsonify, redirect, render_template, url_for
from flask_login import LoginManager, current_user, login_required
from sqlalchemy import text
from werkzeug.middleware.proxy_fix import ProxyFix

from .config import Config
from .db import db
from .models import User
from .security import generate_csrf_token, validate_csrf
from .modules.auth.admin import admin_bp
from .modules.auth.routes import auth_bp
from .ui.mock_data import dashboard_stats
from .ui.routes import ui_bp
from .modules.pipeline.webhook import github_webhook_bp

login_manager = LoginManager()
login_manager.login_view = "auth.login"
login_manager.login_message = "Vui lòng đăng nhập để truy cập."
login_manager.login_message_category = "warning"


def create_app(config_class=Config):
    validate_production = getattr(config_class, "validate_production", None)
    if callable(validate_production):
        validate_production()

    app = Flask(__name__)
    app.config.from_object(config_class)
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
    login_manager.init_app(app)
    app.before_request(validate_csrf)
    app.jinja_env.globals["csrf_token"] = generate_csrf_token

    app.register_blueprint(ui_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(github_webhook_bp)

    @login_manager.user_loader
    def load_user(user_id: str):
        return db.session.get(User, int(user_id))

    with app.app_context():
        from pathlib import Path

        Path(app.instance_path).mkdir(parents=True, exist_ok=True)
        db.create_all()
        from .delivery_store import migrate_default_json_state
        from .modules.pipeline.engine import recover_interrupted_pipeline_runs

        migrate_default_json_state()
        recover_interrupted_pipeline_runs()
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
        """Readiness probe for the durable application database."""
        try:
            db.session.execute(text("SELECT 1"))
            from .delivery_store import check_database

            check_database()
        except Exception:
            app.logger.exception("Readiness database check failed")
            return jsonify({"status": "not_ready", "database": "failed"}), 503
        return jsonify({"status": "ready", "database": "ok"}), 200

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
        admin = User(username=username, role="Admin")
        admin.set_password(password)
        db.session.add(admin)
        db.session.commit()
        return

    admin = User(username="admin", role="Admin")
    admin.set_password("admin123")
    db.session.add(admin)

    dev = User(username="dev", role="Developer")
    dev.set_password("dev123")
    db.session.add(dev)

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
