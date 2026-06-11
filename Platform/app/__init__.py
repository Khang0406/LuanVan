from flask import Flask, redirect, render_template, url_for

from .config import Config
from .ui.mock_data import dashboard_stats
from .ui.routes import ui_bp


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)
    app.register_blueprint(ui_bp)

    @app.route("/")
    def index():
        return redirect(url_for("dashboard"))

    @app.route("/dashboard")
    def dashboard():
        return render_template("dashboard.html", stats=dashboard_stats())

    return app
