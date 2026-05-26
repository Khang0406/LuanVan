# app.py - CICTAdmin (optimized & fixed)
import os
import shlex
import sys
import sqlite3
import subprocess
import platform
import datetime
import json
import socket
import logging
import platform
from functools import wraps

import psutil
import paramiko
from flask import (
    Flask, render_template, request, redirect, url_for, session, g, flash, jsonify
)

def apply_firewall_rule(ip, port, action):
    if platform.system() == "Windows":
        print(f"[WINDOWS] {action} {ip}:{port} (demo only)")
        return

    try:
        if action == "deny":
            cmd = f"sudo iptables -A INPUT -s {ip} -p tcp --dport {port} -j DROP"
        else:
            cmd = f"sudo iptables -A INPUT -s {ip} -p tcp --dport {port} -j ACCEPT"

        os.system(cmd)
    except Exception as e:
        print("Firewall error:", e)

# Ensure UTF-8 stdout on Windows
if platform.system() == "Windows":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ---------------- App Config ----------------
app = Flask(__name__)
app.secret_key = "cictadmin_secret_key"
app.config['TEMPLATES_AUTO_RELOAD'] = True

@app.context_processor
def inject_user():
    return dict(current_user=session.get("user"))


FORBIDDEN_CMDS = [
    "rm", "rm -rf", "shutdown", "reboot", "poweroff", "halt",
    "mkfs", "killall", "dd", "userdel", "init 0", "init 6"
]

def is_forbidden(cmd: str):
    for bad in FORBIDDEN_CMDS:
        if cmd.strip().startswith(bad):
            return True
    return False

def save_terminal_log(username, cmd, output):
    conn = sqlite3.connect("cictadmin.db")
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO terminal_logs (username, command, output)
        VALUES (?, ?, ?)
    """, (username, cmd, output[:2000]))
    conn.commit()
    conn.close()


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATABASE = os.path.join(BASE_DIR, "cictadmin.db")
print("Using database:", DATABASE)



# ---------------- Database Helpers ----------------
def get_db():
    db = getattr(g, "_database", None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE, detect_types=sqlite3.PARSE_DECLTYPES)
        db.row_factory = sqlite3.Row
    return db


@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, "_database", None)
    if db:
        db.close()


def query_db(query, args=(), one=False):
    cur = get_db().execute(query, args)
    rv = cur.fetchall()
    cur.close()
    rv = [dict(r) for r in rv]
    return (rv[0] if rv else None) if one else rv


# ---------------- Init DB (create tables if not exist) ----------------
def init_db():
    conn = sqlite3.connect(DATABASE)
    cursor = conn.cursor()

    # users
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        role TEXT DEFAULT 'user',
        is_locked INTEGER DEFAULT 0
    )
    """)

    # servers
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS servers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        ip TEXT NOT NULL,
        username TEXT NOT NULL,
        password TEXT NOT NULL,
        os TEXT,
        description TEXT,
        port INTEGER DEFAULT 22
    )
    """)

    # websites
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS websites (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        url TEXT,
        server_id INTEGER,
        service_name TEXT,
        description TEXT,
        FOREIGN KEY (server_id) REFERENCES servers(id)
    )
    """)

    # user_servers (assignment)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS user_servers (
        user_id INTEGER NOT NULL,
        server_id INTEGER NOT NULL,
        PRIMARY KEY (user_id, server_id),
        FOREIGN KEY (user_id) REFERENCES users(id),
        FOREIGN KEY (server_id) REFERENCES servers(id)
    )
    """)

    # user_website (assignment)
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS user_website (
        user_id INTEGER NOT NULL,
        website_id INTEGER NOT NULL,
        PRIMARY KEY (user_id, website_id),
        FOREIGN KEY (user_id) REFERENCES users(id),
        FOREIGN KEY (website_id) REFERENCES websites(id)
    )
    """)

    # user_blocklist
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS user_blocklist (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT NOT NULL,
        reason TEXT,
        blocked_at DATETIME DEFAULT CURRENT_TIMESTAMP,
        blocked_by TEXT
    )
    """)

    # security_logs
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS security_logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT,
        action TEXT,
        detail TEXT,
        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # failed_logins
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS failed_logins (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE,
        attempts INTEGER DEFAULT 0,
        last_attempt DATETIME
    )
    """)

    # terminal_log
    cursor.execute("""
    CREATE TABLE IF NOT EXISTS terminal_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT,
    command TEXT,
    output TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    """)

    # default admin (ensure single admin entry)
    cursor.execute("SELECT id FROM users WHERE username = 'admin'")
    if not cursor.fetchone():
        cursor.execute("""
            INSERT INTO users (username, password, role, is_locked)
            VALUES ('admin', 'admin', 'admin', 0)
        """)
        print("✔ Created default admin/admin")

    # sample server + websites if none exist
    cursor.execute("SELECT id FROM servers LIMIT 1")
    s = cursor.fetchone()
    if not s:
        cursor.execute("""
            INSERT INTO servers (name, ip, username, password, os, description)
            VALUES ('Server Test 1', '192.168.1.1', 'testuser', 'testpass', 'Linux', 'Server de test')
        """)
        server_id = cursor.lastrowid
        cursor.execute("""
            INSERT INTO websites (name, url, server_id, service_name, description)
            VALUES ('Web Quan Ly', 'ql.local', ?, 'nginx', 'Trang quan ly noi bo')
        """, (server_id,))
        cursor.execute("""
            INSERT INTO websites (name, url, server_id, service_name, description)
            VALUES ('Web Cong Khai', 'congkhai.com', ?, 'apache2', 'Trang web cong khai')
        """, (server_id,))
        print("✔ Added sample server and websites")


        try:
                # 1. Kiểm tra xem cột 'port' đã tồn tại trong bảng servers chưa
                cursor.execute("PRAGMA table_info(servers)")
                columns = [col[1] for col in cursor.fetchall()]
                
                if 'port' not in columns:
                    # 2. Nếu chưa có, thực hiện ALTER TABLE để thêm cột 'port' với giá trị mặc định là 22
                    cursor.execute("ALTER TABLE servers ADD COLUMN port INTEGER DEFAULT 22")
                    print("✅ Đã tự động thêm cột 'port' vào bảng servers (Migration thành công).")
                
        except Exception as e:
                # Xử lý lỗi nếu database có vấn đề (ít xảy ra)
                print(f"Lỗi khi kiểm tra/thêm cột 'port': {e}")
    conn.commit()
    conn.close()


if not os.path.exists(DATABASE):
    init_db()
else:
    # ensure migrations / columns - here kept simple by always running init_db
    init_db()


# ---------------- Security utilities ----------------
BLOCK_THRESHOLD = 5


def is_user_blocked(username):
    db = get_db()
    cur = db.execute("SELECT 1 FROM user_blocklist WHERE username = ?", (username,))
    return cur.fetchone() is not None


def log_security_event(username, action, detail=""):
    db = get_db()
    db.execute("INSERT INTO security_logs (username, action, detail) VALUES (?, ?, ?)",
               (username, action, detail))
    db.commit()


def record_failed_login(username):
    db = get_db()
    now = datetime.datetime.utcnow()
    cur = db.execute("SELECT * FROM failed_logins WHERE username = ?", (username,))
    row = cur.fetchone()
    if row:
        attempts = row["attempts"] + 1
        db.execute("UPDATE failed_logins SET attempts = ?, last_attempt = ? WHERE username = ?",
                   (attempts, now, username))
    else:
        attempts = 1
        db.execute("INSERT INTO failed_logins (username, attempts, last_attempt) VALUES (?, ?, ?)",
                   (username, attempts, now))
    db.commit()

    if attempts >= BLOCK_THRESHOLD:
        db.execute("INSERT INTO user_blocklist (username, reason, blocked_by) VALUES (?, ?, ?)",
                   (username, f"Đăng nhập sai {attempts} lần", "system"))
        db.execute("DELETE FROM failed_logins WHERE username = ?", (username,))
        db.commit()
        log_security_event(username, "BLOCK_AUTO", f"Tự động chặn sau {attempts} lần đăng nhập sai")
        return True
    else:
        log_security_event(username, "FAILED_LOGIN", f"Lần đăng nhập thất bại #{attempts}")
        return False


def clear_failed_attempts(username):
    db = get_db()
    db.execute("DELETE FROM failed_logins WHERE username = ?", (username,))
    db.commit()


# ---------------- Authentication & Decorators ----------------
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "logged_in" not in session:
            flash("Bạn phải đăng nhập trước.", "warning")
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated_function


def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if "logged_in" not in session or session.get("role") != "admin":
            flash("Bạn không có quyền truy cập trang này!", "danger")
            return redirect(url_for("dashboard"))
        return f(*args, **kwargs)
    return decorated_function


def user_assigned_to_server(user_id, server_id):
    res = query_db("SELECT 1 FROM user_servers WHERE user_id = ? AND server_id = ?", [user_id, server_id], one=True)
    return res is not None


def user_assigned_to_website(user_id, website_id):
    res = query_db("SELECT 1 FROM user_website WHERE user_id = ? AND website_id = ?", [user_id, website_id], one=True)
    return res is not None


# ---------------- Parsing helpers ----------------
def parse_service_list(output):
    services = []
    lines = output.splitlines()
    # Skip header/footer heuristics
    data_lines = lines[1:-7] if len(lines) > 8 else lines[1:]
    import re
    pattern = re.compile(r"(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(.*)")
    for line in data_lines:
        match = pattern.match(line.strip())
        if match:
            unit, load, active, sub, description = match.groups()
            if active == "active" and sub == "running":
                display_status = "Đang chạy"
            elif active == "inactive":
                display_status = "Đã dừng"
            elif active == "failed":
                display_status = "Lỗi (Failed)"
            else:
                display_status = f"{active} ({sub})"

            services.append({
                "name": unit,
                "status_active": active,
                "status_sub": sub,
                "description": description,
                "display_status": display_status
            })
    return services


def parse_free_h(output):
    lines = output.strip().splitlines()
    if len(lines) > 1:
        parts = lines[1].split()
        if len(parts) >= 7:
            return {"total": parts[1], "available": parts[6]}
    return {"total": "N/A", "available": "N/A"}


def parse_df_h(output):
    lines = output.strip().splitlines()
    if len(lines) > 1:
        parts = lines[-1].split()
        if len(parts) >= 5:
            return {"total": parts[1], "used": parts[2], "available": parts[3]}
    return {"total": "N/A", "used": "N/A", "available": "N/A"}


# ---------------- Routes ----------------
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"].strip()
        db = get_db()
        try:
            db.execute("INSERT INTO users (username, password) VALUES (?, ?)", (username, password))
            db.commit()
            flash('Đăng ký tài khoản thành công! Vui lòng đăng nhập.', 'success')
            log_security_event(username, "REGISTER", "Người dùng đăng ký tài khoản")
            return redirect(url_for("login"))
        except sqlite3.IntegrityError:
            return render_template("register.html", error="Tên người dùng đã tồn tại!")
    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = request.form["username"].strip()
        password = request.form["password"].strip()

        if is_user_blocked(username):
            log_security_event(username, "LOGIN_BLOCKED", "Người dùng cố gắng đăng nhập trong khi bị chặn")
            return render_template("login.html", error="Tài khoản của bạn đã bị chặn. Liên hệ quản trị viên.")

        db = get_db()
        cur = db.execute("SELECT * FROM users WHERE username=? AND password=?", (username, password))
        user = cur.fetchone()

        if user:
            session["user"] = user["username"]
            session["role"] = user["role"]
            session["user_id"] = user["id"]
            session["logged_in"] = True
            clear_failed_attempts(username)
            log_security_event(username, "LOGIN_SUCCESS", "Đăng nhập thành công")
            return redirect(url_for("dashboard"))
        else:
            blocked = record_failed_login(username)
            if blocked:
                flash("Tài khoản đã bị chặn do nhiều lần đăng nhập sai.", "danger")
                return render_template("login.html", error="Tài khoản đã bị chặn do nhiều lần đăng nhập sai.")
            else:
                return render_template("login.html", error="Sai tài khoản hoặc mật khẩu")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/dashboard")
@login_required
def dashboard():
    system_info = {
        "os": platform.system() + " " + platform.release(),
        "hostname": socket.gethostname(),
        "ip_address": socket.gethostbyname(socket.gethostname()),
        "cpu": platform.processor(),
        "cpu_usage": psutil.cpu_percent(interval=1),
        "ram_total": round(psutil.virtual_memory().total / (1024 ** 3), 2),
        "ram_used": round(psutil.virtual_memory().used / (1024 ** 3), 2),
        "disk_total": round(psutil.disk_usage('/').total / (1024 ** 3), 2),
        "disk_used": round(psutil.disk_usage('/').used / (1024 ** 3), 2)
    }
    return render_template("dashboard.html", user=session.get("user"), system_info=system_info, system_json=json.dumps(system_info))


# ---------------- Remote service (SSH) ----------------
@app.route("/remote_service", methods=["GET", "POST"])
@login_required
def remote_service():
    db = get_db()
    servers = db.execute("SELECT * FROM servers").fetchall()
    print("SERVERS:", servers)

    results = []

    if request.method == "POST":
        server_ids = request.form.getlist("server_ids")
        service = request.form.get("service")
        action = request.form.get("action")

        if not server_ids:
            flash("❌ Bạn chưa chọn máy chủ.", "danger")
            return redirect(url_for("remote_service"))

        if not service:
            flash("❌ Thiếu tên dịch vụ.", "danger")
            return redirect(url_for("remote_service"))

        for sid in server_ids:
            server = db.execute("SELECT * FROM servers WHERE id = ?", (sid,)).fetchone()

            if not server:
                continue

            try:
                ssh = paramiko.SSHClient()
                ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

                ssh.connect(
                    server["ip"],
                    username=server["username"],
                    password=server["password"],
                    timeout=5
                )

                cmd = f"sudo systemctl {action} {service}"
                stdin, stdout, stderr = ssh.exec_command(cmd)

                output = stdout.read().decode()
                error = stderr.read().decode()

                ssh.close()

                if error:
                    results.append({
                        "server": server["name"],
                        "status": "error",
                        "output": error
                    })
                    log_security_event(session.get("user"), "REMOTE_SERVICE_FAIL",
                                       f"{action} {service} on {server['ip']}")
                else:
                    results.append({
                        "server": server["name"],
                        "status": "success",
                        "output": output or "OK"
                    })
                    log_security_event(session.get("user"), "REMOTE_SERVICE",
                                       f"{action} {service} on {server['ip']}")

            except Exception as e:
                results.append({
                    "server": server["name"],
                    "status": "error",
                    "output": str(e)
                })
                log_security_event(session.get("user"), "REMOTE_SERVICE_ERROR", str(e))

    return render_template(
        "remote_service.html",
        servers=servers,
        results=results
    )

# ---------------- Users management ----------------
@app.route("/users")
@admin_required
def users():
    db = get_db()
    cur = db.execute("SELECT id, username, role, is_locked FROM users")
    all_users = cur.fetchall()
    return render_template("users.html", users=all_users, user=session.get("user"))


@app.route("/add_user", methods=["POST"])
@admin_required
def add_user():
    username = request.form["username"].strip()
    password = request.form["password"].strip()
    role = request.form["role"].strip()
    db = get_db()
    try:
        db.execute("INSERT INTO users (username, password, role) VALUES (?, ?, ?)",
                   (username, password, role))
        db.commit()
        flash("Thêm người dùng thành công!", "success")
        log_security_event(session.get("user"), "USER_ADD", f"Added {username} with role {role}")
    except sqlite3.IntegrityError:
        flash("Tên người dùng đã tồn tại!", "danger")
    return redirect(url_for("users"))


@app.route("/delete_user/<int:user_id>")
@admin_required
def delete_user(user_id):
    db = get_db()
    cur = db.execute("SELECT username FROM users WHERE id=?", (user_id,))
    row = cur.fetchone()
    username = row["username"] if row else None
    db.execute("DELETE FROM users WHERE id=?", (user_id,))
    db.commit()
    flash("Xóa người dùng thành công!", "success")
    if username:
        log_security_event(session.get("user"), "USER_DELETE", f"Deleted user {username}")
    return redirect(url_for("users"))


@app.route("/edit_user/<int:user_id>", methods=["POST"])
@admin_required
def edit_user(user_id):
    username = request.form["username"].strip()
    role = request.form["role"].strip()
    db = get_db()
    db.execute("UPDATE users SET username=?, role=? WHERE id=?", (username, role, user_id))
    db.commit()
    flash("Cập nhật người dùng thành công!", "success")
    log_security_event(session.get("user"), "USER_EDIT", f"Edited user id {user_id} -> {username}/{role}")
    return redirect(url_for("users"))


# ---------------- Servers management ----------------
@app.route("/servers")
@login_required
def servers():
    db = get_db()
    role = session.get("role")
    user_id = session.get("user_id")
    if role == "admin":
        servers = db.execute("SELECT * FROM servers").fetchall()
    else:
        servers = db.execute("""
            SELECT s.* FROM servers s
            JOIN user_servers us ON s.id = us.server_id
            WHERE us.user_id = ?
        """, (user_id,)).fetchall()
    return render_template("servers.html", servers=servers, active="servers")


@app.route("/add_server", methods=["GET", "POST"])
@login_required
def add_server():
    if request.method == "GET":
        return render_template("add_server.html", active="add_server")
    current_time = datetime.datetime.now().timestamp()
    last = session.get("last_server_add_time", 0)
    if current_time - last < 2:
        return redirect(url_for("servers"))
    session["last_server_add_time"] = current_time
    name = request.form["name"].strip()
    ip = request.form["ip"].strip()
    username = request.form["username"].strip()
    password = request.form["password"].strip()
    os_type = request.form["os"].strip()
    description = request.form["description"].strip()
    port = int(request.form.get("port", 22))
    db = get_db()
    cur = db.cursor()
    cur.execute("""
        INSERT INTO servers (name, ip, username, password, os, description)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (name, ip, username, password, os_type, description))
    server_id = cur.lastrowid
    cur.execute("INSERT INTO user_servers (user_id, server_id) VALUES (?, ?)",
                (session.get("user_id"), server_id))
    db.commit()
    flash(f"Đã thêm máy chủ {name} thành công!", "success")
    log_security_event(session.get("user"), "SERVER_ADD", f"Added server {name} ({ip})")
    return redirect(url_for("servers"))


@app.route("/delete_server/<int:server_id>", methods=["POST"])
@login_required
def delete_server(server_id):
    db = get_db()
    cur = db.execute("SELECT name FROM servers WHERE id=?", (server_id,))
    row = cur.fetchone()
    if not row:
        flash(" Máy chủ không tồn tại!", "danger")
        return redirect(url_for("servers"))
    server_name = row["name"]
    db.execute("DELETE FROM servers WHERE id=?", (server_id,))
    db.execute("DELETE FROM user_servers WHERE server_id=?", (server_id,))
    db.execute("DELETE FROM websites WHERE server_id=?", (server_id,))
    db.commit()
    flash(f"Đã xóa máy chủ {server_name} thành công!", "success")
    log_security_event(session.get("user"), "SERVER_DELETE", f"Deleted server {server_name}")
    return redirect(url_for("servers"))

import socket

def check_server_online(ip, port=22):
    try:
        sock = socket.create_connection((ip, port), timeout=1)
        sock.close()
        return True
    except:
        return False


@app.route("/server/<int:server_id>/terminal")
@login_required
def server_terminal(server_id):
    server = get_server_by_id(server_id)

    if not server:
        abort(404)

    # kiểm tra quyền truy cập máy chủ
    if session.get("role") != "admin":
        if not user_has_access(session["user_id"], server_id):
            return "Bạn không có quyền truy cập máy chủ này", 403

    # nếu máy chủ offline thì không cho vào terminal
    if not check_server_online(server["ip"]):   # ← SỬA Ở ĐÂY
        return "Máy chủ đang OFFLINE, không thể truy cập Terminal.", 400

    return render_template("server_terminal.html", server=server)



ALLOWED_COMMANDS = ["ls", "pwd", "whoami", "df -h", "free -m", "uname -a"]
DANGEROUS = ["rm", "reboot", "shutdown", "kill", "mkfs", "dd", "mount"]

def save_log(user, server_id, cmd, result):
    with open("command.log", "a", encoding="utf-8") as f:
        f.write(f"[{datetime.now()}] USER={user} SERVER={server_id} CMD={cmd}\nRESULT={result}\n\n")

@app.route("/server/<int:server_id>/run", methods=["POST"])
@login_required
def run_server_command(server_id):
    server = get_server_by_id(server_id)
    if not server:
        abort(404)

    command = request.form.get("command", "").strip()

    # 1️⃣ Chặn lệnh nguy hiểm
    for bad in DANGEROUS:
        if bad in command:
            return jsonify({"output": f"Lệnh bị chặn: {bad}"}), 403

    # 2️⃣ Giới hạn lệnh cho user thường
    # Lưu ý: 'ALLOWED_COMMANDS' và 'DANGEROUS' phải được định nghĩa ở cấp độ module
    if session["role"] != "admin":
        if command not in ALLOWED_COMMANDS:
            return jsonify({"output": "Bạn không có quyền chạy lệnh này!"}), 403

    # 3️⃣ SSH vào server để thực lệnh
    try:
        # Kiểm tra và đặt giá trị mặc định cho port nếu bị thiếu
        ssh_port = server.get("port", 22) 
        
        # Lưu ý: đảm bảo paramiko đã được import ở đầu file (import paramiko)
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        
        # ĐÃ SỬA LỖI QUAN TRỌNG NHẤT: THÊM THAM SỐ port=ssh_port
        # ĐÃ CHUYỂN KHÓA TỪ 'ssh_user'/'ssh_pass' SANG 'username'/'password' 
        # (Giả định rằng get_server_by_id trả về các khóa này)
        ssh.connect(
            server["ip"],
            port=ssh_port, # <-- Đã thêm Port
            username=server["username"], 
            password=server["password"]
        )

        stdin, stdout, stderr = ssh.exec_command(command)
        output = stdout.read().decode() + stderr.read().decode()
        ssh.close()
    except Exception as e:
        output = f"Lỗi SSH: {str(e)}"

    # 4️⃣ Log
    save_log(session["username"], server_id, command, output)

    return jsonify({"output": output})

# ---------------- Power server ----------------

@app.route("/power/<int:server_id>/<action>")
@admin_required
def power_control(server_id, action):
    db = get_db()
    server = db.execute("SELECT * FROM servers WHERE id = ?", (server_id,)).fetchone()

    if not server:
        flash("Server không tồn tại!", "danger")
        return redirect(url_for("servers"))

    try:
        import paramiko

        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        ssh.connect(
            server["ip"],
            username=server["username"],
            password=server["password"],
            port=server["port"]
        )

        if action == "shutdown":
            cmd = "sudo shutdown now"
        elif action == "reboot":
            cmd = "sudo reboot"
        else:
            flash("Action không hợp lệ!", "danger")
            return redirect(url_for("servers"))

        ssh.exec_command(cmd)
        ssh.close()

        flash(f"Đã gửi lệnh {action} tới server {server['name']}", "success")

    except Exception as e:
        flash(f"Lỗi: {str(e)}", "danger")

    return redirect(url_for("servers"))

# ---------------- Websites management ----------------
@app.route("/websites")
@admin_required
def websites():
    db = get_db()
    cur = db.execute("""
        SELECT websites.*, servers.name AS server_name
        FROM websites
        LEFT JOIN servers ON websites.server_id = servers.id
    """)
    websites_list = cur.fetchall()
    servers = db.execute("SELECT id, name FROM servers").fetchall()
    return render_template("websites.html", websites=websites_list, servers=servers)


@app.route("/add_website", methods=["POST"])
@admin_required
def add_website():
    name = request.form["name"].strip()
    url = request.form["url"].strip()
    server_id = request.form["server_id"]
    service_name = request.form["service_name"].strip()
    description = request.form["description"].strip()
    db = get_db()
    db.execute(
        "INSERT INTO websites (name, url, server_id, service_name, description) VALUES (?, ?, ?, ?, ?)",
        (name, url, server_id, service_name, description)
    )
    db.commit()
    flash("Đã thêm website mới thành công!", "success")
    log_security_event(session.get("user"), "WEBSITE_ADD", f"Added website {name} on server {server_id}")
    return redirect(url_for("websites"))


@app.route("/delete_website/<int:web_id>")
@admin_required
def delete_website(web_id):
    db = get_db()
    cur = db.execute("SELECT name FROM websites WHERE id=?", (web_id,))
    row = cur.fetchone()
    web_name = row["name"] if row else str(web_id)
    db.execute("DELETE FROM websites WHERE id=?", (web_id,))
    db.execute("DELETE FROM user_website WHERE website_id=?", (web_id,))
    db.commit()
    flash("Đã xóa website!", "info")
    log_security_event(session.get("user"), "WEBSITE_DELETE", f"Deleted website {web_name}")
    return redirect(url_for("websites"))


# ---------------- Assign users to servers/websites ----------------
@app.route("/assign_user", methods=["GET"])
@admin_required
def assign_user():
    db = get_db()
    servers = db.execute("SELECT id, name FROM servers").fetchall()
    users_ = db.execute("SELECT id, username FROM users").fetchall()
    websites = db.execute("SELECT id, name FROM websites").fetchall()
    return render_template("assign_user.html", servers=servers, users=users_, websites=websites)


@app.route("/assign_user", methods=["POST"])
@admin_required
def assign_user_post():
    user_id = request.form["user_id"]
    resource_type = request.form["resource_type"]
    db = get_db()
    try:
        if resource_type == "server":
            server_id = request.form["server_id"]
            db.execute("INSERT OR IGNORE INTO user_servers (user_id, server_id) VALUES (?, ?)", (user_id, server_id))
            flash("Đã gán người dùng cho máy chủ thành công!", "success")
            log_security_event(session.get("user"), "ASSIGN_SERVER", f"Assigned user {user_id} to server {server_id}")
        elif resource_type == "website":
            website_id = request.form["website_id"]
            db.execute("INSERT OR IGNORE INTO user_website (user_id, website_id) VALUES (?, ?)", (user_id, website_id))
            flash("Đã gán người dùng cho website thành công!", "success")
            log_security_event(session.get("user"), "ASSIGN_WEBSITE", f"Assigned user {user_id} to website {website_id}")
        else:
            flash("Lỗi: Loại tài nguyên không hợp lệ.", "danger")
    except Exception as e:
        flash("Lỗi khi gán: " + str(e), "danger")
    db.commit()
    return redirect(url_for("assign_user"))


# ---------------- My Resources (user view) ----------------
@app.route("/my_resources")
@login_required
def my_resources():
    user_id = session.get("user_id")
    if not user_id:
        u = query_db("SELECT id FROM users WHERE username=?", [session.get("user")], one=True)
        if not u:
            flash("Không tìm thấy user", "danger")
            return redirect(url_for("logout"))
        user_id = u["id"]
        session["user_id"] = user_id

    db = get_db()
    servers = db.execute("""
        SELECT s.* FROM servers s
        JOIN user_servers us ON s.id = us.server_id
        WHERE us.user_id = ?
    """, (user_id,)).fetchall()

    websites = db.execute("""
        SELECT w.* FROM websites w
        JOIN user_website uw ON w.id = uw.website_id
        WHERE uw.user_id = ?
    """, (user_id,)).fetchall()

    return render_template("user_resources.html",
                           assigned_servers=servers,
                           assigned_websites=websites)


# ---------------- Monitor API (was commented previously) ----------------
@app.route("/api/monitor_detail/<int:server_id>")
@login_required
def api_monitor_detail(server_id):
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401

    db = get_db()
    cur = db.execute("SELECT * FROM servers WHERE id=?", (server_id,))
    server = cur.fetchone()
    if not server:
        return jsonify({"error": "Không tìm thấy máy chủ."}), 404

    ssh = None
    commands = {
        "os_release": "cat /etc/os-release | grep PRETTY_NAME | cut -d '=' -f2 | tr -d '\"'",
        "kernel_arch": "uname -m",
        "cpu_model": "lscpu | grep 'Model name' | awk -F': ' '{print $2}' | sed 's/^[ \t]*//' || true",
        "process_count": "ps aux | wc -l",
        "free_h": "free -h",
        "df_h": "df -h /",
        "cpu_usage": "top -bn1 | grep 'Cpu(s)' | awk '{print $2}' || true",
        "uptime": "uptime -p"
    }

    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(server["ip"], username=server["username"], password=server["password"], timeout=10)

        raw_results = {}
        for key, cmd in commands.items():
            stdin, stdout, stderr = ssh.exec_command(cmd)
            raw_results[key] = stdout.read().decode().strip()

        ssh.close()

        ram_info = parse_free_h(raw_results.get("free_h", ""))
        disk_info = parse_df_h(raw_results.get("df_h", ""))

        # try to compute percent if possible (best-effort)
        cpu_percent = raw_results.get("cpu_usage", "")
        process_count = raw_results.get("process_count", "")

        results = {
            "name": server["name"],
            "ip": server["ip"],
            "os": raw_results.get("os_release", ""),
            "architecture": raw_results.get("kernel_arch", ""),
            "cpu_model": raw_results.get("cpu_model", ""),
            "cpu": cpu_percent,
            "ram": ram_info,
            "disk": disk_info,
            "uptime": raw_results.get("uptime", ""),
            "process_count": process_count
        }

        return jsonify(results)
    except Exception as e:
        if ssh:
            ssh.close()
        logging.exception("Monitor error")
        return jsonify({"error": str(e)}), 500


# ---------------- API server status for user ----------------
@app.route("/api/server_status/<int:server_id>")
@login_required
def api_server_status(server_id):
    if "user_id" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    if not user_assigned_to_server(session["user_id"], server_id):
        return jsonify({"error": "permission denied"}), 403

    status = {
        "cpu": psutil.cpu_percent(),
        "ram": psutil.virtual_memory().percent,
        "disk": psutil.disk_usage('/').percent,
        "processes": []
    }
    for p in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
        status["processes"].append(p.info)
    return jsonify(status)


# ---------------- User/server detail (user view) ----------------
@app.route("/user/server/<int:server_id>")
@login_required
def user_server_detail(server_id):
    if "user_id" not in session or not user_assigned_to_server(session["user_id"], server_id):
        flash("Bạn không có quyền truy cập máy chủ này", "danger")
        return redirect(url_for("my_resources"))

    server = query_db("SELECT * FROM servers WHERE id=?", [server_id], one=True)
    if not server:
        flash("Máy chủ không tồn tại", "danger")
        return redirect(url_for("my_resources"))

    info = {"cpu": "Lỗi", "ram": "Lỗi", "process_count": "Lỗi"}
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(server["ip"], username=server["username"], password=server["password"], timeout=6)

        commands = {
            "cpu": "top -bn1 | grep 'Cpu(s)' | awk '{print $2}' || true",
            "ram": "free | grep Mem | awk '{print $3/$2 * 100.0}' || true",
            "process_count": "ps aux | wc -l"
        }

        results = {}
        for key, cmd in commands.items():
            stdin, stdout, stderr = ssh.exec_command(cmd)
            results[key] = stdout.read().decode().strip()
        ssh.close()

        info = {
            "cpu": float(results["cpu"]) if results["cpu"] and results["cpu"].replace('.', '', 1).isdigit() else results["cpu"],
            "ram": float(results["ram"]) if results["ram"] and results["ram"].replace('.', '', 1).isdigit() else results["ram"],
            "process_count": results["process_count"]
        }
    except Exception as e:
        logging.error(f"Lỗi kết nối tới {server['ip']} (User View): {e}")
        flash(f"Không thể kết nối tới máy chủ {server['ip']} để giám sát: {e}", "warning")

    return render_template("user_server_detail.html", server=server, info=info)


# ---------------- User remote action (control) ----------------
@app.route("/user/remote_action", methods=["POST"])
@login_required
def user_remote_action():
    server_id = request.form.get("server_id")
    service = request.form.get("service_name")
    action = request.form.get("action")
    if "user_id" not in session or not user_assigned_to_server(session["user_id"], int(server_id)):
        flash("Bạn không có quyền thực hiện thao tác này.", "danger")
        return redirect(url_for("user_server_detail", server_id=server_id))

    server = query_db("SELECT * FROM servers WHERE id=?", [server_id], one=True)
    if not server or not service or action not in ["restart", "stop", "start"]:
        flash("Yêu cầu không hợp lệ.", "danger")
        return redirect(url_for("user_server_detail", server_id=server_id))

    cmd = f"sudo systemctl {action} {service}"
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(server["ip"], username=server["username"], password=server["password"], timeout=8)
        stdin, stdout, stderr = ssh.exec_command(cmd)
        exit_status = stdout.channel.recv_exit_status()
        ssh.close()
        if exit_status == 0:
            flash(f"Đã {action} dịch vụ {service} trên {server['ip']} thành công!", "success")
        else:
            flash(f"Lỗi khi thực thi lệnh trên {server['ip']}. Kiểm tra quyền sudo.", "danger")
    except Exception as e:
        flash(f"Không thể kết nối hoặc lỗi SSH: {e}", "danger")
    return redirect(url_for("user_server_detail", server_id=server_id))


# ---------------- User website detail ----------------
@app.route("/user/website/<int:web_id>")
@login_required
def user_website_detail(web_id):
    if "user_id" not in session:
        return redirect(url_for("login"))
    if not user_assigned_to_website(session["user_id"], web_id):
        return "Không có quyền truy cập website", 403
    website = query_db("SELECT * FROM websites WHERE id=?", [web_id], one=True)
    if not website:
        return "Website không tồn tại", 404
    return render_template("user_website_detail.html", website=website)


# ---------------- Website control API (admin) ----------------
@app.route("/api/website/control", methods=["POST"])
@admin_required
def website_control():
    web_id = request.json.get("web_id")
    action = request.json.get("action")
    website = query_db("SELECT service_name FROM websites WHERE id=?", [web_id], one=True)
    if website:
        svc = website.get("service_name")
        if action == "start":
            os.system(f"systemctl start {svc}")
        elif action == "stop":
            os.system(f"systemctl stop {svc}")
        elif action == "restart":
            os.system(f"systemctl restart {svc}")
        return jsonify({"status": "ok"})
    return jsonify({"status": "error", "message": "Website not found"}), 404


# ---------------- Sysinfo & services local ----------------
@app.route("/sysinfo")
@login_required
def sysinfo():
    db = get_db()
    role = session.get("role")
    user_id = session.get("user_id")
    if role == "admin":
        servers = db.execute("SELECT * FROM servers ORDER BY name ASC").fetchall()
    else:
        servers = db.execute("""
            SELECT s.* FROM servers s
            JOIN user_servers us ON s.id = us.server_id
            WHERE us.user_id=? ORDER BY s.name ASC
        """, (user_id,)).fetchall()
    return render_template("sysinfo.html", servers=servers)


@app.route("/api/sysinfo/<int:server_id>")
@login_required
def api_sysinfo(server_id):
    db = get_db()
    server = db.execute("SELECT * FROM servers WHERE id=?", (server_id,)).fetchone()
    if not server:
        return jsonify({"error": "Không tìm thấy máy chủ"}), 404
    try:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(server["ip"], username=server["username"], password=server["password"], timeout=8)
        cmds = {
            "cpu": "top -bn1 | grep 'Cpu(s)' | awk '{print $2}' || true",
            "ram": "free | grep Mem | awk '{printf(\"%d\", $3/$2 * 100)}' || true",
            "disk": "df / | awk 'NR==2 {print $5}' | sed 's/%//' || true",
            "ram_full": "free -h | grep Mem || true",
            "disk_full": "df -h / | tail -1 || true"
        }
        raw = {}
        for key, cmd in cmds.items():
            stdin, stdout, stderr = ssh.exec_command(cmd)
            raw[key] = stdout.read().decode().strip()
        ssh.close()
        r = raw.get("ram_full", "").split()
        ram_total = r[1] if len(r) > 1 else "N/A"
        ram_used = r[2] if len(r) > 2 else "N/A"
        d = raw.get("disk_full", "").split()
        disk_total = d[1] if len(d) > 1 else "N/A"
        disk_used = d[2] if len(d) > 2 else "N/A"
        return jsonify({
            "cpu": raw.get("cpu", ""),
            "ram": raw.get("ram", ""),
            "ram_total": ram_total,
            "ram_used": ram_used,
            "disk": raw.get("disk", ""),
            "disk_total": disk_total,
            "disk_used": disk_used
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/services_local")
@login_required
def services_local():
    services = ["nginx", "apache2", "mysql", "ssh"]
    status = {}
    for s in services:
        try:
            result = subprocess.run(["systemctl", "is-active", s], stdout=subprocess.PIPE)
            status[s] = result.stdout.decode().strip()
        except Exception:
            status[s] = "unknown"
    return render_template("services_local.html", status=status)


@app.route("/services_local/action", methods=["POST"])
@login_required
def services_local_action():
    service = request.form["service"]
    action = request.form["action"]
    subprocess.run(["sudo", "systemctl", action, service])
    flash(f"{action} dịch vụ {service} thành công!", "success")
    return redirect(url_for("services_local"))


# ---------------- Process management ----------------
@app.route("/process_management", methods=["GET", "POST"])
@login_required
def process_management():
    db = get_db()
    servers = [dict(s) for s in db.execute("SELECT * FROM servers").fetchall()]

    results = []

    if request.method == "POST":
        server_ids = request.form.getlist("server_ids")
        action = request.form.get("action")
        pid = request.form.get("pid")
        command = request.form.get("command")

        for sid in server_ids:
            server = next((s for s in servers if str(s["id"]) == sid), None)
            if not server:
                continue

            try:
                ssh = paramiko.SSHClient()
                ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

                ssh.connect(
                    server["ip"],
                    username=server["username"],
                    password=server["password"],
                    timeout=5
                )

                # 👇 COMMAND
                if action == "list":
                    cmd = "ps aux --sort=-%cpu | head -n 15"

                elif action == "kill":
                    cmd = f"kill -9 {pid}"

                elif action == "run":
                    cmd = command

                stdin, stdout, stderr = ssh.exec_command(cmd)

                output = stdout.read().decode()
                error = stderr.read().decode()

                ssh.close()

                results.append({
                    "server": server["name"],
                    "status": "error" if error else "success",
                    "output": error if error else output
                })

            except Exception as e:
                results.append({
                    "server": server["name"],
                    "status": "error",
                    "output": str(e)
                })

    return render_template(
        "process_management.html",
        servers=servers,
        results=results
    )


# ---------------- Security & block management ----------------
@app.route("/security")
@admin_required
def security():
    db = get_db()
    blocked = db.execute("SELECT id, username, reason, blocked_at, blocked_by FROM user_blocklist ORDER BY blocked_at DESC").fetchall()
    logs = db.execute("SELECT id, username, action, detail, timestamp FROM security_logs ORDER BY timestamp DESC LIMIT 200").fetchall()
    return render_template("security.html", blocked=blocked, logs=logs, user=session.get("user"))


@app.route("/block_user/<username>", methods=["POST", "GET"])
@admin_required
def block_user(username):
    db = get_db()
    reason = request.form.get("reason", "Blocked by admin")
    db.execute("INSERT INTO user_blocklist (username, reason, blocked_by) VALUES (?, ?, ?)",
               (username, reason, session.get("user")))
    db.commit()
    log_security_event(session.get("user"), "BLOCK_MANUAL", f"Blocked {username}: {reason}")
    flash(f"Đã chặn người dùng {username}", "warning")
    return redirect(url_for("security"))


@app.route("/unblock_user/<username>", methods=["POST", "GET"])
@admin_required
def unblock_user(username):
    db = get_db()
    db.execute("DELETE FROM user_blocklist WHERE username = ?", (username,))
    db.commit()
    log_security_event(session.get("user"), "UNBLOCK_MANUAL", f"Unblocked {username}")
    flash(f"Đã bỏ chặn người dùng {username}", "success")
    return redirect(url_for("security"))


@app.route("/toggle_lock/<int:user_id>")
@admin_required
def toggle_lock(user_id):
    db = get_db()
    cur = db.execute("SELECT is_locked FROM users WHERE id = ?", (user_id,))
    user = cur.fetchone()
    if user:
        new_status = 0 if user["is_locked"] else 1
        db.execute("UPDATE users SET is_locked = ? WHERE id = ?", (new_status, user_id))
        db.commit()
        flash("Đã cập nhật trạng thái tài khoản!", "success")
    return redirect(url_for("users"))

# ---------------- Firewall ----------------
@app.route("/firewall", methods=["GET", "POST"])
@admin_required
def firewall():
    db = get_db()

    if request.method == "POST":
        ip = request.form["ip"]
        port = request.form["port"]
        action = request.form["action"]

        db.execute(
            "INSERT INTO firewall_rules (ip, port, action) VALUES (?, ?, ?)",
            (ip, port, action)
        )
        db.commit()

        # chạy firewall thật
        apply_firewall_rule(ip, port, action)

        flash("Đã thêm rule firewall!", "success")

    rules = db.execute("SELECT * FROM firewall_rules").fetchall()

    return render_template("firewall.html", rules=rules, active="firewall")

@app.route("/delete_firewall/<int:rule_id>", methods=["POST"])
@admin_required
def delete_firewall(rule_id):
    db = get_db()
    db.execute("DELETE FROM firewall_rules WHERE id = ?", (rule_id,))
    db.commit()

    flash("Đã xoá rule firewall!", "success")
    return redirect(url_for("firewall"))

# ---------------- User Info & password ----------------
@app.route("/userinfo")
@login_required
def userinfo():
    user = query_db("SELECT * FROM users WHERE id=?", [session.get("user_id")], one=True)
    return render_template("userinfo.html", user=user)


@app.route("/change_password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        old = request.form["old"]
        new = request.form["new"]
        user = query_db("SELECT * FROM users WHERE id=?", [session.get("user_id")], one=True)
        if user["password"] != old:
            flash("Mật khẩu cũ không đúng!", "danger")
            return redirect(url_for("change_password"))
        db = get_db()
        db.execute("UPDATE users SET password=? WHERE id=?", (new, session["user_id"]))
        db.commit()
        flash("Đổi mật khẩu thành công!", "success")
        return redirect(url_for("userinfo"))
    return render_template("change_password.html")


# ---------------- SSH Terminal ----------------
# ---------------- SSH Terminal ----------------
# ---------------- SSH Terminal - ĐÃ SỬA LỖI SQLITE 'no such column: s.port' ----------------
# app.py - Hàm ssh_terminal đã hoàn chỉnh và sửa lỗi UnboundLocalError

@app.route("/ssh_terminal", methods=["GET", "POST"])
@login_required
def ssh_terminal():
    user_id = session.get("user_id")
    db = get_db()
    role = session.get("role")

    # Lấy danh sách server
    if role == "admin":
        servers = db.execute(
            "SELECT id, name, ip, username, port, description FROM servers ORDER BY name"
        ).fetchall()
    else:
        servers = db.execute("""
            SELECT s.id, s.name, s.ip, s.username, s.port, s.description 
            FROM servers s
            JOIN user_servers us ON s.id = us.server_id
            WHERE us.user_id = ? ORDER BY s.name
        """, (user_id,)).fetchall()

    servers = [dict(s) for s in servers]

    outputs = []

    if request.method == "POST":
        server_ids = request.form.getlist("server_ids")  # ✅ FIX QUAN TRỌNG
        command = request.form.get("command", "").strip()

        print("SERVER IDS:", server_ids)
        print("COMMAND:", command)

        if not server_ids or not command:
            flash("Vui lòng chọn máy chủ và nhập lệnh.", "danger")
        else:
            for sid in server_ids:
                ssh = None
                host = "unknown"

                try:
                    server = db.execute(
                        "SELECT ip, username, password, port FROM servers WHERE id = ?",
                        (sid,)
                    ).fetchone()

                    if not server:
                        continue

                    server = dict(server)

                    host = server["ip"]
                    username = server["username"]
                    password = server["password"]
                    port = server.get("port", 22)

                    if is_forbidden(command):
                        result = "❌ Lệnh bị chặn!"
                    else:
                        ssh = paramiko.SSHClient()
                        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

                        ssh.connect(
                            host,
                            port=port,
                            username=username,
                            password=password,
                            timeout=10
                        )

                        stdin, stdout, stderr = ssh.exec_command(command)
                        result = stdout.read().decode() + stderr.read().decode()

                    outputs.append({
                        "server": host,
                        "output": result
                    })

                    save_terminal_log(
                        session.get("user"),
                        command,
                        f"[{host}] {result}"
                    )

                except paramiko.AuthenticationException:
                    outputs.append({
                        "server": host,
                        "output": "❌ Sai username/password"
                    })

                except Exception as e:
                    outputs.append({
                        "server": host,
                        "output": f"❌ {str(e)}"
                    })

                finally:
                    if ssh:
                        ssh.close()

            flash("✅ Đã chạy lệnh trên các máy chủ đã chọn.", "success")

    # 👉 load log
    if role == "admin":
        logs = db.execute(
            "SELECT username, command, output, created_at FROM terminal_logs ORDER BY created_at DESC LIMIT 50"
        ).fetchall()
    else:
        logs = db.execute(
            "SELECT username, command, output, created_at FROM terminal_logs WHERE username = ? ORDER BY created_at DESC LIMIT 50",
            (session.get("user"),)
        ).fetchall()

    return render_template(
        "ssh_terminal.html",
        servers=servers,
        outputs=outputs,
        logs=logs
    )

# ---------------- Helper AJAX endpoints ----------------
@app.route("/get_users_by_server/<int:server_id>")
@login_required
def get_users_by_server(server_id):
    conn = get_db()
    users = conn.execute(
        "SELECT u.id, u.username FROM users u JOIN user_servers us ON u.id = us.user_id WHERE us.server_id=?",
        (server_id,)
    ).fetchall()
    return jsonify([dict(u) for u in users])


@app.route("/get_users_by_website/<int:website_id>")
@login_required
def get_users_by_website(website_id):
    conn = get_db()
    users = conn.execute(
        "SELECT u.id, u.username FROM users u JOIN user_website uw ON u.id = uw.user_id WHERE uw.website_id=?",
        (website_id,)
    ).fetchall()
    return jsonify([dict(u) for u in users])

@app.route("/monitor/<int:server_id>")
@login_required
def monitor(server_id):
    # Kiểm tra quyền
    user_id = session.get("user_id")
    if session.get("role") != "admin":
        if not user_assigned_to_server(user_id, server_id):
            flash("Bạn không có quyền giám sát máy chủ này.", "danger")
            return redirect(url_for("my_resources"))

    server = query_db("SELECT * FROM servers WHERE id=?", [server_id], one=True)
    if not server:
        flash("Không tìm thấy thông tin máy chủ.", "danger")
        return redirect(url_for("dashboard"))

    return render_template("monitor.html", server=server)

def get_sftp_connection(server):
    """Trả về SSH + SFTP client cho server."""
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    ssh.connect(
        hostname=server["ip"],
        username=server["username"],
        password=server["password"],
        timeout=6
    )

    return ssh, ssh.open_sftp()

# ==========================================================
# FILE MANAGER – FULL BACKEND (Webmin style)
# ==========================================================

import stat, time
from io import BytesIO
from flask import send_file

# ==========================================================
# FILE MANAGER – FULL BACKEND (FINAL FIXED VERSION)
# ==========================================================

import os, stat, time
from flask import send_file, flash, redirect, url_for, request, render_template
from io import BytesIO


# ==========================================================
# MAIN VIEW – LIST DIRECTORY
# ==========================================================
@app.route("/file_manager/<int:server_id>")
@login_required
def file_manager(server_id):
    server = query_db("SELECT * FROM servers WHERE id=?", [server_id], one=True)

    path = request.args.get("path", "/")

    ssh, sftp = get_sftp_connection(server)
    items = []

    try:
        for attr in sftp.listdir_attr(path):
            items.append({
                "name": attr.filename,
                "is_dir": stat.S_ISDIR(attr.st_mode),
                "size": attr.st_size,
                "modified": time.strftime("%d/%m/%Y %H:%M:%S", time.localtime(attr.st_mtime))
            })
    except Exception as e:
        flash(f"Lỗi đọc thư mục: {e}", "danger")

    ssh.close()

    return render_template(
        "file_manager_remote.html",
        server=server,
        server_id=server_id,
        path=path,
        items=items
    )


# ==========================================================
# VIEW FILE
# ==========================================================
@app.route("/file_manager/<int:server_id>/view")
@login_required
def file_view(server_id):
    server = query_db("SELECT * FROM servers WHERE id=?", [server_id], one=True)
    path = request.args.get("path")

    ssh, sftp = get_sftp_connection(server)
    try:
        with sftp.open(path, "r") as f:
            content = f.read().decode("utf-8", errors="ignore")
    except Exception as e:
        flash(f"Lỗi đọc file: {e}", "danger")
        content = ""
    ssh.close()

    return render_template("file_manager_view.html",
                           server=server, server_id=server_id,
                           path=path, content=content)


# ==========================================================
# EDIT FILE
# ==========================================================
@app.route("/file_manager/<int:server_id>/edit", methods=["GET", "POST"])
@login_required
def file_edit(server_id):
    server = query_db("SELECT * FROM servers WHERE id=?", [server_id], one=True)
    path = request.args.get("path")

    ssh, sftp = get_sftp_connection(server)

    if request.method == "POST":
        new_content = request.form.get("content", "")
        try:
            with sftp.open(path, "w") as f:
                f.write(new_content)
            flash("Đã lưu file!", "success")
        except Exception as e:
            flash(f"Lỗi khi lưu file: {e}", "danger")

        ssh.close()
        parent = os.path.dirname(path) or "/"
        return redirect(url_for("file_manager", server_id=server_id, path=parent))

    # GET
    with sftp.open(path, "r") as f:
        content = f.read().decode("utf-8", errors="ignore")

    ssh.close()

    return render_template("file_manager_edit.html",
                           server=server, server_id=server_id,
                           path=path, content=content)


# ==========================================================
# FILE INFO
# ==========================================================
@app.route("/file_manager/<int:server_id>/info")
@login_required
def file_info(server_id):
    server = query_db("SELECT * FROM servers WHERE id=?", [server_id], one=True)
    path = request.args.get("path")

    ssh, sftp = get_sftp_connection(server)
    st = sftp.stat(path)
    ssh.close()

    info = {
        "size": st.st_size,
        "permissions": oct(st.st_mode)[-3:],
        "uid": st.st_uid,
        "gid": st.st_gid,
        "modified": time.strftime("%d/%m/%Y %H:%M:%S", time.localtime(st.st_mtime))
    }

    return render_template("file_manager_info.html",
                           server=server, server_id=server_id,
                           path=path, info=info)


# ==========================================================
# CREATE FILE
# ==========================================================
@app.route("/file_manager/<int:server_id>/create", methods=["POST"])
@login_required
def file_create(server_id):
    server = query_db("SELECT * FROM servers WHERE id=?", [server_id], one=True)
    path = request.form.get("path")
    filename = request.form.get("filename", "").strip()

    if not filename:
        flash("Tên file không hợp lệ!", "danger")
        return redirect(url_for("file_manager", server_id=server_id, path=path))

    ssh, sftp = get_sftp_connection(server)

    try:
        with sftp.open(path.rstrip("/") + "/" + filename, "w") as f:
            f.write("")
        flash("Tạo file thành công!", "success")
    except Exception as e:
        flash(f"Lỗi tạo file: {e}", "danger")

    ssh.close()
    return redirect(url_for("file_manager", server_id=server_id, path=path))


# ==========================================================
# CREATE FOLDER
# ==========================================================
@app.route("/file_manager/<int:server_id>/folder", methods=["POST"])
@login_required
def folder_create(server_id):
    server = query_db("SELECT * FROM servers WHERE id=?", [server_id], one=True)
    path = request.form.get("path")
    foldername = request.form.get("foldername", "").strip()

    ssh, sftp = get_sftp_connection(server)

    try:
        sftp.mkdir(path.rstrip("/") + "/" + foldername)
        flash("Tạo thư mục thành công!", "success")
    except Exception as e:
        flash(f"Lỗi tạo thư mục: {e}", "danger")

    ssh.close()
    return redirect(url_for("file_manager", server_id=server_id, path=path))


# ==========================================================
# DELETE FILE
# ==========================================================
@app.route("/file_manager/<int:server_id>/delete")
@login_required
def file_delete(server_id):
    server = query_db("SELECT * FROM servers WHERE id=?", [server_id], one=True)
    remote_path = request.args.get("path")

    parent = os.path.dirname(remote_path) or "/"

    ssh, sftp = get_sftp_connection(server)
    try:
        sftp.remove(remote_path)
        flash("Đã xóa file!", "info")
    except Exception as e:
        flash(f"Lỗi xóa file: {e}", "danger")
    ssh.close()

    return redirect(url_for("file_manager", server_id=server_id, path=parent))


# ==========================================================
# RENAME FILE / FOLDER
# ==========================================================
@app.route("/file_manager/<int:server_id>/rename", methods=["POST"])
@login_required
def file_rename(server_id):
    server = query_db("SELECT * FROM servers WHERE id=?", [server_id], one=True)

    old = request.form.get("old_path")
    new_name = request.form.get("new_name")

    parent = os.path.dirname(old)
    new_path = parent.rstrip("/") + "/" + new_name

    ssh, sftp = get_sftp_connection(server)
    try:
        sftp.rename(old, new_path)
        flash("Đổi tên thành công!", "success")
    except Exception as e:
        flash(f"Lỗi đổi tên: {e}", "danger")
    ssh.close()

    return redirect(url_for("file_manager", server_id=server_id, path=parent))


# ==========================================================
# UPLOAD FILE  (✔ FIXED ENDPOINT)
# ==========================================================
@app.route("/file_manager/<int:server_id>/upload", methods=["POST"])
@login_required
def file_manager_upload_remote(server_id):
    server = query_db("SELECT * FROM servers WHERE id=?", [server_id], one=True)

    path = request.form.get("path")
    file = request.files.get("file")

    if not file:
        flash("Không có file để upload!", "danger")
        return redirect(url_for("file_manager", server_id=server_id, path=path))

    ssh, sftp = get_sftp_connection(server)

    try:
        remote_path = path.rstrip("/") + "/" + file.filename
        with sftp.open(remote_path, "wb") as f:
            f.write(file.read())

        flash("Upload file thành công!", "success")
    except Exception as e:
        flash(f"Lỗi upload file: {e}", "danger")

    ssh.close()
    return redirect(url_for("file_manager", server_id=server_id, path=path))

@app.route("/admin/terminal")
def admin_terminal():
    if session.get("role") != "admin":
        return "Bạn không có quyền truy cập Terminal", 403
    return render_template("admin_terminal.html")

@app.route("/admin/terminal/run", methods=["POST"])
def terminal_run():
    if session.get("role") != "admin":
        return jsonify({"output": "Không có quyền truy cập"}), 403

    cmd = request.json.get("cmd", "").strip()

    # 1) Lọc lệnh nguy hiểm
    if is_forbidden(cmd):
        log_output = "❌ Lệnh bị chặn vì nguy hiểm!"
        save_terminal_log(session.get("username"), cmd, output)

        return jsonify({"output": log_output})

    try:
        args = shlex.split(cmd)
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=10
        )
        output = result.stdout + result.stderr

    except Exception as e:
        output = f"Lỗi: {str(e)}"

    # 2) Lưu log
    save_terminal_log(session.get("username"), cmd, output)


    return jsonify({"output": output})

@app.route("/admin/terminal/logs")
def terminal_logs():
    if session.get("role") != "admin":
        return "Không có quyền truy cập", 403

    conn = sqlite3.connect("cictadmin.db")
    cur = conn.cursor()
    cur.execute("SELECT username, command, output, created_at FROM terminal_logs ORDER BY id DESC")
    logs = cur.fetchall()
    conn.close()

    return render_template("terminal_logs.html", logs=logs)


# ---------------- Run app ----------------
if __name__ == "__main__":
    # For development only; in production use a proper WSGI server
    app.run(debug=True, use_reloader=False, host="127.0.0.1", port=5001)
