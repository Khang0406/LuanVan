from flask import Flask, redirect, render_template, url_for
from flask_login import LoginManager, login_required

from .config import Config
from .db import db
from .models import User
from .modules.auth.admin import admin_bp
from .modules.auth.routes import auth_bp
from .ui.mock_data import dashboard_stats
from .ui.routes import ui_bp

login_manager = LoginManager()
login_manager.login_view = "auth.login"
login_manager.login_message = "Vui lòng đăng nhập để truy cập."
login_manager.login_message_category = "warning"


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    db.init_app(app)
    login_manager.init_app(app)

    app.register_blueprint(ui_bp)
    app.register_blueprint(auth_bp)
    app.register_blueprint(admin_bp)

    @login_manager.user_loader
    def load_user(user_id: str):
        return db.session.get(User, int(user_id))

    with app.app_context():
        from pathlib import Path

        Path(app.instance_path).mkdir(parents=True, exist_ok=True)
        db.create_all()
        _seed_default_users()

    @app.route("/")
    @login_required
    def index():
        return redirect(url_for("dashboard"))

    @app.route("/dashboard")
    @login_required
    def dashboard():
        return render_template("dashboard.html", stats=dashboard_stats())

    return app


def _seed_default_users() -> None:
    if User.query.count() > 0:
        return

    admin = User(username="admin", role="Admin")
    admin.set_password("admin123")
    db.session.add(admin)

    dev = User(username="dev", role="Developer")
    dev.set_password("dev123")
    db.session.add(dev)

    db.session.commit()
