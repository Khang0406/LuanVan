from flask import Blueprint, flash, redirect, render_template, request, url_for
import paramiko

from ...db import db
from ...models import Server
from ..auth.routes import login_required
from ..audit.service import record_audit

servers_bp = Blueprint(
    "servers",
    __name__,
    url_prefix="/servers"
)


@servers_bp.route("")
@login_required
def list_servers():

    servers = Server.query.order_by(
        Server.id.desc()
    ).all()

    return render_template(
        "servers/list.html",
        servers=servers
    )


@servers_bp.route("/new", methods=["GET", "POST"])
@login_required
def new_server():

    if request.method == "POST":

        server = Server(
            name=request.form["name"].strip(),
            ip=request.form["ip"].strip(),
            ssh_port=int(
                request.form.get("ssh_port") or 22
            ),
            ssh_user=request.form["ssh_user"].strip(),
            ssh_password=request.form.get(
                "ssh_password"
            ) or None,
            ssh_key_path=request.form.get(
                "ssh_key_path"
            ) or None,
            os=request.form.get(
                "os"
            ) or "Ubuntu",
        )

        db.session.add(server)
        db.session.commit()

        record_audit(
            "SERVER_CREATE",
            "server",
            server.id,
            message=f"Added server {server.ip}"
        )

        flash(
            "Đã thêm máy chủ.",
            "success"
        )

        return redirect(
            url_for("servers.list_servers")
        )

    return render_template(
        "servers/form.html"
    )


@servers_bp.route("/<int:server_id>/edit",
                  methods=["GET", "POST"])
@login_required
def edit_server(server_id):

    server = Server.query.get_or_404(server_id)

    if request.method == "POST":

        server.name = request.form["name"].strip()
        server.ip = request.form["ip"].strip()

        server.ssh_port = int(
            request.form.get("ssh_port") or 22
        )

        server.ssh_user = request.form[
            "ssh_user"
        ].strip()

        server.ssh_password = (
            request.form.get(
                "ssh_password"
            ) or None
        )

        server.ssh_key_path = (
            request.form.get(
                "ssh_key_path"
            ) or None
        )

        server.os = (
            request.form.get(
                "os"
            ) or "Ubuntu"
        )

        db.session.commit()

        record_audit(
            "SERVER_UPDATE",
            "server",
            server.id,
            message=f"Updated server {server.ip}"
        )

        flash(
            "Cập nhật máy chủ thành công.",
            "success"
        )

        return redirect(
            url_for("servers.list_servers")
        )

    return render_template(
        "servers/form.html",
        server=server
    )


@servers_bp.route("/<int:server_id>/delete")
@login_required
def delete_server(server_id):

    server = Server.query.get_or_404(
        server_id
    )

    record_audit(
        "SERVER_DELETE",
        "server",
        server.id,
        message=f"Deleted server {server.ip}"
    )

    db.session.delete(server)
    db.session.commit()

    flash(
        "Đã xóa máy chủ.",
        "success"
    )

    return redirect(
        url_for("servers.list_servers")
    )


@servers_bp.route("/<int:server_id>/test")
@login_required
def test_server(server_id):

    server = Server.query.get_or_404(
        server_id
    )

    try:

        ssh = paramiko.SSHClient()

        ssh.set_missing_host_key_policy(
            paramiko.AutoAddPolicy()
        )

        connect_kwargs = {
            "hostname": server.ip,
            "username": server.ssh_user,
            "port": server.ssh_port,
            "timeout": 5,
        }

        if server.ssh_key_path:

            connect_kwargs[
                "key_filename"
            ] = server.ssh_key_path

        else:

            connect_kwargs[
                "password"
            ] = server.ssh_password

        ssh.connect(**connect_kwargs)

        ssh.close()

        server.status = "ONLINE"

        db.session.commit()

        record_audit(
            "SERVER_TEST",
            "server",
            server.id,
            message=f"SSH success {server.ip}"
        )

        flash(
            "Kết nối SSH thành công.",
            "success"
        )

    except Exception as exc:

        server.status = "OFFLINE"

        db.session.commit()

        record_audit(
            "SERVER_TEST",
            "server",
            server.id,
            result="FAILED",
            message=str(exc)
        )

        flash(
            f"Lỗi SSH: {exc}",
            "danger"
        )

    return redirect(
        url_for("servers.list_servers")
    )