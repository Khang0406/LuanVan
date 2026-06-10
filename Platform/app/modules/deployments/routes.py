from datetime import datetime

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for

from ...db import db
from ...models import AppService, DeploymentRecord
from ..auth.routes import login_required
from ..audit.service import record_audit
from .kubectl import kubectl, kubectl_apply_file
from .manifest import render_service_manifest, safe_name

deployments_bp = Blueprint("deployments", __name__, url_prefix="/services")


@deployments_bp.route("/<int:service_id>/deploy", methods=["POST"])
@login_required
def deploy(service_id):
    service = AppService.query.get_or_404(service_id)
    manifest_path = render_service_manifest(service, current_app.config["K8S_GENERATED_DIR"])
    record = DeploymentRecord(service_id=service.id, image=service.image, replicas=service.replicas, status="RUNNING")
    db.session.add(record)
    db.session.commit()
    code, stdout, stderr = kubectl_apply_file(manifest_path)
    record.finished_at = datetime.utcnow()
    if code == 0:
        service.status = "DEPLOYED"
        record.status = "SUCCESS"
        record.message = stdout
        if service.node_port:
            record.endpoint_url = f"http://<node-ip>:{service.node_port}"
        flash("Deploy thành công.", "success")
    else:
        service.status = "FAILED"
        record.status = "FAILED"
        record.message = stderr or stdout
        flash(record.message, "danger")
    db.session.commit()
    record_audit("SERVICE_DEPLOY", "service", service.id, result=record.status, message=record.message)
    return redirect(url_for("deployments.detail", service_id=service.id))


@deployments_bp.route("/<int:service_id>")
@login_required
def detail(service_id):
    service = AppService.query.get_or_404(service_id)
    code, stdout, stderr = kubectl(["get", "pods", "-n", safe_name(service.application.namespace), "-l", f"app={safe_name(service.name)}", "-o", "wide"])
    pod_status = stdout if code == 0 else stderr
    return render_template("deployments/detail.html", service=service, pod_status=pod_status)


@deployments_bp.route("/<int:service_id>/scale", methods=["POST"])
@login_required
def scale(service_id):
    service = AppService.query.get_or_404(service_id)
    replicas = int(request.form.get("replicas") or service.replicas)
    code, stdout, stderr = kubectl([
        "scale",
        "deployment",
        safe_name(service.name),
        "-n",
        safe_name(service.application.namespace),
        f"--replicas={replicas}",
    ])
    if code == 0:
        service.replicas = replicas
        db.session.commit()
        record_audit("SERVICE_SCALE", "service", service.id, message=f"replicas={replicas}")
        flash("Đã scale service.", "success")
    else:
        flash(stderr or stdout, "danger")
    return redirect(url_for("deployments.detail", service_id=service.id))


@deployments_bp.route("/<int:service_id>/logs")
@login_required
def logs(service_id):
    service = AppService.query.get_or_404(service_id)
    code, stdout, stderr = kubectl([
        "logs",
        "-n",
        safe_name(service.application.namespace),
        "-l",
        f"app={safe_name(service.name)}",
        "--tail=100",
    ])
    return render_template("deployments/logs.html", service=service, logs=stdout if code == 0 else stderr)
