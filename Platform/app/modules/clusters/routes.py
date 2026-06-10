from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for

from ...db import db
from ...models import Cluster, ClusterNode, Server
from ..ansible.runner import run_playbook
from ..auth.routes import login_required
from ..audit.service import record_audit
from ..deployments.kubectl import kubectl

clusters_bp = Blueprint("clusters", __name__, url_prefix="/clusters")


@clusters_bp.route("")
@login_required
def list_clusters():
    return render_template("clusters/list.html", clusters=Cluster.query.order_by(Cluster.id.desc()).all())


@clusters_bp.route("/new", methods=["GET", "POST"])
@login_required
def new_cluster():
    servers = Server.query.order_by(Server.name).all()
    if request.method == "POST":
        cluster = Cluster(name=request.form["name"].strip(), description=request.form.get("description"))
        db.session.add(cluster)
        db.session.flush()
        for server in servers:
            role = request.form.get(f"role_{server.id}")
            if role in {"master", "worker"}:
                db.session.add(ClusterNode(cluster_id=cluster.id, server_id=server.id, role=role))
        db.session.commit()
        record_audit("CLUSTER_CREATE", "cluster", cluster.id, message=f"Created cluster {cluster.name}")
        flash("Đã tạo cluster.", "success")
        return redirect(url_for("clusters.detail", cluster_id=cluster.id))
    return render_template("clusters/install.html", servers=servers)


@clusters_bp.route("/<int:cluster_id>")
@login_required
def detail(cluster_id):
    cluster = Cluster.query.get_or_404(cluster_id)
    return render_template("clusters/detail.html", cluster=cluster)


@clusters_bp.route("/<int:cluster_id>/ping", methods=["POST"])
@login_required
def ping(cluster_id):
    cluster = Cluster.query.get_or_404(cluster_id)
    job = run_playbook(cluster, "ping.yml")
    flash("Đã chạy Ansible ping.", "info")
    return redirect(url_for("jobs.detail", job_id=job.id))


@clusters_bp.route("/<int:cluster_id>/install", methods=["POST"])
@login_required
def install(cluster_id):
    cluster = Cluster.query.get_or_404(cluster_id)
    job = run_playbook(cluster, "install_k3s_cluster.yml")
    cluster.status = "READY" if job.status == "SUCCESS" else "INSTALL_FAILED"
    cluster.kubeconfig_path = current_app.config["KUBECONFIG"]
    db.session.commit()
    record_audit("CLUSTER_INSTALL", "cluster", cluster.id, result=job.status, message=job.playbook)
    return redirect(url_for("jobs.detail", job_id=job.id))


@clusters_bp.route("/<int:cluster_id>/refresh-nodes", methods=["POST"])
@login_required
def refresh_nodes(cluster_id):
    cluster = Cluster.query.get_or_404(cluster_id)
    code, stdout, stderr = kubectl(["get", "nodes", "-o", "wide"])
    if code == 0:
        cluster.status = "READY"
        flash(stdout, "success")
    else:
        cluster.status = "UNKNOWN"
        flash(stderr or stdout or "Không chạy được kubectl get nodes.", "danger")
    db.session.commit()
    return redirect(url_for("clusters.detail", cluster_id=cluster.id))
