from flask import Blueprint, flash, redirect, render_template, request, url_for

from ...db import db
from ...models import Server
from ..auth.routes import login_required
from ..audit.service import record_audit

servers_bp = Blueprint("servers", __name__, url_prefix="/servers")


@servers_bp.route("")
@login_required
def list_servers():
    return render_template("servers/list.html", servers=Server.query.order_by(Server.id.desc()).all())


@servers_bp.route("/new", methods=["GET", "POST"])
@login_required
def new_server():
    if request.method == "POST":
        server = Server(
            name=request.form["name"].strip(),
            ip=request.form["ip"].strip(),
            ssh_port=int(request.form.get("ssh_port") or 22),
            ssh_user=request.form["ssh_user"].strip(),
            ssh_password=request.form.get("ssh_password") or None,
            ssh_key_path=request.form.get("ssh_key_path") or None,
            os=request.form.get("os") or "Ubuntu",
        )
        db.session.add(server)
        db.session.commit()
        record_audit("SERVER_CREATE", "server", server.id, message=f"Added server {server.ip}")
        flash("Đã thêm máy chủ.", "success")
        return redirect(url_for("servers.list_servers"))
    return render_template("servers/form.html")
