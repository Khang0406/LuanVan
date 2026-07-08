import json
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import BASE_DIR

DATA_DIR = BASE_DIR / "app" / "data"
SERVERS_FILE = DATA_DIR / "servers.json"


DEFAULT_SERVERS = [
    {
        "id": "b2204938",
        "name": "b2204938-virtualbox",
        "ip": "192.168.56.11",
        "role": "Master",
        "status": "Ready",
        "cpu": "-",
        "ram": "-",
        "ssh_user": "b2204938",
        "ssh_port": 22,
        "ssh_key_path": "~/.ssh/id_ed25519",
        "last_check": "",
        "last_output": "",
        "ssh_status": "",
        "ansible_status": "",
        "sudo_status": "",
        "preview": "",
    },
    {
        "id": "khang",
        "name": "khang-virtualbox",
        "ip": "192.168.56.12",
        "role": "Worker",
        "status": "Ready",
        "cpu": "-",
        "ram": "-",
        "ssh_user": "khang",
        "ssh_port": 22,
        "ssh_key_path": "~/.ssh/id_ed25519",
        "last_check": "",
        "last_output": "",
        "ssh_status": "",
        "ansible_status": "",
        "sudo_status": "",
        "preview": "",
    },
]


def _ensure_data_file() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not SERVERS_FILE.exists():
        save_servers(DEFAULT_SERVERS)


def load_servers() -> list[dict[str, Any]]:
    _ensure_data_file()
    with SERVERS_FILE.open("r", encoding="utf-8") as file:
        return json.load(file)


def save_servers(servers: list[dict[str, Any]]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with SERVERS_FILE.open("w", encoding="utf-8") as file:
        json.dump(servers, file, ensure_ascii=False, indent=2)


def make_server_id(name: str, ip: str) -> str:
    safe_name = "".join(ch.lower() if ch.isalnum() else "-" for ch in name).strip("-")
    safe_ip = ip.replace(".", "-")
    return safe_name or f"server-{safe_ip}"


def add_server(form: dict[str, Any]) -> dict[str, Any]:
    servers = load_servers()

    name = form.get("name", "").strip()
    ip = form.get("ip", "").strip()
    ssh_user = form.get("ssh_user", "").strip()
    role = form.get("role", "Worker").strip()
    ssh_port = int(form.get("ssh_port") or 22)
    ssh_key_path = form.get("ssh_key_path", "~/.ssh/id_ed25519").strip()

    server = {
        "id": make_server_id(name, ip),
        "name": name,
        "ip": ip,
        "role": role,
        "status": "New",
        "cpu": "-",
        "ram": "-",
        "ssh_user": ssh_user,
        "ssh_port": ssh_port,
        "ssh_key_path": ssh_key_path,
        "last_check": "",
        "last_output": "",
        "ssh_status": "",
        "ansible_status": "",
        "sudo_status": "",
        "preview": "",
    }

    servers = [item for item in servers if item["id"] != server["id"] and item["ip"] != ip]
    servers.append(server)
    save_servers(servers)

    # Tự động kiểm tra sudo NOPASSWD ngay sau khi thêm server
    # Nếu VM đã có NOPASSWD → status = "Sudo Ready"
    # Nếu chưa → status = "Sudo Needed" (cần bootstrap thủ công với sudo password)
    _ = test_sudo_nopasswd(server["id"])

    return server


def find_server(server_id: str) -> dict[str, Any] | None:
    return next((server for server in load_servers() if server["id"] == server_id), None)


def update_server(updated_server: dict[str, Any]) -> None:
    servers = load_servers()
    for index, server in enumerate(servers):
        if server["id"] == updated_server["id"]:
            servers[index] = updated_server
            break
    save_servers(servers)


def _ssh_key_args(server: dict[str, Any]) -> list[str]:
    key_path = server.get("ssh_key_path", "").strip()
    if not key_path:
        return []

    expanded_key_path = Path(key_path).expanduser()
    if not expanded_key_path.exists():
        return []

    return ["-i", str(expanded_key_path)]


def _ansible_inventory_content(server: dict[str, Any]) -> str:
    ansible_user = server["ssh_user"]
    ansible_port = server.get("ssh_port", 22)
    key_path = server.get("ssh_key_path", "").strip()
    expanded_key_path = Path(key_path).expanduser() if key_path else None

    inventory_parts = [
        f"{server['id']} ansible_host={server['ip']}",
        f"ansible_user={ansible_user}",
        f"ansible_port={ansible_port}",
        "ansible_ssh_common_args='-o StrictHostKeyChecking=accept-new'",
    ]

    if expanded_key_path and expanded_key_path.exists():
        inventory_parts.append(f"ansible_ssh_private_key_file={expanded_key_path}")

    return "[target]\n" + " ".join(inventory_parts) + "\n"


def _rebuild_preview(server: dict[str, Any]) -> None:
    parts: list[str] = []

    ssh_status = server.get("ssh_status", "")
    if ssh_status:
        parts.append(f"SSH {ssh_status}")

    ansible_status = server.get("ansible_status", "")
    if ansible_status:
        parts.append(f"Ansible {ansible_status}")

    sudo_status = server.get("sudo_status", "")
    if sudo_status:
        parts.append(f"Sudo {sudo_status}")

    last_check = server.get("last_check", "")
    if last_check:
        parts.append(last_check)

    server["preview"] = " · ".join(parts) if parts else "Chưa kiểm tra"


def test_ssh(server_id: str) -> tuple[bool, str]:
    server = find_server(server_id)
    if not server:
        return False, "Không tìm thấy server."

    destination = f"{server['ssh_user']}@{server['ip']}"
    command = [
        "ssh",
        *_ssh_key_args(server),
        "-p",
        str(server.get("ssh_port", 22)),
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=8",
        "-o",
        "StrictHostKeyChecking=accept-new",
        destination,
        "hostname",
    ]

    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=15, check=False)
        raw_output = (completed.stdout + completed.stderr).strip()
        success = completed.returncode == 0
    except subprocess.TimeoutExpired:
        success = False
        raw_output = "SSH timeout sau 15 giây."

    server["status"] = "Online" if success else "SSH Failed"
    server["last_check"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    server["last_output"] = raw_output
    server["ssh_status"] = "OK" if success else "Failed"
    _rebuild_preview(server)
    update_server(server)

    if success:
        hostname = completed.stdout.strip().splitlines()[0] if completed.stdout.strip() else server["name"]
        return True, f"SSH thành công tới {server['name']} ({server['ip']}). Hostname: {hostname}."

    return False, f"SSH thất bại tới {server['name']} ({server['ip']}). {raw_output}"


def bootstrap_sudo_nopasswd(server_id: str, sudo_password: str) -> tuple[bool, str]:
    server = find_server(server_id)
    if not server:
        return False, "Không tìm thấy server."

    # Kiểm tra xem sudo NOPASSWD đã hoạt động chưa — nếu rồi thì không cần bootstrap lại
    already_ready, ready_msg = test_sudo_nopasswd(server_id)
    if already_ready:
        return True, f"Sudo NOPASSWD đã sẵn sàng trên {server['name']} ({server['ip']}), không cần bootstrap lại. {ready_msg}"

    sudo_password = sudo_password.strip()
    if not sudo_password:
        return False, "Vui lòng nhập sudo password để bootstrap quyền sudo NOPASSWD."

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    inventory_file = DATA_DIR / f"ansible-{server['id']}.ini"
    inventory_file.write_text(_ansible_inventory_content(server), encoding="utf-8")

    sudoers_file = f"/etc/sudoers.d/luanvan-{server['ssh_user']}"
    sudoers_rule = f"{server['ssh_user']} ALL=(ALL) NOPASSWD:ALL"
    shell_script = (
        f"printf '%s\\n' '{sudoers_rule}' > {sudoers_file} && "
        f"chmod 440 {sudoers_file} && "
        f"visudo -cf {sudoers_file}"
    )

    command = [
        "ansible",
        "target",
        "-i",
        str(inventory_file),
        "-b",
        "-m",
        "shell",
        "-a",
        shell_script,
        "--extra-vars",
        f"ansible_become_password={sudo_password}",
    ]

    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=40, check=False)
        raw_output = (completed.stdout + completed.stderr).strip()
        success = completed.returncode == 0
    except subprocess.TimeoutExpired:
        success = False
        raw_output = "Bootstrap sudo timeout sau 40 giây."

    if success:
        verify_success, verify_output = test_sudo_nopasswd(server_id)
        success = verify_success
        raw_output = f"{raw_output}\n\nVERIFY NOPASSWD:\n{verify_output}".strip()

    server = find_server(server_id) or server
    server["status"] = "Sudo Ready" if success else "Sudo Failed"
    server["last_check"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    server["last_output"] = raw_output
    server["sudo_status"] = "Ready" if success else "Failed"
    _rebuild_preview(server)
    update_server(server)

    if success:
        return True, f"Bootstrap sudo NOPASSWD thành công cho {server['name']} ({server['ip']}). Các lần install sau không cần nhập sudo password."
    return False, f"Bootstrap sudo NOPASSWD thất bại cho {server['name']} ({server['ip']}). {raw_output}"


def test_sudo_nopasswd(server_id: str) -> tuple[bool, str]:
    server = find_server(server_id)
    if not server:
        return False, "Không tìm thấy server."

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    inventory_file = DATA_DIR / f"ansible-{server['id']}.ini"
    inventory_file.write_text(_ansible_inventory_content(server), encoding="utf-8")

    command = [
        "ansible",
        "target",
        "-i",
        str(inventory_file),
        "-b",
        "-m",
        "command",
        "-a",
        "whoami",
        "-o",
    ]

    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=25, check=False)
        raw_output = (completed.stdout + completed.stderr).strip()
        success = completed.returncode == 0 and "root" in raw_output
    except subprocess.TimeoutExpired:
        success = False
        raw_output = "Test sudo NOPASSWD timeout sau 25 giây."

    server["status"] = "Sudo Ready" if success else "Sudo Failed"
    server["last_check"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    server["last_output"] = raw_output
    server["sudo_status"] = "Ready" if success else "Failed"
    _rebuild_preview(server)
    update_server(server)

    if success:
        return True, f"Sudo NOPASSWD đã sẵn sàng trên {server['name']} ({server['ip']})."
    return False, f"Sudo NOPASSWD chưa sẵn sàng trên {server['name']} ({server['ip']}). {raw_output}"


def bootstrap_multiple_servers(server_ids: list[str], sudo_password: str) -> tuple[bool, str]:
    if not server_ids:
        return False, "Vui lòng chọn ít nhất 1 server để bootstrap sudo."

    results: list[str] = []
    all_success = True

    for server_id in server_ids:
        success, message = bootstrap_sudo_nopasswd(server_id, sudo_password)
        all_success = all_success and success
        status = "PASS" if success else "FAIL"
        results.append(f"[{status}] {message}")

    summary = "\n".join(results)
    if all_success:
        return True, f"Bootstrap sudo NOPASSWD thành công cho tất cả {len(server_ids)} server được chọn.\n{summary}"

    return False, f"Có server bootstrap sudo thất bại trong {len(server_ids)} server được chọn.\n{summary}"


def test_ansible_ping(server_id: str) -> tuple[bool, str]:
    server = find_server(server_id)
    if not server:
        return False, "Không tìm thấy server."

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    inventory_file = DATA_DIR / f"ansible-{server['id']}.ini"
    inventory_file.write_text(_ansible_inventory_content(server), encoding="utf-8")

    command = [
        "ansible",
        "target",
        "-i",
        str(inventory_file),
        "-m",
        "ping",
        "-o",
    ]

    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=25, check=False)
        raw_output = (completed.stdout + completed.stderr).strip()
        success = completed.returncode == 0 and "pong" in raw_output
    except subprocess.TimeoutExpired:
        success = False
        raw_output = "Ansible ping timeout sau 25 giây."

    server["status"] = "Online" if success else "Ansible Failed"
    server["last_check"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    server["last_output"] = raw_output
    server["ansible_status"] = "OK (pong)" if success else "Failed"
    _rebuild_preview(server)
    update_server(server)

    if success:
        return True, f"Ansible ping thành công tới {server['name']} ({server['ip']}): pong."

    return False, f"Ansible ping thất bại tới {server['name']} ({server['ip']}). {raw_output}"


def test_multiple_servers(server_ids: list[str], test_type: str) -> tuple[bool, str]:
    if not server_ids:
        return False, "Vui lòng chọn ít nhất 1 server để kiểm tra."

    results: list[str] = []
    all_success = True

    for server_id in server_ids:
        if test_type == "ssh":
            success, message = test_ssh(server_id)
            label = "SSH"
        else:
            success, message = test_ansible_ping(server_id)
            label = "Ansible"

        all_success = all_success and success
        status = "PASS" if success else "FAIL"
        results.append(f"[{status}] {label}: {message}")

    summary = "\n".join(results)
    if all_success:
        return True, f"Tất cả {len(server_ids)} server được chọn đã kiểm tra thành công.\n{summary}"

    return False, f"Có server kiểm tra thất bại trong {len(server_ids)} server được chọn.\n{summary}"
