import json
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import BASE_DIR

DATA_DIR = BASE_DIR / "app" / "data"
APPLICATIONS_FILE = DATA_DIR / "applications.json"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", value.lower().strip())
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug or "application"


def _default_applications() -> list[dict[str, Any]]:
    return [
        {
            "id": "demo-nginx",
            "name": "demo-nginx",
            "owner": "team-demo",
            "namespace": "demo-nginx",
            "source_type": "docker",
            "docker_image": "nginx:stable-alpine",
            "github_url": "",
            "description": "Demo application deployed from Docker Hub image.",
            "services": [
                {
                    "name": "web",
                    "image": "nginx:stable-alpine",
                    "container_port": 80,
                    "replicas": 1,
                    "service_type": "NodePort",
                    "node_port": "",
                    "env": [],
                    "cpu_request": "100m",
                    "cpu_limit": "500m",
                    "memory_request": "128Mi",
                    "memory_limit": "512Mi",
                    "min_replicas": 1,
                    "max_replicas": 3,
                    "autoscaling": False,
                    "cpu_threshold": 70,
                }
            ],
            "status": "Draft",
            "url": "",
            "last_deployed_at": "",
            "created_at": _now(),
            "updated_at": _now(),
            "activity_logs": [
                {
                    "time": _now(),
                    "type": "CREATE",
                    "message": "Application mẫu được tạo từ Docker image nginx:stable-alpine.",
                    "status": "Done",
                }
            ],
        }
    ]


def ensure_applications_file() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not APPLICATIONS_FILE.exists():
        save_applications(_default_applications())


def load_applications() -> list[dict[str, Any]]:
    ensure_applications_file()
    with APPLICATIONS_FILE.open("r", encoding="utf-8") as file:
        return json.load(file)


def save_applications(applications: list[dict[str, Any]]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with APPLICATIONS_FILE.open("w", encoding="utf-8") as file:
        json.dump(applications, file, ensure_ascii=False, indent=2)


def find_application(application_id: str) -> dict[str, Any] | None:
    return next((app for app in load_applications() if app["id"] == application_id), None)


def save_application(updated_application: dict[str, Any]) -> None:
    applications = load_applications()
    for index, application in enumerate(applications):
        if application["id"] == updated_application["id"]:
            updated_application["updated_at"] = _now()
            applications[index] = updated_application
            break
    else:
        applications.append(updated_application)
    save_applications(applications)


def parse_env_lines(raw_env: str) -> list[dict[str, str]]:
    env: list[dict[str, str]] = []
    for line in raw_env.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env.append({"name": key.strip(), "value": value.strip()})
    return env


def add_activity(application: dict[str, Any], event_type: str, message: str, status: str = "Done") -> None:
    application.setdefault("activity_logs", []).insert(
        0,
        {
            "time": _now(),
            "type": event_type,
            "message": message,
            "status": status,
        },
    )


def _parse_services_from_form(form: dict[str, Any]) -> list[dict[str, Any]]:
    """Parse danh sách services từ form multi-service.

    Form gửi các field dạng array:
      service_names[], service_images[], service_ports[], service_replicas[],
      service_types[], service_node_ports[], service_envs[]

    Fallback: nếu không có service_names[] thì parse single-service cũ.
    """
    names = form.getlist("service_names")
    if not names:
        # fallback single-service form cũ
        default_image = form.get("docker_image", "").strip()
        image = default_image or f"{slugify(form.get('owner', '').strip())}/{slugify(form.get('name', '').strip())}:latest"
        return [
            {
                "name": slugify(form.get("service_name", "web").strip() or "web"),
                "image": image,
                "container_port": int(form.get("container_port") or 80),
                "replicas": int(form.get("replicas") or 1),
                "service_type": form.get("service_type", "NodePort").strip(),
                "node_port": form.get("node_port", "").strip(),
                "env": parse_env_lines(form.get("env", "")),
                "cpu_request": form.get("cpu_request", "100m").strip() or "100m",
                "cpu_limit": form.get("cpu_limit", "500m").strip() or "500m",
                "memory_request": form.get("memory_request", "128Mi").strip() or "128Mi",
                "memory_limit": form.get("memory_limit", "512Mi").strip() or "512Mi",
                "min_replicas": int(form.get("min_replicas") or 1),
                "max_replicas": int(form.get("max_replicas") or 3),
                "autoscaling": form.get("autoscaling") == "on",
                "cpu_threshold": int(form.get("cpu_threshold") or 70),
            }
        ]

    images = form.getlist("service_images")
    ports = form.getlist("service_ports")
    replicas_list = form.getlist("service_replicas")
    types = form.getlist("service_types")
    node_ports = form.getlist("service_node_ports")
    envs = form.getlist("service_envs")

    services: list[dict[str, Any]] = []
    for i, name_raw in enumerate(names):
        name = slugify(name_raw.strip() or f"svc-{i+1}")
        image = images[i].strip() if i < len(images) else ""
        if not image:
            default_img = form.get("docker_image", "").strip()
            image = default_img or f"{slugify(form.get('owner', '').strip())}/{slugify(form.get('name', '').strip())}:latest"
        port = int(ports[i]) if i < len(ports) and ports[i] else 80
        replicas = int(replicas_list[i]) if i < len(replicas_list) and replicas_list[i] else 1
        svc_type = types[i].strip() if i < len(types) and types[i] else "NodePort"
        node_port = node_ports[i].strip() if i < len(node_ports) else ""
        env_raw = envs[i].strip() if i < len(envs) else ""
        services.append({
            "name": name,
            "image": image,
            "container_port": port,
            "replicas": replicas,
            "service_type": svc_type,
            "node_port": node_port,
            "env": parse_env_lines(env_raw),
            "cpu_request": form.get("cpu_request", "100m").strip() or "100m",
            "cpu_limit": form.get("cpu_limit", "500m").strip() or "500m",
            "memory_request": form.get("memory_request", "128Mi").strip() or "128Mi",
            "memory_limit": form.get("memory_limit", "512Mi").strip() or "512Mi",
            "min_replicas": int(form.get("min_replicas") or max(replicas, 1)),
            "max_replicas": int(form.get("max_replicas") or max(replicas, 3)),
            "autoscaling": form.get("autoscaling") == "on",
            "cpu_threshold": int(form.get("cpu_threshold") or 70),
        })

    return services


def create_application(form: dict[str, Any]) -> dict[str, Any]:
    name = form.get("name", "").strip()
    namespace = form.get("namespace", "").strip() or slugify(name)
    owner = form.get("owner", "").strip() or "developer"
    source_type = form.get("source_type", "docker").strip()
    docker_image = form.get("docker_image", "").strip()
    github_url = form.get("github_url", "").strip()

    services = _parse_services_from_form(form)
    image = docker_image or services[0]["image"]

    # Registry config for GitHub source builds
    registry: dict[str, str] = {}
    registry_url = form.get("registry_url", "").strip()
    registry_username = form.get("registry_username", "").strip()
    registry_password = form.get("registry_password", "").strip()
    if registry_url or registry_username or registry_password:
        registry = {
            "url": registry_url,
            "username": registry_username,
            "password": registry_password,
        }

    application = {
        "id": slugify(name),
        "name": name,
        "owner": owner,
        "namespace": namespace,
        "source_type": source_type,
        "docker_image": docker_image,
        "github_url": github_url,
        "registry": registry,
        "description": form.get("description", "").strip(),
        "services": services,
        "status": "Draft",
        "url": "",
        "last_deployed_at": "",
        "created_at": _now(),
        "updated_at": _now(),
        "activity_logs": [],
    }

    if source_type == "github":
        add_activity(application, "CREATE", f"Tạo application từ GitHub repository {github_url}.", "Ready")
        if registry:
            add_activity(application, "CONFIG", f"Đã cấu hình Docker registry {registry.get('url', 'docker.io')} cho build/push.", "Done")
        add_activity(application, "PIPELINE", "Chờ build Docker image từ GitHub source.", "Pending")
    else:
        add_activity(application, "CREATE", f"Tạo application từ Docker image {image}.", "Ready")

    applications = [app for app in load_applications() if app["id"] != application["id"]]
    applications.append(application)
    save_applications(applications)
    return application


def build_pipeline_steps(application: dict[str, Any]) -> list[dict[str, str]]:
    """Build pipeline steps from latest pipeline run data (real) or fallback to static view."""
    try:
        from app.modules.pipeline.engine import get_latest_pipeline_run

        latest = get_latest_pipeline_run(application["id"])
        if latest and latest.get("stages"):
            steps: list[dict[str, str]] = []
            for stage in latest["stages"]:
                steps.append({
                    "name": stage["name"],
                    "status": stage["status"],
                    "note": stage.get("message", ""),
                })
            return steps
    except Exception:
        pass  # fallback to static view below

    source_type = application.get("source_type", "docker")
    status = application.get("status", "Draft")
    deployed = status in {"Running", "Deployed"}
    failed = "Failed" in status

    steps = []
    if source_type == "github":
        steps.extend(
            [
                {"name": "SOURCE", "status": "Done", "note": application.get("github_url", "")},
                {"name": "BUILD", "status": "Planned", "note": "Docker build/push sẽ được mở rộng ở phase sau"},
                {"name": "TEST", "status": "Planned", "note": "Container health check"},
                {"name": "PUSH", "status": "Planned", "note": "Docker Hub hoặc private registry"},
            ]
        )
    else:
        steps.append({"name": "SOURCE", "status": "Done", "note": application["services"][0]["image"]})

    steps.extend(
        [
            {"name": "DEPLOY", "status": "Done" if deployed else ("Failed" if failed else "Pending"), "note": "kubectl apply lên K3s cluster"},
            {"name": "VERIFY", "status": "Running" if deployed else ("Failed" if failed else "Waiting"), "note": "Pods, Service, health check"},
        ]
    )
    return steps


def build_github_image(application: dict[str, Any]) -> tuple[bool, str]:
    """Placeholder-ready GitHub build step.

    The project can later add Docker Hub credentials and run clone/build/push here.
    For now this records an explainable pipeline state instead of pretending to deploy source directly.
    """
    if application.get("source_type") != "github":
        return True, "Application dùng Docker image, không cần build từ GitHub."

    github_url = application.get("github_url", "")
    if not github_url:
        return False, "Thiếu GitHub repository URL."

    add_activity(application, "BUILD", "GitHub build image chưa bật: cần Docker Hub token/registry để build và push image.", "Planned")
    save_application(application)
    return False, "GitHub build image đang ở trạng thái planned. Hãy dùng Docker image để deploy thật trong phase hiện tại."


def delete_application(application_id: str) -> bool:
    """Xóa application khỏi ứng dụng và dọn dẹp namespace Kubernetes nếu có cluster."""
    applications = load_applications()
    application = next((a for a in applications if a["id"] == application_id), None)
    if not application:
        return False

    # Dọn dẹp namespace trên K3s nếu application đã deploy
    namespace = application.get("namespace") or application["id"]
    try:
        # Kiểm tra kubectl có sẵn không
        subprocess.run(["kubectl", "delete", "namespace", namespace], capture_output=True, text=True, timeout=10)
    except Exception:
        pass  # Cluster có thể không chạy

    # Xóa khỏi applications.json
    applications = [a for a in applications if a["id"] != application_id]
    save_applications(applications)

    # Xóa build artifacts nếu có
    builds_dir = DATA_DIR / "builds"
    if builds_dir.exists():
        import shutil
        for build_dir in builds_dir.iterdir():
            if build_dir.name.startswith(f"{application_id}-"):
                shutil.rmtree(build_dir, ignore_errors=True)
    return True


def summarize_runtime(application: dict[str, Any]) -> dict[str, Any]:
    services = application.get("services", [])
    total_replicas = sum(int(service.get("replicas", 0)) for service in services)
    return {
        "services": len(services),
        "replicas": total_replicas,
        "cpu_limit": ", ".join(service.get("cpu_limit", "-") for service in services),
        "memory_limit": ", ".join(service.get("memory_limit", "-") for service in services),
    }