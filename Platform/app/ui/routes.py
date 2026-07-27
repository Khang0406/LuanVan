import json
from typing import Any

from flask import Blueprint, abort, current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.modules.applications.service import (
    build_pipeline_steps,
    create_application,
    delete_application,
    find_accessible_application,
    load_accessible_applications,
    load_applications,
    summarize_runtime,
)
from app.modules.clusters.service import build_cluster_inventory, install_kubernetes, load_clusters
from app.modules.deployments.kubectl import (
    delete_application_workloads,
    deploy_application,
    get_application_logs,
    get_application_status,
    restart_application,
    scale_application,
)
from app.modules.servers.service import (
    add_server,
    bootstrap_multiple_servers,
    bootstrap_sudo_nopasswd,
    find_server,
    load_servers,
    ping_scan_network,
    scan_network,
    test_ansible_ping,
    test_multiple_servers,
    test_ssh,
    test_sudo_nopasswd,
)

from app.modules.pipeline.engine import (
    get_latest_pipeline_run,
    load_all_pipeline_events,
    load_pipeline_runs,
    trigger_pipeline,
)
from app.modules.monitoring.collector import (
    application_metrics,
    cluster_summary,
    get_cached_data,
    get_chart_history,
    node_metrics,
    record_snapshot,
    save_snapshot,
    start_background_collector,
)
from app.modules.monitoring.alerting import (
    acknowledge_alert,
    alert_summary,
    collect_and_persist,
    get_prometheus_app_metrics,
    get_prometheus_node_metrics,
    load_alerts,
)
from app.modules.monitoring.grafana import get_grafana_embed_url
from app.modules.monitoring.k8s_manifests import deploy_monitoring_stack
from app.modules.auth.routes import role_required
from app.modules.audit.service import load_audit_logs, record_audit
from app.modules.jobs.service import load_accessible_jobs, pipeline_run_to_job, record_completed_job

from .mock_data import INSTALL_STEPS

ui_bp = Blueprint("ui", __name__)


def _get_authorized_application(application_id: str) -> dict[str, Any]:
    application = find_accessible_application(application_id, current_user)
    if not application:
        abort(404)
    return application


def _result_label(success: bool) -> str:
    return "SUCCESS" if success else "FAILED"


def _server_output(server_id: str, fallback: str = "") -> str:
    server = find_server(server_id)
    return (server or {}).get("last_output", "") or fallback


@ui_bp.route("/servers")
@login_required
@role_required("Admin")
def servers():
    return render_template("servers/list.html", servers=load_servers())


@ui_bp.route("/servers/<server_id>")
@login_required
@role_required("Admin")
def server_detail(server_id):
    server = find_server(server_id)
    if not server:
        abort(404)
    return render_template("servers/detail.html", server=server)


@ui_bp.route("/servers/new", methods=["GET", "POST"])
@login_required
@role_required("Admin")
def server_form():
    if request.method == "POST":
        required_fields = ["name", "ip", "ssh_user", "role"]
        missing_fields = [field for field in required_fields if not request.form.get(field, "").strip()]
        if missing_fields:
            flash("Vui lòng nhập đầy đủ tên máy, IP, SSH user và vai trò.", "danger")
            return render_template("servers/form.html", form=request.form)

        server = add_server(request.form)
        record_audit("SERVER_CREATE", server["id"], "SUCCESS", f"Đã thêm server {server['name']} ({server['ip']}).")
        flash(f"Đã lưu server {server['name']} ({server['ip']}).", "success")
        return redirect(url_for("ui.server_detail", server_id=server["id"]))

    return render_template("servers/form.html", form={})


@ui_bp.post("/servers/<server_id>/test-ssh")
@login_required
@role_required("Admin")
def server_test_ssh(server_id):
    success, message = test_ssh(server_id)
    server = find_server(server_id)
    target = server.get("name", server_id) if server else server_id
    record_completed_job("SSH", "Kiểm tra SSH", target, success, _server_output(server_id, message), command="ssh hostname")
    record_audit("SERVER_TEST_SSH", target, _result_label(success), message)
    flash(message, "success" if success else "danger")
    return redirect(request.referrer or url_for("ui.server_detail", server_id=server_id))


@ui_bp.post("/servers/<server_id>/test-ansible")
@login_required
@role_required("Admin")
def server_test_ansible(server_id):
    success, message = test_ansible_ping(server_id)
    server = find_server(server_id)
    target = server.get("name", server_id) if server else server_id
    record_completed_job("Ansible", "Kiểm tra Ansible ping", target, success, _server_output(server_id, message), command="ansible target -m ping")
    record_audit("SERVER_TEST_ANSIBLE", target, _result_label(success), message)
    flash(message, "success" if success else "danger")
    return redirect(request.referrer or url_for("ui.server_detail", server_id=server_id))


@ui_bp.post("/servers/<server_id>/bootstrap-sudo")
@login_required
@role_required("Admin")
def server_bootstrap_sudo(server_id):
    sudo_password = request.form.get("sudo_password", "")
    success, message = bootstrap_sudo_nopasswd(server_id, sudo_password)
    server = find_server(server_id)
    target = server.get("name", server_id) if server else server_id
    record_completed_job("Ansible", "Bootstrap sudo NOPASSWD", target, success, _server_output(server_id, message), command="ansible target -b -m shell")
    record_audit("SERVER_BOOTSTRAP_SUDO", target, _result_label(success), message)
    flash(message, "success" if success else "danger")
    return redirect(request.referrer or url_for("ui.server_detail", server_id=server_id))


@ui_bp.post("/servers/<server_id>/test-sudo")
@login_required
@role_required("Admin")
def server_test_sudo(server_id):
    success, message = test_sudo_nopasswd(server_id)
    server = find_server(server_id)
    target = server.get("name", server_id) if server else server_id
    record_completed_job("Ansible", "Kiểm tra sudo NOPASSWD", target, success, _server_output(server_id, message), command="ansible target -b -m command -a whoami")
    record_audit("SERVER_TEST_SUDO", target, _result_label(success), message)
    flash(message, "success" if success else "danger")
    return redirect(request.referrer or url_for("ui.server_detail", server_id=server_id))


@ui_bp.post("/servers/bulk-test")
@login_required
@role_required("Admin")
def servers_bulk_test():
    server_ids = request.form.getlist("server_ids")
    test_type = request.form.get("test_type", "ansible")
    success, message = test_multiple_servers(server_ids, test_type)
    record_completed_job("Ansible" if test_type != "ssh" else "SSH", f"Bulk {test_type} test", f"{len(server_ids)} server(s)", success, message, command=f"bulk {test_type} test")
    record_audit("SERVER_BULK_TEST", f"{len(server_ids)} server(s)", _result_label(success), message, metadata={"server_ids": server_ids, "test_type": test_type})
    flash(message, "success" if success else "danger")
    return redirect(url_for("ui.servers"))


@ui_bp.post("/servers/bulk-bootstrap-sudo")
@login_required
@role_required("Admin")
def servers_bulk_bootstrap_sudo():
    server_ids = request.form.getlist("server_ids")
    sudo_password = request.form.get("sudo_password", "")
    success, message = bootstrap_multiple_servers(server_ids, sudo_password)
    record_completed_job("Ansible", "Bulk bootstrap sudo", f"{len(server_ids)} server(s)", success, message, command="bulk ansible bootstrap sudo")
    record_audit("SERVER_BULK_BOOTSTRAP_SUDO", f"{len(server_ids)} server(s)", _result_label(success), message, metadata={"server_ids": server_ids})
    flash(message, "success" if success else "danger")
    return redirect(url_for("ui.servers"))


@ui_bp.route("/clusters")
@login_required
@role_required("Admin")
def clusters():
    return render_template("clusters/list.html", servers=load_servers(), steps=INSTALL_STEPS)


def _cluster_install_context(result_message: str = "", result_output: str = "") -> dict:
    return {
        "servers": load_servers(),
        "steps": INSTALL_STEPS,
        "result_message": result_message,
        "result_output": result_output,
    }


@ui_bp.route("/clusters/install")
@login_required
@role_required("Admin")
def cluster_install():
    return render_template("clusters/install.html", **_cluster_install_context())


@ui_bp.post("/clusters/dry-run")
@login_required
@role_required("Admin")
def cluster_dry_run():
    selected_nodes = [
        {"server_id": server_id, "role": request.form.get(f"role_{server_id}", "Worker")}
        for server_id in request.form.getlist("server_ids")
    ]
    success, message, output = build_cluster_inventory(selected_nodes)
    record_completed_job("Ansible", "Sinh inventory K3s", "cluster inventory", success, output or message, command="generate cluster.ini")
    record_audit("CLUSTER_DRY_RUN", "cluster inventory", _result_label(success), message, metadata={"nodes": selected_nodes})
    flash(message, "success" if success else "danger")
    return render_template("clusters/install.html", **_cluster_install_context(message, output))


@ui_bp.post("/clusters/install")
@login_required
@role_required("Admin")
def cluster_install_submit():
    selected_nodes = [
        {"server_id": server_id, "role": request.form.get(f"role_{server_id}", "Worker")}
        for server_id in request.form.getlist("server_ids")
    ]
    sudo_password = request.form.get("sudo_password", "")
    success, message, output = install_kubernetes(selected_nodes, sudo_password)
    steps = [
        {"name": "INVENTORY", "status": "Done", "message": "Sinh inventory cluster", "started_at": "", "finished_at": ""},
        {"name": "ANSIBLE", "status": "Done" if success else "Failed", "message": message, "started_at": "", "finished_at": ""},
        {"name": "KUBECONFIG", "status": "Done" if success else "Skipped", "message": "Sync kubeconfig sau khi cài thành công", "started_at": "", "finished_at": ""},
    ]
    record_completed_job("K3s", "Cài Kubernetes/K3s bằng Ansible", "cluster", success, output or message, command="ansible-playbook install_k3s_cluster.yml", steps=steps)
    record_audit("CLUSTER_INSTALL", "cluster", _result_label(success), message, metadata={"nodes": selected_nodes})
    flash(message, "success" if success else "danger")
    return render_template("clusters/install.html", **_cluster_install_context(message, output))


@ui_bp.route("/clusters/detail")
@login_required
@role_required("Admin")
def cluster_detail():
    return render_template("clusters/detail.html", servers=load_servers(), steps=INSTALL_STEPS)


@ui_bp.route("/applications")
@login_required
def applications():
    apps = load_accessible_applications(current_user)
    return render_template("applications/list.html", applications=apps)


@ui_bp.route("/applications/new", methods=["GET", "POST"])
@login_required
def application_form():
    if request.method == "POST":
        required_fields = ["name", "owner"]
        missing_fields = [field for field in required_fields if not request.form.get(field, "").strip()]

        # multi-service: cần ít nhất 1 service có tên
        service_names = request.form.getlist("service_names")
        has_service = any(name.strip() for name in service_names)
        if not has_service:
            missing_fields.append("service_names")

        # nếu source_type = docker, cần docker_image hoặc mỗi service có image riêng
        if request.form.get("source_type") == "docker":
            has_docker_image = request.form.get("docker_image", "").strip()
            service_images = request.form.getlist("service_images")
            has_service_image = any(img.strip() for img in service_images)
            if not has_docker_image and not has_service_image:
                missing_fields.append("docker_image (hoặc khai báo image trong mỗi service)")

        if request.form.get("source_type") == "github" and not request.form.get("github_url", "").strip():
            missing_fields.append("github_url")

        if missing_fields:
            flash("Vui lòng nhập đủ thông tin application/source/runtime.", "danger")
            return render_template("applications/form.html", form=request.form)

        try:
            application = create_application(request.form, current_user)
        except ValueError as exc:
            record_audit("APPLICATION_CREATE", request.form.get("name", ""), "FAILED", str(exc))
            flash(str(exc), "danger")
            return render_template("applications/form.html", form=request.form), 409
        record_audit("APPLICATION_CREATE", application["id"], "SUCCESS", f"Đã tạo application {application['name']}.")
        flash(f"Đã tạo application {application['name']}.", "success")
        return redirect(url_for("ui.application_detail", application_id=application["id"]))

    return render_template("applications/form.html", form={})


@ui_bp.route("/applications/<application_id>")
@login_required
def application_detail(application_id):
    application = _get_authorized_application(application_id)
    runtime = summarize_runtime(application)
    pipeline_steps = build_pipeline_steps(application)
    cluster_status = get_application_status(application)
    return render_template(
        "applications/detail.html",
        app=application,
        runtime=runtime,
        pipeline_steps=pipeline_steps,
        cluster_status=cluster_status,
    )


@ui_bp.post("/applications/<application_id>/deploy")
@login_required
def application_deploy(application_id):
    application = _get_authorized_application(application_id)
    success, output = deploy_application(application)
    record_completed_job("Kubectl", "Deploy application", application["name"], success, output, command="kubectl apply --validate=false")
    record_audit("APPLICATION_DEPLOY", application["id"], _result_label(success), output[:500])
    flash(("Deploy thành công. " if success else "Deploy thất bại. ") + output, "success" if success else "danger")
    return redirect(url_for("ui.application_detail", application_id=application_id))


@ui_bp.post("/applications/<application_id>/restart")
@login_required
def application_restart(application_id):
    application = _get_authorized_application(application_id)
    success, output = restart_application(application)
    record_completed_job("Kubectl", "Restart application", application["name"], success, output, command="kubectl rollout restart")
    record_audit("APPLICATION_RESTART", application["id"], _result_label(success), output[:500])
    flash(("Restart thành công. " if success else "Restart thất bại. ") + output, "success" if success else "danger")
    return redirect(url_for("ui.application_detail", application_id=application_id))


@ui_bp.post("/applications/<application_id>/scale")
@login_required
def application_scale(application_id):
    application = _get_authorized_application(application_id)
    replicas = int(request.form.get("replicas") or 1)
    success, output = scale_application(application, replicas)
    record_completed_job("Kubectl", f"Scale application lên {replicas}", application["name"], success, output, command=f"kubectl scale --replicas={replicas}")
    record_audit("APPLICATION_SCALE", application["id"], _result_label(success), output[:500], metadata={"replicas": replicas})
    flash(("Scale thành công. " if success else "Scale thất bại. ") + output, "success" if success else "danger")
    return redirect(url_for("ui.application_detail", application_id=application_id))


@ui_bp.post("/applications/<application_id>/delete-workloads")
@login_required
def application_delete_workloads(application_id):
    application = _get_authorized_application(application_id)
    success, output = delete_application_workloads(application)
    record_completed_job("Kubectl", "Xóa workloads application", application["name"], success, output, command="kubectl delete workloads")
    record_audit("APPLICATION_DELETE_WORKLOADS", application["id"], _result_label(success), output[:500])
    flash(output if not success else "Đã xóa workloads.", "success" if success else "danger")
    return redirect(url_for("ui.application_detail", application_id=application_id))


@ui_bp.post("/applications/<application_id>/delete")
@login_required
def application_delete(application_id):
    """Xóa application khỏi hệ thống và dọn dẹp namespace K8s."""
    application = _get_authorized_application(application_id)
    success = delete_application(application_id)
    if success:
        record_audit("APPLICATION_DELETE", application_id, "SUCCESS", f"Đã xóa application '{application['name']}'.")
        flash(f"Đã xóa application '{application['name']}'.", "success")
    else:
        record_audit("APPLICATION_DELETE", application_id, "FAILED", f"Không tìm thấy application '{application_id}'.")
        flash(f"Không tìm thấy application '{application_id}'.", "danger")
    return redirect(url_for("ui.applications"))


@ui_bp.post("/applications/<application_id>/pipeline")
@login_required
def application_pipeline_trigger(application_id):
    """Trigger CI/CD pipeline for a specific application."""
    application = _get_authorized_application(application_id)
    try:
        pipeline_run = trigger_pipeline(application_id, actor=current_user)
        record_audit("PIPELINE_TRIGGER", application["id"], "SUCCESS", f"Pipeline {pipeline_run['id']} đã được khởi động.")
        flash(f"Pipeline {pipeline_run['id']} đã được khởi động (6 stages). Đang chạy ngầm...", "info")
    except Exception as exc:
        record_audit("PIPELINE_TRIGGER", application["id"], "FAILED", str(exc))
        flash(f"Không thể khởi động pipeline: {exc}", "danger")
    return redirect(url_for("ui.application_detail", application_id=application_id))


@ui_bp.route("/applications/<application_id>/pipeline-history")
@login_required
def application_pipeline_history(application_id):
    """View pipeline run history for an application."""
    application = _get_authorized_application(application_id)
    runs = load_pipeline_runs(application_id)
    return render_template("applications/pipeline.html", app=application, runs=runs)


@ui_bp.route("/applications/<application_id>/logs")
@login_required
def application_logs(application_id):
    application = _get_authorized_application(application_id)
    success, logs = get_application_logs(application)
    record_completed_job("Kubectl", "Lấy pod logs", application["name"], success, logs, command="kubectl logs")
    record_audit("APPLICATION_LOGS_VIEW", application["id"], _result_label(success), "Người dùng truy vấn pod logs.")
    return render_template("deployments/logs.html", app=application, logs=logs, success=success)


@ui_bp.route("/deployments/service-form")
@login_required
def service_form():
    return redirect(url_for("ui.application_form"))


@ui_bp.route("/deployments/detail")
@login_required
def deployment_detail():
    return render_template("deployments/detail.html", applications=load_accessible_applications(current_user))


@ui_bp.route("/deployments/logs")
@login_required
def deployment_logs():
    applications = load_accessible_applications(current_user)
    if applications:
        return redirect(url_for("ui.application_logs", application_id=applications[0]["id"]))
    flash("Chưa có application để xem logs.", "warning")
    return redirect(url_for("ui.applications"))


@ui_bp.route("/jobs")
@login_required
def jobs():
    application_ids = {app["id"] for app in load_accessible_applications(current_user)}
    pipeline_jobs = [pipeline_run_to_job(run) for run in load_pipeline_runs()]
    if not current_user.is_admin:
        pipeline_jobs = [job for job in pipeline_jobs if job.get("metadata", {}).get("application_id") in application_ids]
    jobs_data = load_accessible_jobs(current_user, limit=None) + pipeline_jobs
    jobs_data = sorted(jobs_data, key=lambda item: item.get("created_at", ""), reverse=True)
    selected_id = request.args.get("job")
    selected_job = next((job for job in jobs_data if job.get("id") == selected_id), None) if selected_id else None
    selected_job = selected_job or (jobs_data[0] if jobs_data else None)
    return render_template("jobs/detail.html", jobs=jobs_data, selected_job=selected_job)


@ui_bp.route("/cicd")
@login_required
def cicd():
    application_ids = {app["id"] for app in load_accessible_applications(current_user)}
    events = load_all_pipeline_events(application_ids)
    # fallback to mock if empty
    from .mock_data import PIPELINE_EVENTS as MOCK_EVENTS

    if not events and current_user.is_admin:
        events = MOCK_EVENTS
    return render_template("cicd.html", events=events)


def _get_monitoring_data() -> dict[str, Any]:
    """Return live monitoring data from Prometheus (primary) and kubectl (fallback).

    Prometheus queries are fast (<1s) when the cluster is healthy.
    Falls back to kubectl only when Prometheus has no data.
    """
    from datetime import datetime

    apps: list[dict[str, Any]] = []
    try:
        apps = load_applications()
    except Exception:
        pass

    # --- node metrics: Prometheus (node_exporter) → kubectl ---
    nodes: list[dict[str, Any]] = []
    try:
        nodes = get_prometheus_node_metrics()
    except Exception:
        pass
    if not nodes:
        try:
            nodes = node_metrics()
        except Exception:
            pass

    # --- app metrics: Prometheus (kube-state-metrics) → kubectl ---
    app_metrics_list: list[dict[str, Any]] = []
    try:
        app_metrics_list = get_prometheus_app_metrics(apps)
    except Exception:
        pass
    if not app_metrics_list:
        try:
            app_metrics_list = application_metrics(apps)
        except Exception:
            pass

    # --- summary from fetched data ---
    total_pods = sum(a.get("total_pods", 0) for a in app_metrics_list)
    ready_pods = sum(a.get("ready_pods", 0) for a in app_metrics_list)
    summary: dict[str, Any] = {
        "nodes": len(nodes),
        "node_cpu_pct": round(sum(n.get("cpu_percent", 0) for n in nodes) / max(len(nodes), 1), 1),
        "node_ram_pct": round(sum(n.get("memory_percent", 0) for n in nodes) / max(len(nodes), 1), 1),
        "apps": len(apps),
        "total_pods": total_pods,
        "ready_pods": ready_pods,
        "pod_health_pct": round(ready_pods / max(total_pods, 1) * 100, 1),
    }

    # --- alerts ---
    all_alerts: list[dict[str, Any]] = []
    try:
        all_alerts = collect_and_persist(apps, nodes=nodes, app_metrics=app_metrics_list)
    except Exception:
        pass

    alert_counts: dict[str, int] = {"total": 0, "active": 0, "critical": 0, "warning": 0}
    try:
        alert_counts = alert_summary()
    except Exception:
        pass

    # --- grafana ---
    grafana_embed: str = ""
    try:
        grafana_embed = get_grafana_embed_url() or ""
    except Exception:
        pass

    # --- ring buffer ---
    try:
        record_snapshot(apps, nodes=nodes)
    except Exception:
        pass

    return {
        "nodes": nodes,
        "app_metrics": app_metrics_list,
        "summary": summary,
        "alerts": all_alerts,
        "alert_counts": alert_counts,
        "grafana_embed": grafana_embed,
        "fetched_at": datetime.now().strftime("%H:%M:%S"),
    }


@ui_bp.route("/monitoring")
@login_required
@role_required("Admin")
def monitoring():
    data = _get_monitoring_data()
    return render_template(
        "monitoring.html",
        servers=load_servers(),
        applications=load_applications(),
        nodes=data["nodes"],
        app_metrics=data["app_metrics"],
        summary=data["summary"],
        alerts=data["alerts"],
        alert_counts=data["alert_counts"],
        grafana_embed=data["grafana_embed"],
    )


@ui_bp.route("/monitoring/api/metrics")
@login_required
@role_required("Admin")
def monitoring_api_metrics():
    """JSON endpoint for real-time AJAX polling. Always returns valid JSON."""
    try:
        return jsonify(_get_monitoring_data())
    except Exception as exc:
        current_app.logger.exception("monitoring_api_metrics failed")
        return jsonify({"error": str(exc), "fetched_at": ""}), 500


@ui_bp.route("/monitoring/api/charts")
@login_required
@role_required("Admin")
def monitoring_api_charts():
    """JSON endpoint for chart time-series data (last 30 min).

    Tries Prometheus range queries first; falls back to the kubectl
    ring-buffer maintained by _get_monitoring_data().
    Always returns valid JSON.
    """
    import time

    empty = {"cpu": [], "memory": [], "network": [], "fetched_at": time.strftime("%H:%M:%S")}

    # --- try Prometheus first ---
    try:
        from app.modules.monitoring.prometheus import get_prometheus_client

        now = int(time.time())
        start = str(now - 1800)
        end = str(now)
        step = "30s"

        client = get_prometheus_client()
        if client.is_available():
            cpu_series = client.range_query(
                'avg(rate(node_cpu_seconds_total{mode!="idle"}[5m])) by (instance) * 100',
                start=start, end=end, step=step,
            )
            mem_series = client.range_query(
                "(1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100",
                start=start, end=end, step=step,
            )
            net_series = client.range_query(
                "sum(rate(node_network_receive_bytes_total[5m])) by (instance) * 8 / 1e6",
                start=start, end=end, step=step,
            )
            # If we got *any* data, return it
            if cpu_series or mem_series or net_series:
                return jsonify({
                    "cpu": cp2chart(cpu_series),
                    "memory": cp2chart(mem_series),
                    "network": cp2chart(net_series),
                    "fetched_at": time.strftime("%H:%M:%S"),
                })
    except Exception:
        pass

    # --- fallback: kubectl ring buffer ---
    try:
        history = get_chart_history()
        cpu_rows = history.get("cpu", [])
        mem_rows = history.get("memory", [])
        net_rows = history.get("network_rx", [])

        def _build_kubectl_series(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
            """Convert ring-buffer samples → Chart.js compatible series."""
            series: dict[str, list[dict[str, Any]]] = {}  # instance → [{x: ts, y: val}]
            for snap in samples:
                ts = snap["ts"]
                for d in snap["data"]:
                    inst = d.get("instance", "?")
                    series.setdefault(inst, []).append({"x": ts, "y": d.get("value", 0)})
            return [{"label": inst, "points": pts} for inst, pts in series.items()]

        return jsonify({
            "cpu": _build_kubectl_series(cpu_rows),
            "memory": _build_kubectl_series(mem_rows),
            "network": _build_kubectl_series(net_rows),
            "fetched_at": time.strftime("%H:%M:%S"),
        })
    except Exception as exc:
        current_app.logger.exception("monitoring_api_charts fallback failed")
        return jsonify(empty), 500


def cp2chart(prom_result: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert Prometheus range-query result to Chart.js-friendly format."""
    import re

    series: list[dict[str, Any]] = []
    seen = set()
    for r in prom_result:
        metric = r.get("metric", {})
        raw_label = metric.get("node") or metric.get("instance") or metric.get("nodename") or "?"
        label = re.sub(r":\d+$", "", raw_label)
        label = re.sub(r":.*$", "", label) if ":" in label else label
        if label in seen:
            continue
        seen.add(label)
        values = r.get("values", [])
        points = [{"x": v[0], "y": float(v[1])} for v in values]
        series.append({"label": label, "points": points})
    return series


@ui_bp.post("/monitoring/refresh")
@login_required
@role_required("Admin")
def monitoring_refresh():
    """Force refresh metrics snapshot."""
    apps = load_applications()
    save_snapshot(apps)
    collect_and_persist(apps)
    record_audit("MONITORING_REFRESH", "monitoring", "SUCCESS", "Đã làm mới metrics.")
    flash("Đã làm mới metrics thành công.", "success")
    return redirect(url_for("ui.monitoring"))


@ui_bp.route("/monitoring/grafana")
@login_required
@role_required("Admin")
def monitoring_grafana():
    """Render Grafana embedded iframe page."""
    data = _get_monitoring_data()
    grafana_embed = get_grafana_embed_url() or data.get("grafana_embed", "")
    return render_template(
        "monitoring.html",
        servers=load_servers(),
        applications=load_applications(),
        nodes=data["nodes"],
        app_metrics=data["app_metrics"],
        summary=data["summary"],
        alerts=data["alerts"],
        alert_counts=data["alert_counts"],
        grafana_embed=grafana_embed,
    )


@ui_bp.route("/monitoring/install-stack", methods=["POST"])
@login_required
@role_required("Admin")
def monitoring_install_stack():
    """Install Prometheus+Grafana monitoring stack on the K3s cluster."""
    result = deploy_monitoring_stack()
    success_all = all(result.values())
    record_completed_job("Kubectl", "Deploy monitoring stack", "monitoring", success_all, json.dumps(result, ensure_ascii=False, indent=2), command="kubectl apply monitoring manifests")
    record_audit("MONITORING_INSTALL_STACK", "monitoring", _result_label(success_all), json.dumps(result, ensure_ascii=False))
    flash(f"Deploy monitoring stack: {'✅ tất cả thành công' if success_all else '⚠ có lỗi - kiểm tra log'} — {result}", "success" if success_all else "warning")
    return redirect(url_for("ui.monitoring"))


@ui_bp.route("/monitoring/proxy/prometheus", defaults={"rest": ""}, methods=["GET", "POST"])
@ui_bp.route("/monitoring/proxy/prometheus/<path:rest>", methods=["GET", "POST"])
@login_required
@role_required("Admin")
def monitoring_proxy_prometheus(rest=""):
    """Proxy requests to Prometheus API (frontend query endpoint)."""
    from flask import Response
    import urllib.request
    import urllib.error

    prom_url = current_app.config.get("PROMETHEUS_URL", "http://localhost:30900")
    target = f"{prom_url}/api/v1/{rest}?{request.query_string.decode()}"
    try:
        body = request.get_data()
        req = urllib.request.Request(target, data=body if body else None, method=request.method)
        if "Content-Type" in request.headers:
            req.add_header("Content-Type", request.headers["Content-Type"])
        resp = urllib.request.urlopen(req, timeout=10)
        return Response(resp.read(), status=resp.status, content_type=resp.headers.get("Content-Type", "application/json"))
    except urllib.error.HTTPError as e:
        return Response(e.read(), status=e.code)
    except Exception as e:
        return Response(json.dumps({"status": "error", "error": str(e)}), status=502, content_type="application/json")


def _proxy_to_grafana(target_path: str, _redirects: int = 0) -> Any:
    """Forward a request to Grafana and return a Flask Response.

    Handles headers, cookies, binary content, and redirects.
    """
    from flask import Response

    import requests as _requests

    from app.modules.monitoring.grafana import resolve_grafana_url

    if _redirects > 5:
        return Response("Too many Grafana redirects", status=502)

    graf_url = resolve_grafana_url()
    qs = request.query_string.decode()
    target = f"{graf_url}{target_path}"
    if qs:
        target = f"{target}?{qs}"

    forward_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() in ("accept", "accept-encoding", "accept-language",
                         "content-type", "origin", "referer", "user-agent",
                         "x-requested-with", "cookie")
    }

    try:
        resp = _requests.request(
            method=request.method,
            url=target,
            headers=forward_headers,
            data=request.get_data() or None,
            allow_redirects=False,
            timeout=30,
        )
        if resp.status_code in (301, 302, 303, 307, 308):
            loc = resp.headers.get("Location", "")
            if "localhost:3000" in loc:
                idx = loc.find("/d/")
                if idx >= 0:
                    return _proxy_to_grafana(f"/grafana{loc[idx:]}", _redirects + 1)
            if loc.startswith("/"):
                return _proxy_to_grafana(loc, _redirects + 1)
        ct = resp.headers.get("Content-Type", "text/html")
        response = Response(resp.content, status=resp.status_code, content_type=ct)
        for key, val in resp.headers.items():
            if key.lower() in ("set-cookie",):
                response.headers[key] = val
        return response
    except _requests.RequestException as e:
        current_app.logger.error("Grafana proxy error: %s", e)
        return Response(f"Grafana proxy error: {e}", status=502)


@ui_bp.route("/monitoring/proxy/grafana/", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
@ui_bp.route("/monitoring/proxy/grafana/<path:rest>", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
@login_required
@role_required("Admin")
def monitoring_proxy_grafana(rest=""):
    target_path = f"/grafana/{rest}" if rest else "/grafana/"
    return _proxy_to_grafana(target_path)


@ui_bp.route("/grafana/", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
@ui_bp.route("/grafana/<path:rest>", methods=["GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS"])
@login_required
@role_required("Admin")
def monitoring_grafana_static_proxy(rest=""):
    target_path = f"/grafana/{rest}" if rest else "/grafana/"
    return _proxy_to_grafana(target_path)


@ui_bp.route("/monitoring/alerts")
@login_required
@role_required("Admin")
def monitoring_alerts():
    """View all alert history."""
    data = _get_monitoring_data()
    return render_template("monitoring.html", servers=load_servers(), applications=load_applications(),
                           nodes=data["nodes"], app_metrics=data["app_metrics"],
                           summary=data["summary"], alerts=data["alerts"], alert_counts=data["alert_counts"])


@ui_bp.post("/monitoring/alerts/<int:alert_id>/ack")
@login_required
@role_required("Admin")
def monitoring_ack_alert(alert_id):
    """Acknowledge an alert."""
    success = acknowledge_alert(alert_id)
    if success:
        record_audit("ALERT_ACK", f"alert-{alert_id}", "SUCCESS", "Đã acknowledge alert.")
        flash("Đã acknowledge alert.", "success")
    else:
        record_audit("ALERT_ACK", f"alert-{alert_id}", "FAILED", "Alert không tồn tại.")
        flash("Alert không tồn tại.", "warning")
    return redirect(url_for("ui.monitoring"))


@ui_bp.route("/audit")
@login_required
@role_required("Admin")
def audit():
    return render_template("audit.html", logs=load_audit_logs())


# ---------------------------------------------------------------------------
# network scan
# ---------------------------------------------------------------------------

@ui_bp.route("/servers/scan", methods=["GET", "POST"])
@login_required
@role_required("Admin")
def servers_scan():
    if request.method == "POST":
        subnet = request.form.get("subnet", "").strip()
        mode = request.form.get("mode", "ssh").strip()
        ssh_user = request.form.get("ssh_user", "").strip()
        ssh_key = request.form.get("ssh_key_path", "~/.ssh/id_ed25519").strip()
        ssh_port = int(request.form.get("ssh_port", 22))
        selected = request.form.getlist("selected_ips")

        if not subnet:
            flash("Vui lòng nhập dãy IP (VD: 10.0.0.0/24 hoặc 10.0.0.1-10.0.0.254).", "warning")
            return render_template("servers/scan.html", servers=[], subnet=subnet, mode=mode,
                                  ssh_user=ssh_user, ssh_key=ssh_key, ssh_port=ssh_port)

        # Scan
        if mode == "ping":
            discovered = ping_scan_network(subnet)
        else:
            if not ssh_user:
                flash("Vui lòng nhập SSH User cho chế độ SSH scan.", "warning")
                return render_template("servers/scan.html", servers=[], subnet=subnet, mode=mode,
                                      ssh_user=ssh_user, ssh_key=ssh_key, ssh_port=ssh_port)
            discovered = scan_network(subnet, ssh_user, ssh_key, ssh_port)

        # Add selected
        if selected:
            added = 0
            for d in discovered:
                if d["ip"] in selected:
                    # Check if already exists
                    existing = load_servers()
                    if any(s["ip"] == d["ip"] for s in existing):
                        continue
                    form_data = {
                        "name": d["hostname"],
                        "ip": d["ip"],
                        "ssh_user": d["ssh_user"],
                        "ssh_port": str(d["ssh_port"]),
                        "ssh_key_path": ssh_key,
                        "role": d["role"],
                    }
                    add_server(form_data)
                    added += 1
            record_audit("SERVER_SCAN_ADD", subnet, "SUCCESS", f"Đã thêm {added} server mới vào hệ thống.", metadata={"mode": mode, "selected_ips": selected})
            flash(f"Đã thêm {added} server mới vào hệ thống.", "success")
            return redirect(url_for("ui.servers"))

        if not discovered:
            flash(f"Không tìm thấy server nào trong dãy {subnet}.", "warning")
        else:
            flash(f"Tìm thấy {len(discovered)} server trong dãy {subnet}. Chọn server muốn thêm.", "info")
        record_completed_job("Network", f"Scan network ({mode})", subnet, True, json.dumps(discovered, ensure_ascii=False, indent=2), command=f"{mode} scan {subnet}")
        record_audit("SERVER_SCAN", subnet, "SUCCESS", f"Tìm thấy {len(discovered)} server.", metadata={"mode": mode})

        return render_template("servers/scan.html", servers=discovered, subnet=subnet, mode=mode,
                               ssh_user=ssh_user, ssh_key=ssh_key, ssh_port=ssh_port)

    return render_template("servers/scan.html", servers=[], subnet="", mode="ssh",
                           ssh_user="", ssh_key="~/.ssh/id_ed25519", ssh_port=22)
