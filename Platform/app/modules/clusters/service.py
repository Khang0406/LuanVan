import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from app.security import temporary_ansible_extra_vars

from app.config import BASE_DIR
from app.json_store import is_list_of_dicts, read_json, write_json
from app.modules.servers.service import find_server, load_servers, save_servers

DATA_DIR = BASE_DIR / "app" / "data"
GENERATED_INVENTORY = BASE_DIR / "ansible" / "inventories" / "generated" / "cluster.ini"
INSTALL_CLUSTER_PLAYBOOK = BASE_DIR / "ansible" / "playbooks" / "install_k3s_cluster.yml"
KUBECONFIG_HOST_PATH = Path.home() / ".kube" / "config"
CLUSTERS_FILE = DATA_DIR / "clusters.json"


def _ensure_clusters_file() -> None:
    """Đảm bảo file clusters.json tồn tại."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not CLUSTERS_FILE.exists():
        CLUSTERS_FILE.write_text("[]", encoding="utf-8")


def load_clusters() -> list[dict[str, Any]]:
    """Đọc danh sách cluster từ clusters.json."""
    return read_json(CLUSTERS_FILE, [], is_list_of_dicts)


def save_clusters(clusters: list[dict[str, Any]]) -> None:
    """Ghi danh sách cluster vào clusters.json."""
    if not is_list_of_dicts(clusters):
        raise ValueError("clusters must be a list of objects")
    write_json(CLUSTERS_FILE, clusters)


def create_cluster(selected_nodes: list[dict[str, str]], name: str | None = None) -> str:
    """
    Tạo một cluster mới và gán cluster_id cho tất cả server được chọn.
    Trả về cluster_id vừa tạo.
    """
    clusters = load_clusters()
    cluster_id = name or f"cluster-{datetime.now().strftime('%y%m%d%H%M%S')}"

    # Tìm node master để lấy IP
    master_ip = ""
    node_list: list[dict[str, Any]] = []
    for node in selected_nodes:
        server_id = node.get("server_id", "").strip()
        role = node.get("role", "Worker").strip().lower()
        server = find_server(server_id)
        if server:
            node_list.append({
                "server_id": server_id,
                "name": server.get("name", server_id),
                "ip": server.get("ip", ""),
                "role": role.capitalize(),
            })
            if role == "master":
                master_ip = server.get("ip", "")

    # Tạo cluster record
    cluster = {
        "id": cluster_id,
        "name": cluster_id,
        "master_ip": master_ip,
        "nodes": node_list,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "status": "active",
    }

    # Ghi cluster vào clusters.json
    clusters = [c for c in clusters if c.get("id") != cluster_id]
    clusters.append(cluster)
    save_clusters(clusters)

    # Cập nhật cluster_id cho từng server trong servers.json
    servers = load_servers()
    for node in node_list:
        for srv in servers:
            if srv["id"] == node["server_id"]:
                srv["cluster_id"] = cluster_id
                break
    save_servers(servers)

    return cluster_id


def _safe_host_alias(server: dict[str, Any]) -> str:
    raw = server.get("id") or server.get("name") or server.get("ip", "node")
    safe = "".join(ch.lower() if ch.isalnum() else "-" for ch in str(raw)).strip("-")
    return safe or "node"


def _server_inventory_line(server: dict[str, Any]) -> str:
    parts = [
        _safe_host_alias(server),
        f"ansible_host={server['ip']}",
        f"ansible_user={server['ssh_user']}",
        f"ansible_port={server.get('ssh_port', 22)}",
        "ansible_ssh_common_args='-o StrictHostKeyChecking=accept-new'",
    ]

    key_path = str(server.get("ssh_key_path", "")).strip()
    if key_path:
        expanded_key_path = Path(key_path).expanduser()
        if expanded_key_path.exists():
            parts.append(f"ansible_ssh_private_key_file={expanded_key_path}")

    return " ".join(parts)


def build_cluster_inventory(selected_nodes: list[dict[str, str]]) -> tuple[bool, str, str]:
    masters: list[dict[str, Any]] = []
    workers: list[dict[str, Any]] = []

    for node in selected_nodes:
        server_id = node.get("server_id", "").strip()
        role = node.get("role", "Worker").strip().lower()
        server = find_server(server_id)
        if not server:
            continue

        if role == "master":
            masters.append(server)
        else:
            workers.append(server)

    if not masters:
        return False, "Cần chọn ít nhất 1 máy Master.", ""

    master_ip = masters[0]["ip"]
    lines = [
        "[masters]",
        *[_server_inventory_line(server) for server in masters],
        "",
        "[workers]",
        *[_server_inventory_line(server) for server in workers],
        "",
        "[k3s_cluster:children]",
        "masters",
        "workers",
        "",
        "[k3s_cluster:vars]",
        "ansible_python_interpreter=/usr/bin/python3",
        "k3s_version=v1.29.6+k3s2",
        "k3s_token=k3s-cluster-secret-token-2026",
        f"k3s_server_url=https://{master_ip}:6443",
        "",
    ]

    content = "\n".join(lines)
    GENERATED_INVENTORY.parent.mkdir(parents=True, exist_ok=True)
    GENERATED_INVENTORY.write_text(content, encoding="utf-8")
    return True, f"Đã sinh inventory: {GENERATED_INVENTORY.relative_to(BASE_DIR)}", content


def install_kubernetes(selected_nodes: list[dict[str, str]], sudo_password: str = "") -> tuple[bool, str, str]:
    success, message, inventory_content = build_cluster_inventory(selected_nodes)
    if not success:
        return False, message, inventory_content

    if not INSTALL_CLUSTER_PLAYBOOK.exists() or INSTALL_CLUSTER_PLAYBOOK.stat().st_size == 0:
        return (
            False,
            "Chưa có nội dung playbook install_k3s_cluster.yml. Đã sinh inventory, nhưng chưa chạy cài đặt.",
            inventory_content,
        )

    command = [
        "ansible-playbook",
        "-i",
        str(GENERATED_INVENTORY),
        str(INSTALL_CLUSTER_PLAYBOOK),
    ]

    sudo_password = sudo_password.strip()
    extra_vars = (
        {"ansible_become_password": sudo_password}
        if sudo_password
        else {}
    )
    with temporary_ansible_extra_vars(extra_vars) as extra_vars_file:
        if extra_vars:
            command.extend(["--extra-vars", f"@{extra_vars_file}"])

        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=1800,
                check=False,
            )
            output = (completed.stdout + completed.stderr).strip()
            if completed.returncode == 0:
                kube_msg = _sync_kubeconfig_to_host(selected_nodes)

                # Tạo cluster record và gán cluster_id cho các server
                cluster_id = create_cluster(selected_nodes)
                cluster_msg = f"Đã tạo cluster {cluster_id} và gán cho {len(selected_nodes)} server."

                full_msg = "\n".join(
                    part for part in [
                        "Cài đặt Kubernetes/K3s bằng Ansible thành công.",
                        kube_msg,
                        cluster_msg,
                    ] if part
                )
                return True, full_msg, output
            return False, "Cài đặt Kubernetes/K3s thất bại. Xem log chi tiết bên dưới.", output
        except subprocess.TimeoutExpired as exc:
            output = ((exc.stdout or "") + (exc.stderr or "")).strip()
            return False, "Cài đặt Kubernetes/K3s timeout sau 30 phút.", output


def _sync_kubeconfig_to_host(selected_nodes: list[dict[str, str]]) -> str:
    master_server = None
    for node in selected_nodes:
        if node.get("role", "").strip().lower() == "master":
            server_id = node.get("server_id", "").strip()
            master_server = find_server(server_id)
            break

    if not master_server:
        return "Không tìm thấy master node, không thể sync kubeconfig."

    master_ip = master_server["ip"]
    ssh_user = master_server["ssh_user"]
    ssh_port = master_server.get("ssh_port", 22)
    key_path = str(master_server.get("ssh_key_path", "")).strip()

    ssh_command = [
        "ssh",
        "-p", str(ssh_port),
        "-o", "ConnectTimeout=10",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "BatchMode=yes",
    ]

    expanded_key = Path(key_path).expanduser() if key_path else None
    if expanded_key and expanded_key.exists():
        ssh_command.extend(["-i", str(expanded_key)])

    ssh_command.extend([
        f"{ssh_user}@{master_ip}",
        "sudo cat /etc/rancher/k3s/k3s.yaml",
    ])

    try:
        result = subprocess.run(ssh_command, capture_output=True, text=True, timeout=15, check=False)
        if result.returncode != 0:
            return f"Không thể lấy kubeconfig từ master {master_ip}: {result.stderr.strip()}"

        kubeconfig = result.stdout
        kubeconfig = kubeconfig.replace("127.0.0.1", master_ip)

        KUBECONFIG_HOST_PATH.parent.mkdir(parents=True, exist_ok=True)
        backup_path = KUBECONFIG_HOST_PATH.with_suffix(".config.bak")
        if KUBECONFIG_HOST_PATH.exists():
            shutil.copy2(KUBECONFIG_HOST_PATH, backup_path)

        KUBECONFIG_HOST_PATH.write_text(kubeconfig, encoding="utf-8")
        KUBECONFIG_HOST_PATH.chmod(0o600)

        return f"Đã sync kubeconfig từ master {master_ip} vào {KUBECONFIG_HOST_PATH}."
    except subprocess.TimeoutExpired:
        return f"Timeout khi lấy kubeconfig từ master {master_ip}."
    except OSError as exc:
        return f"Lỗi ghi kubeconfig: {exc}"
