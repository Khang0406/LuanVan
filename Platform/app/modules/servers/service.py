import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from app.security import temporary_ansible_extra_vars

from app.config import BASE_DIR

from app.json_store import is_list_of_dicts, read_json, write_json
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
    return read_json(SERVERS_FILE, DEFAULT_SERVERS, is_list_of_dicts)


def save_servers(servers: list[dict[str, Any]]) -> None:
    if not is_list_of_dicts(servers):
        raise ValueError("servers must be a list of objects")
    write_json(SERVERS_FILE, servers)


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

    with temporary_ansible_extra_vars(
        {"ansible_become_password": sudo_password}
    ) as extra_vars_file:
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
            f"@{extra_vars_file}",
        ]

        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=40,
                check=False,
            )
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


# ---------------------------------------------------------------------------
# network scan — auto-discover servers on a subnet
# ---------------------------------------------------------------------------

def scan_network(
    subnet: str,
    ssh_user: str,
    ssh_key_path: str,
    ssh_port: int = 22,
    timeout: int = 5,
) -> list[dict[str, Any]]:
    """Scan a subnet range for reachable SSH servers.

    Parameters
    ----------
    subnet : str
        CIDR notation (e.g. ``10.0.0.0/24``) or explicit range (``10.0.0.1-10.0.0.254``).
    ssh_user : str
        SSH username to try on each host.
    ssh_key_path : str
        Path to SSH private key.
    ssh_port : int
        SSH port (default 22).
    timeout : int
        Seconds to wait per host.

    Returns list of discovered servers with ``ip``, ``hostname``, ``role``.
    """
    import ipaddress

    # --- resolve comma-list / CIDR / range → list of IPs ---
    if "," in subnet:
        ips = [ip.strip() for ip in subnet.split(",") if ip.strip()]
    elif "-" in subnet:
        start, end = subnet.split("-")
        start_ip = ipaddress.IPv4Address(start.strip())
        end_ip = ipaddress.IPv4Address(end.strip())
        ips = [str(ipaddress.IPv4Address(i)) for i in range(int(start_ip), int(end_ip) + 1)]
    elif "/" in subnet:
        net = ipaddress.IPv4Network(subnet, strict=False)
        ips = [str(ip) for ip in net.hosts()]
    else:
        ips = [subnet.strip()]

    discovered: list[dict[str, Any]] = []
    ssh_users = [u.strip() for u in ssh_user.split(",") if u.strip()]
    for ip in ips:
        found_user = ""
        found_hostname = ""
        for user in ssh_users:
            ok, hostname = _quick_ssh_check(ip, user, ssh_key_path, ssh_port, timeout)
            if ok:
                found_user = user
                found_hostname = hostname
                break
        if not found_user:
            continue
        role = "Worker"
        lower = found_hostname.lower()
        if "master" in lower or "control" in lower:
            role = "Master"
        discovered.append({
            "ip": ip,
            "hostname": found_hostname,
            "role": role,
            "ssh_user": found_user,
            "ssh_port": ssh_port,
        })
    return discovered


def ping_scan_network(subnet: str, timeout: int = 2) -> list[dict[str, Any]]:
    """Quick scan via ICMP ping — no SSH credentials needed.

    Returns list of live hosts with ``ip`` and ``hostname`` (empty if no reverse DNS).
    """
    import ipaddress

    if "," in subnet:
        ips = [ip.strip() for ip in subnet.split(",") if ip.strip()]
    elif "-" in subnet:
        start, end = subnet.split("-")
        start_ip = ipaddress.IPv4Address(start.strip())
        end_ip = ipaddress.IPv4Address(end.strip())
        ips = [str(ipaddress.IPv4Address(i)) for i in range(int(start_ip), int(end_ip) + 1)]
    elif "/" in subnet:
        net = ipaddress.IPv4Network(subnet, strict=False)
        ips = [str(ip) for ip in net.hosts()]
    else:
        ips = [subnet.strip()]

    discovered: list[dict[str, Any]] = []
    for ip in ips:
        try:
            result = subprocess.run(
                ["ping", "-c", "1", "-W", str(timeout), ip],
                capture_output=True, text=True, timeout=timeout + 2,
            )
            if result.returncode == 0:
                discovered.append({"ip": ip, "hostname": "", "role": "Unknown"})
        except Exception:
            pass
    return discovered


def _quick_ssh_check(ip: str, user: str, key: str, port: int, timeout: int) -> tuple[bool, str]:
    """Run ``hostname`` via SSH. Returns (ok, hostname)."""
    cmd = [
        "ssh",
        "-i", str(Path(key).expanduser()),
        "-p", str(port),
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "ConnectTimeout=3",
        "-o", "BatchMode=yes",
        f"{user}@{ip}",
        "hostname",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if result.returncode == 0:
            return True, result.stdout.strip()
        return False, ""
    except Exception:
        return False, ""
