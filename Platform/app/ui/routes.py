from flask import Blueprint, abort, flash, redirect, render_template, request, url_for

from app.modules.applications.service import (
    build_pipeline_steps,
    create_application,
    delete_application,
    find_application,
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
    node_metrics,
    save_snapshot,
)
from app.modules.monitoring.alerting import (
    acknowledge_alert,
    alert_summary,
    collect_and_persist,
    load_alerts,
)

from .mock_data import AUDIT_LOGS, INSTALL_STEPS

ui_bp = Blueprint("ui", __name__)


@ui_bp.route("/servers")
def servers():
    return render_template("servers/list.html", servers=load_servers())


@ui_bp.route("/servers/<server_id>")
def server_detail(server_id):
    server = find_server(server_id)
    if not server:
        abort(404)
    return render_template("servers/detail.html", server=server)


@ui_bp.route("/servers/new", methods=["GET", "POST"])
def server_form():
    if request.method == "POST":
        required_fields = ["name", "ip", "ssh_user", "role"]
        missing_fields = [field for field in required_fields if not request.form.get(field, "").strip()]
        if missing_fields:
            flash("Vui lòng nhập đầy đủ tên máy, IP, SSH user và vai trò.", "danger")
            return render_template("servers/form.html", form=request.form)

        server = add_server(request.form)
        flash(f"Đã lưu server {server['name']} ({server['ip']}).", "success")
        return redirect(url_for("ui.server_detail", server_id=server["id"]))

    return render_template("servers/form.html", form={})


@ui_bp.post("/servers/<server_id>/test-ssh")
def server_test_ssh(server_id):
    success, message = test_ssh(server_id)
    flash(message, "success" if success else "danger")
    return redirect(request.referrer or url_for("ui.server_detail", server_id=server_id))


@ui_bp.post("/servers/<server_id>/test-ansible")
def server_test_ansible(server_id):
    success, message = test_ansible_ping(server_id)
    flash(message, "success" if success else "danger")
    return redirect(request.referrer or url_for("ui.server_detail", server_id=server_id))


@ui_bp.post("/servers/<server_id>/bootstrap-sudo")
def server_bootstrap_sudo(server_id):
    sudo_password = request.form.get("sudo_password", "")
    success, message = bootstrap_sudo_nopasswd(server_id, sudo_password)
    flash(message, "success" if success else "danger")
    return redirect(request.referrer or url_for("ui.server_detail", server_id=server_id))


@ui_bp.post("/servers/<server_id>/test-sudo")
def server_test_sudo(server_id):
    success, message = test_sudo_nopasswd(server_id)
    flash(message, "success" if success else "danger")
    return redirect(request.referrer or url_for("ui.server_detail", server_id=server_id))


@ui_bp.post("/servers/bulk-test")
def servers_bulk_test():
    server_ids = request.form.getlist("server_ids")
    test_type = request.form.get("test_type", "ansible")
    success, message = test_multiple_servers(server_ids, test_type)
    flash(message, "success" if success else "danger")
    return redirect(url_for("ui.servers"))


@ui_bp.post("/servers/bulk-bootstrap-sudo")
def servers_bulk_bootstrap_sudo():
    server_ids = request.form.getlist("server_ids")
    sudo_password = request.form.get("sudo_password", "")
    success, message = bootstrap_multiple_servers(server_ids, sudo_password)
    flash(message, "success" if success else "danger")
    return redirect(url_for("ui.servers"))


@ui_bp.route("/clusters")
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
def cluster_install():
    return render_template("clusters/install.html", **_cluster_install_context())


@ui_bp.post("/clusters/dry-run")
def cluster_dry_run():
    selected_nodes = [
        {"server_id": server_id, "role": request.form.get(f"role_{server_id}", "Worker")}
        for server_id in request.form.getlist("server_ids")
    ]
    success, message, output = build_cluster_inventory(selected_nodes)
    flash(message, "success" if success else "danger")
    return render_template("clusters/install.html", **_cluster_install_context(message, output))


@ui_bp.post("/clusters/install")
def cluster_install_submit():
    selected_nodes = [
        {"server_id": server_id, "role": request.form.get(f"role_{server_id}", "Worker")}
        for server_id in request.form.getlist("server_ids")
    ]
    sudo_password = request.form.get("sudo_password", "")
    success, message, output = install_kubernetes(selected_nodes, sudo_password)
    flash(message, "success" if success else "danger")
    return render_template("clusters/install.html", **_cluster_install_context(message, output))


@ui_bp.route("/clusters/detail")
def cluster_detail():
    return render_template("clusters/detail.html", servers=load_servers(), steps=INSTALL_STEPS)


@ui_bp.route("/applications")
def applications():
    apps = load_applications()
    return render_template("applications/list.html", applications=apps)


@ui_bp.route("/applications/new", methods=["GET", "POST"])
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

        application = create_application(request.form)
        flash(f"Đã tạo application {application['name']}.", "success")
        return redirect(url_for("ui.application_detail", application_id=application["id"]))

    return render_template("applications/form.html", form={})


@ui_bp.route("/applications/<application_id>")
def application_detail(application_id):
    application = find_application(application_id)
    if not application:
        abort(404)
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
def application_deploy(application_id):
    application = find_application(application_id)
    if not application:
        abort(404)
    success, output = deploy_application(application)
    flash(("Deploy thành công. " if success else "Deploy thất bại. ") + output, "success" if success else "danger")
    return redirect(url_for("ui.application_detail", application_id=application_id))


@ui_bp.post("/applications/<application_id>/restart")
def application_restart(application_id):
    application = find_application(application_id)
    if not application:
        abort(404)
    success, output = restart_application(application)
    flash(("Restart thành công. " if success else "Restart thất bại. ") + output, "success" if success else "danger")
    return redirect(url_for("ui.application_detail", application_id=application_id))


@ui_bp.post("/applications/<application_id>/scale")
def application_scale(application_id):
    application = find_application(application_id)
    if not application:
        abort(404)
    replicas = int(request.form.get("replicas") or 1)
    success, output = scale_application(application, replicas)
    flash(("Scale thành công. " if success else "Scale thất bại. ") + output, "success" if success else "danger")
    return redirect(url_for("ui.application_detail", application_id=application_id))


@ui_bp.post("/applications/<application_id>/delete-workloads")
def application_delete_workloads(application_id):
    application = find_application(application_id)
    if not application:
        abort(404)
    success, output = delete_application_workloads(application)
    flash(output if not success else "Đã xóa workloads.", "success" if success else "danger")
    return redirect(url_for("ui.application_detail", application_id=application_id))


@ui_bp.post("/applications/<application_id>/delete")
def application_delete(application_id):
    """Xóa application khỏi hệ thống và dọn dẹp namespace K8s."""
    application = find_application(application_id)
    if not application:
        abort(404)
    success = delete_application(application_id)
    if success:
        flash(f"Đã xóa application '{application['name']}'.", "success")
    else:
        flash(f"Không tìm thấy application '{application_id}'.", "danger")
    return redirect(url_for("ui.applications"))


@ui_bp.post("/applications/<application_id>/pipeline")
def application_pipeline_trigger(application_id):
    """Trigger CI/CD pipeline for a specific application."""
    application = find_application(application_id)
    if not application:
        abort(404)
    try:
        pipeline_run = trigger_pipeline(application_id)
        flash(f"Pipeline {pipeline_run['id']} đã được khởi động (6 stages). Đang chạy ngầm...", "info")
    except Exception as exc:
        flash(f"Không thể khởi động pipeline: {exc}", "danger")
    return redirect(url_for("ui.application_detail", application_id=application_id))


@ui_bp.route("/applications/<application_id>/pipeline-history")
def application_pipeline_history(application_id):
    """View pipeline run history for an application."""
    application = find_application(application_id)
    if not application:
        abort(404)
    runs = load_pipeline_runs(application_id)
    return render_template("applications/pipeline.html", app=application, runs=runs)


@ui_bp.route("/applications/<application_id>/logs")
def application_logs(application_id):
    application = find_application(application_id)
    if not application:
        abort(404)
    success, logs = get_application_logs(application)
    return render_template("deployments/logs.html", app=application, logs=logs, success=success)


@ui_bp.route("/deployments/service-form")
def service_form():
    return redirect(url_for("ui.application_form"))


@ui_bp.route("/deployments/detail")
def deployment_detail():
    return render_template("deployments/detail.html", applications=load_applications())


@ui_bp.route("/deployments/logs")
def deployment_logs():
    applications = load_applications()
    if applications:
        return redirect(url_for("ui.application_logs", application_id=applications[0]["id"]))
    flash("Chưa có application để xem logs.", "warning")
    return redirect(url_for("ui.applications"))


@ui_bp.route("/jobs")
def jobs():
    return render_template("jobs/detail.html", steps=INSTALL_STEPS)


@ui_bp.route("/cicd")
def cicd():
    events = load_all_pipeline_events()
    # fallback to mock if empty
    from .mock_data import PIPELINE_EVENTS as MOCK_EVENTS

    if not events:
        events = MOCK_EVENTS
    return render_template("cicd.html", events=events)


@ui_bp.route("/monitoring")
def monitoring():
    apps = load_applications()
    nodes = node_metrics()
    app_metrics_list = application_metrics(apps)
    summary = cluster_summary(apps)
    # Collect and persist alerts
    all_alerts = collect_and_persist(apps)
    alert_counts = alert_summary()
    return render_template(
        "monitoring.html",
        servers=load_servers(),
        applications=apps,
        nodes=nodes,
        app_metrics=app_metrics_list,
        summary=summary,
        alerts=all_alerts,
        alert_counts=alert_counts,
    )


@ui_bp.route("/monitoring/refresh")
def monitoring_refresh():
    """Force refresh metrics snapshot."""
    apps = load_applications()
    save_snapshot(apps)
    collect_and_persist(apps)
    flash("Đã làm mới metrics thành công.", "success")
    return redirect(url_for("ui.monitoring"))


@ui_bp.route("/monitoring/alerts")
def monitoring_alerts():
    """View all alert history."""
    alerts = load_alerts()
    alert_counts = alert_summary()
    return render_template("monitoring.html", servers=load_servers(), applications=load_applications(),
                           nodes=node_metrics(), app_metrics=application_metrics(load_applications()),
                           summary=cluster_summary(load_applications()), alerts=alerts, alert_counts=alert_counts)


@ui_bp.route("/monitoring/alerts/<int:alert_id>/ack")
def monitoring_ack_alert(alert_id):
    """Acknowledge an alert."""
    success = acknowledge_alert(alert_id)
    if success:
        flash("Đã acknowledge alert.", "success")
    else:
        flash("Alert không tồn tại.", "warning")
    return redirect(url_for("ui.monitoring"))


@ui_bp.route("/audit")
def audit():
    return render_template("audit.html", logs=AUDIT_LOGS)
