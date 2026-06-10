from flask import Blueprint, flash, redirect, render_template, request, url_for

from ...db import db
from ...models import Application, AppService
from ..auth.routes import login_required
from ..audit.service import record_audit

applications_bp = Blueprint("applications", __name__, url_prefix="/applications")


@applications_bp.route("")
@login_required
def list_applications():
    apps = Application.query.order_by(Application.id.desc()).all()
    return render_template("applications/list.html", applications=apps)


@applications_bp.route("/new", methods=["GET", "POST"])
@login_required
def new_application():
    if request.method == "POST":
        app = Application(
            name=request.form["name"].strip(),
            namespace=request.form["namespace"].strip(),
            description=request.form.get("description"),
            owner=request.form.get("owner") or "developer",
        )
        db.session.add(app)
        db.session.commit()
        record_audit("APPLICATION_CREATE", "application", app.id, message=app.name)
        flash("Đã tạo application.", "success")
        return redirect(url_for("applications.detail", app_id=app.id))
    return render_template("applications/form.html")


@applications_bp.route("/<int:app_id>")
@login_required
def detail(app_id):
    app = Application.query.get_or_404(app_id)
    return render_template("applications/list.html", applications=[app], selected=app)


@applications_bp.route("/<int:app_id>/services/new", methods=["GET", "POST"])
@login_required
def new_service(app_id):
    app = Application.query.get_or_404(app_id)
    if request.method == "POST":
        service = AppService(
            application_id=app.id,
            name=request.form["name"].strip(),
            image=request.form["image"].strip(),
            port=int(request.form.get("port") or 80),
            replicas=int(request.form.get("replicas") or 1),
            service_type=request.form.get("service_type") or "NodePort",
            node_port=int(request.form["node_port"]) if request.form.get("node_port") else None,
        )
        db.session.add(service)
        db.session.commit()
        record_audit("SERVICE_CREATE", "service", service.id, message=service.name)
        flash("Đã tạo service.", "success")
        return redirect(url_for("applications.detail", app_id=app.id))
    return render_template("deployments/service_form.html", application=app)
