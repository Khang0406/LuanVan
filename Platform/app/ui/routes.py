from flask import Blueprint, render_template

from .mock_data import APPLICATIONS, AUDIT_LOGS, INSTALL_STEPS, PIPELINE_EVENTS, SERVERS

ui_bp = Blueprint("ui", __name__)


@ui_bp.route("/servers")
def servers():
    return render_template("servers/list.html", servers=SERVERS)


@ui_bp.route("/servers/new")
def server_form():
    return render_template("servers/form.html")


@ui_bp.route("/clusters")
def clusters():
    return render_template("clusters/list.html", servers=SERVERS, steps=INSTALL_STEPS)


@ui_bp.route("/clusters/install")
def cluster_install():
    return render_template("clusters/install.html", servers=SERVERS, steps=INSTALL_STEPS)


@ui_bp.route("/clusters/detail")
def cluster_detail():
    return render_template("clusters/detail.html", servers=SERVERS, steps=INSTALL_STEPS)


@ui_bp.route("/applications")
def applications():
    return render_template("applications/list.html", applications=APPLICATIONS)


@ui_bp.route("/applications/new")
def application_form():
    return render_template("applications/form.html")


@ui_bp.route("/deployments/service-form")
def service_form():
    return render_template("deployments/service_form.html")


@ui_bp.route("/deployments/detail")
def deployment_detail():
    return render_template("deployments/detail.html", applications=APPLICATIONS)


@ui_bp.route("/deployments/logs")
def deployment_logs():
    return render_template("deployments/logs.html")


@ui_bp.route("/jobs")
def jobs():
    return render_template("jobs/detail.html", steps=INSTALL_STEPS)


@ui_bp.route("/cicd")
def cicd():
    return render_template("cicd.html", events=PIPELINE_EVENTS)


@ui_bp.route("/monitoring")
def monitoring():
    return render_template("monitoring.html", servers=SERVERS, applications=APPLICATIONS)


@ui_bp.route("/audit")
def audit():
    return render_template("audit.html", logs=AUDIT_LOGS)
