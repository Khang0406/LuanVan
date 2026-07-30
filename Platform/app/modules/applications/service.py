import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import BASE_DIR
from app.delivery_store import list_applications as list_applications_from_db
from app.delivery_store import migrate_default_json_state, replace_applications
from app.json_store import is_list_of_dicts, mask_secrets, read_json, write_json
from app.registry_credentials import save_registry_credential
from app.webhook_secrets import save_webhook_secret

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
                    "min_replicas": 2,
                    "max_replicas": 5,
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
    migrate_default_json_state()
    applications = list_applications_from_db()
    return applications or _default_applications()


def save_applications(applications: list[dict[str, Any]]) -> None:
    if not is_list_of_dicts(applications):
        raise ValueError("applications must be a list of objects")
    migrate_default_json_state()
    replace_applications(applications)


def can_access_application(application: dict[str, Any], user: Any) -> bool:
    """Admins/Viewers can read all apps; Developers are isolated by ownership."""
    if getattr(user, "role", "") in {"Admin", "Viewer"}:
        return True
    return application.get("user_id") == getattr(user, "id", None)


def load_accessible_applications(user: Any) -> list[dict[str, Any]]:
    return [app for app in load_applications() if can_access_application(app, user)]


def find_application(application_id: str) -> dict[str, Any] | None:
    return next((app for app in load_applications() if app["id"] == application_id), None)


def find_accessible_application(application_id: str, user: Any) -> dict[str, Any] | None:
    application = find_application(application_id)
    if application and can_access_application(application, user):
        return application
    return None


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


def parse_env_map(raw_data: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in raw_data.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        result[key.strip()] = value.strip()
    return result


def parse_env_lines(raw_env: str) -> list[dict[str, str]]:
    env: list[dict[str, str]] = []
    for line in raw_env.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env.append({"name": key.strip(), "value": value.strip()})
    return env

def _bounded_int(value: Any, field: str, default: int, minimum: int, maximum: int) -> int:
    raw = default if value in (None, "") else value
    try:
        parsed = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} phải là số nguyên.") from exc
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{field} phải nằm trong khoảng {minimum}-{maximum}.")
    return parsed



def add_activity(application: dict[str, Any], event_type: str, message: str, status: str = "Done") -> None:
    application.setdefault("activity_logs", []).insert(
        0,
        {
            "time": _now(),
            "type": event_type,
            "message": mask_secrets(message),
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
        raw_cmd = form.get("service_command", "").strip()
        raw_args = form.get("service_args", "").strip()
        return [
            {
                "name": slugify(form.get("service_name", "web").strip() or "web"),
                "image": image,
                "container_port": _bounded_int(form.get("container_port"), "Container port", 80, 1, 65535),
                "replicas": _bounded_int(form.get("replicas"), "Replicas", 1, 1, 100),
                "service_type": form.get("service_type", "NodePort").strip(),
                "node_port": form.get("node_port", "").strip(),
                "env": parse_env_lines(form.get("env", "")),
                "command": [c.strip() for c in raw_cmd.split(",") if c.strip()],
                "args": [c.strip() for c in raw_args.split(",") if c.strip()],
                "public": form.get("service_public", "on") == "on",
                "cpu_request": form.get("cpu_request", "100m").strip() or "100m",
                "cpu_limit": form.get("cpu_limit", "500m").strip() or "500m",
                "memory_request": form.get("memory_request", "128Mi").strip() or "128Mi",
                "memory_limit": form.get("memory_limit", "512Mi").strip() or "512Mi",
                "min_replicas": _bounded_int(form.get("min_replicas"), "Min replicas", 2, 1, 100),
                "max_replicas": _bounded_int(form.get("max_replicas"), "Max replicas", 5, 1, 100),
                "autoscaling": form.get("autoscaling") == "on",
                "cpu_threshold": _bounded_int(form.get("cpu_threshold"), "CPU threshold", 50, 1, 100),
            }
        ]

    images = form.getlist("service_images")
    ports = form.getlist("service_ports")
    replicas_list = form.getlist("service_replicas")
    types = form.getlist("service_types")
    node_ports = form.getlist("service_node_ports")
    envs = form.getlist("service_envs")
    commands = form.getlist("service_commands")
    args_list = form.getlist("service_args")
    publics = form.getlist("service_publics")

    services: list[dict[str, Any]] = []
    for i, name_raw in enumerate(names):
        name = slugify(name_raw.strip() or f"svc-{i+1}")
        image = images[i].strip() if i < len(images) else ""
        if not image:
            default_img = form.get("docker_image", "").strip()
            image = default_img or f"{slugify(form.get('owner', '').strip())}/{slugify(form.get('name', '').strip())}:latest"
        port = _bounded_int(ports[i] if i < len(ports) else None, f"Service {i + 1} port", 80, 1, 65535)
        replicas = _bounded_int(replicas_list[i] if i < len(replicas_list) else None, f"Service {i + 1} replicas", 1, 1, 100)
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
            "public": publics[i] == "on" if i < len(publics) else True,
            "env": parse_env_lines(env_raw),
            "command": [c.strip() for c in (commands[i].split(",") if i < len(commands) and commands[i] else "") if c.strip()],
            "args": [c.strip() for c in (args_list[i].split(",") if i < len(args_list) and args_list[i] else "") if c.strip()],
            "cpu_request": form.get("cpu_request", "100m").strip() or "100m",
            "cpu_limit": form.get("cpu_limit", "500m").strip() or "500m",
            "memory_request": form.get("memory_request", "128Mi").strip() or "128Mi",
            "memory_limit": form.get("memory_limit", "512Mi").strip() or "512Mi",
            "min_replicas": _bounded_int(form.get("min_replicas"), "Min replicas", max(replicas, 2), 1, 100),
            "max_replicas": _bounded_int(form.get("max_replicas"), "Max replicas", max(replicas, 5), 1, 100),
            "autoscaling": form.get("autoscaling") == "on",
            "cpu_threshold": _bounded_int(form.get("cpu_threshold"), "CPU threshold", 50, 1, 100),
        })

    return services


def create_application(form: dict[str, Any], user: Any) -> dict[str, Any]:
    name = form.get("name", "").strip()
    application_id = slugify(name)
    requested_namespace = form.get("namespace", "").strip()
    if getattr(user, "is_admin", False):
        namespace = requested_namespace or application_id
    else:
        namespace = f"user-{user.id}-{application_id}"
    owner = form.get("owner", "").strip() or "developer"
    source_type = form.get("source_type", "docker").strip()
    docker_image = form.get("docker_image", "").strip()
    github_url = form.get("github_url", "").strip()
    if not name:
        raise ValueError("Tên application không được để trống.")
    if source_type not in {"docker", "github"}:
        raise ValueError("Source type chỉ có thể là docker hoặc github.")
    if source_type == "github":
        from app.modules.pipeline.build import (
            validate_git_ref,
            validate_repository_path,
            validate_repository_url,
        )

        github_url = validate_repository_url(github_url)
        validate_git_ref(form.get("default_branch", "main"), "Default branch")
        if form.get("requested_ref", "").strip():
            validate_git_ref(form.get("requested_ref", ""), "Commit/ref")
        validate_repository_path(form.get("build_context", "."), "Build context")
        validate_repository_path(form.get("dockerfile_path", "Dockerfile"), "Dockerfile path")

    services = _parse_services_from_form(form)
    if not services:
        raise ValueError("Application phải có ít nhất một service.")
    for service in services:
        if service["min_replicas"] > service["max_replicas"]:
            raise ValueError("Min replicas không được lớn hơn max replicas.")
        if service.get("node_port"):
            _bounded_int(service["node_port"], "NodePort", 30000, 30000, 32767)
    image = docker_image or services[0]["image"]

    # Registry config for GitHub source builds
    # GitHub builds inherit the Platform-managed registry credential by
    # default. Legacy form fields remain accepted as an explicit override.
    registry: dict[str, Any] = {"inherit_platform": True}
    registry_url = form.get("registry_url", "").strip()
    registry_username = form.get("registry_username", "").strip()
    registry_password = form.get("registry_password", "").strip()
    if registry_url or registry_username or registry_password:
        registry = {
            "url": registry_url or "docker.io",
            "username": registry_username,
            "inherit_platform": False,
        }
        if registry_password:
            registry["credential_ref"] = save_registry_credential(
                f"user:{user.id}", registry_username, registry_password, registry_url or "docker.io"
            )

    applications = load_applications()
    if any(app.get("id") == application_id for app in applications):
        raise ValueError(f"Application '{application_id}' đã tồn tại.")
    if any(app.get("namespace") == namespace for app in applications):
        raise ValueError(f"Namespace '{namespace}' đã được application khác sử dụng.")

    application = {
        "id": application_id,
        "name": name,
        "owner": owner,
        "user_id": user.id,
        "namespace": namespace,
        "source_type": source_type,
        "docker_image": docker_image,
        "github_url": github_url,
        "default_branch": form.get("default_branch", "main").strip() or "main",
        "requested_ref": form.get("requested_ref", "").strip(),
        "build_context": form.get("build_context", ".").strip() or ".",
        "dockerfile_path": form.get("dockerfile_path", "Dockerfile").strip() or "Dockerfile",
        "deployment_mode": form.get("deployment_mode", "production").strip() or "production",
        "development_fallback_enabled": form.get("development_fallback_enabled") == "on",
        "registry": registry,
        "description": form.get("description", "").strip(),
        "config_data": parse_env_map(form.get("config_data", "")),
        "secrets": [],
        "services": services,
        "status": "Draft",
        "url": "",
        "last_deployed_at": "",
        "created_at": _now(),
        "updated_at": _now(),
        "activity_logs": [],
    }

    secret_keys_raw = form.get("secret_keys", "").strip()
    secret_values_raw = form.get("secret_values", "").strip()
    if secret_keys_raw and secret_values_raw:
        from app.secret_store import save_secret
        keys_list = [k.strip() for k in secret_keys_raw.split(",") if k.strip()]
        vals_list = [v.strip() for v in secret_values_raw.split(",") if v.strip()]
        for i, key in enumerate(keys_list):
            val = vals_list[i] if i < len(vals_list) else ""
            if key and val:
                save_secret(application_id, key, val)
                application["secrets"].append(key)
    webhook_secret = form.get("webhook_secret", "").strip()
    if source_type == "github" and webhook_secret:
        application["webhook_secret_ref"] = save_webhook_secret(application_id, webhook_secret)

    if source_type == "github":
        add_activity(application, "CREATE", f"Tạo application từ GitHub repository {github_url}.", "Ready")
        if registry:
            add_activity(application, "CONFIG", f"Đã cấu hình Docker registry {registry.get('url', 'docker.io')} cho build/push.", "Done")
        add_activity(application, "PIPELINE", "Chờ build Docker image từ GitHub source.", "Pending")
    else:
        add_activity(application, "CREATE", f"Tạo application từ Docker image {image}.", "Ready")

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
