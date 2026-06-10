from pathlib import Path


def host_line(node):
    server = node.server
    parts = [
        server.name.replace(" ", "-"),
        f"ansible_host={server.ip}",
        f"ansible_port={server.ssh_port}",
        f"ansible_user={server.ssh_user}",
    ]
    if server.ssh_key_path:
        parts.append(f"ansible_ssh_private_key_file={server.ssh_key_path}")
    if server.ssh_password:
        parts.append(f"ansible_password={server.ssh_password}")
        parts.append(f"ansible_become_password={server.ssh_password}")
    return " ".join(parts)


def render_inventory(cluster, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    masters = [node for node in cluster.nodes if node.role == "master"]
    workers = [node for node in cluster.nodes if node.role == "worker"]
    lines = ["[k3s_master]"]
    lines.extend(host_line(node) for node in masters)
    lines.extend(["", "[k3s_workers]"])
    lines.extend(host_line(node) for node in workers)
    lines.extend(["", "[k3s_cluster:children]", "k3s_master", "k3s_workers", ""])
    path = output_dir / f"cluster_{cluster.id}.ini"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
