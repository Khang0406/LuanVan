from pathlib import Path

from flask import Flask, redirect, url_for

from .config import Config
from .db import db
from .models import User


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    Path(app.config["GENERATED_INVENTORY_DIR"]).mkdir(parents=True, exist_ok=True)
    Path(app.config["K8S_GENERATED_DIR"]).mkdir(parents=True, exist_ok=True)
    Path(config_class.BASE_DIR / "instance").mkdir(parents=True, exist_ok=True)

    db.init_app(app)

    from .modules.auth.routes import auth_bp
    from .modules.servers.routes import servers_bp
    from .modules.clusters.routes import clusters_bp
    from .modules.jobs.routes import jobs_bp
    from .modules.applications.routes import applications_bp
    from .modules.deployments.routes import deployments_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(servers_bp)
    app.register_blueprint(clusters_bp)
    app.register_blueprint(jobs_bp)
    app.register_blueprint(applications_bp)
    app.register_blueprint(deployments_bp)

    @app.route("/")
    def index():
        return redirect(url_for("auth.login"))

    @app.route("/dashboard")
    def dashboard():
        from .models import Server, Cluster, Application, DeploymentRecord, AnsibleJob

        stats = {
            "servers": Server.query.count(),
            "clusters": Cluster.query.count(),
            "applications": Application.query.count(),
            "deployments": DeploymentRecord.query.count(),
            "jobs": AnsibleJob.query.count(),
        }
        return app.jinja_env.get_template("dashboard.html").render(stats=stats)

    with app.app_context():
        db.create_all()
        if not User.query.filter_by(username="admin").first():
            admin = User(username="admin", role="ADMIN")
            admin.set_password("admin")
            db.session.add(admin)
            db.session.commit()

    return app
