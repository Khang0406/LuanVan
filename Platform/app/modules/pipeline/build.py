"""GitHub source build pipeline: clone → docker build → push to registry.

Uses Docker Hub credentials from environment variables or application-level
registry configuration. Falls back gracefully when credentials are missing.
"""

import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import BASE_DIR

BUILD_DIR = BASE_DIR / "app" / "data" / "builds"
DEFAULT_REGISTRY = "docker.io"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _ensure_build_dir() -> Path:
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    return BUILD_DIR


def _run_command(args: list[str], cwd: Path | None = None, timeout: int = 300) -> tuple[bool, str]:
    """Run a shell command and return (success, output)."""
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            cwd=str(cwd) if cwd else None,
        )
        output = (result.stdout + result.stderr).strip()
        return result.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, f"Command timed out after {timeout}s: {' '.join(args)}"
    except FileNotFoundError:
        return False, f"Command not found: {args[0]}"


def _get_registry_config(application: dict[str, Any]) -> dict[str, str]:
    """Extract registry configuration from application or environment variables.

    Priority:
    1. Application-level registry config (per-app credentials)
    2. Environment variables (global platform config)
    3. Docker Hub anonymous (no push)
    """
    registry = application.get("registry", {})

    config = {
        "registry": registry.get("url") or os.getenv("DOCKER_REGISTRY", DEFAULT_REGISTRY),
        "username": registry.get("username") or os.getenv("DOCKER_USERNAME", ""),
        "password": registry.get("password") or os.getenv("DOCKER_PASSWORD", ""),
        "token": registry.get("token") or os.getenv("DOCKER_TOKEN", ""),
    }

    # If no explicit registry config, try to parse from docker_image if already prefixed
    docker_image = application.get("docker_image", "")
    if config["registry"] == DEFAULT_REGISTRY:
        # Check if docker_image has a registry prefix (e.g., docker.io/username/image)
        if docker_image and "/" in docker_image and "." in docker_image.split("/")[0]:
            parts = docker_image.split("/")
            config["registry"] = parts[0]

    return config


def _has_valid_registry_credentials(registry_config: dict[str, str]) -> bool:
    """Check if registry credentials appear to be valid (not placeholder/description text).

    Returns True only if there is a username AND a password/token that looks like
    a real credential (not a long Vietnamese description or placeholder text).
    """
    username = registry_config.get("username", "").strip()
    password = registry_config.get("password", "").strip()
    token = registry_config.get("token", "").strip()

    if not username:
        return False

    credential = password or token
    if not credential:
        return False

    # Password looks like a description (long Vietnamese text with spaces) → not real
    if len(credential) > 40 and " " in credential:
        return False

    # Password is too short to be a real token/password (< 4 chars)
    if len(credential) < 4:
        return False

    return True


def _image_tag(application: dict[str, Any], service_name: str, registry_config: dict[str, str]) -> str:
    """Generate full image tag for the built image.

    Format: {registry}/{owner-or-username}/{app_name}-{service_name}:build-{timestamp}
    """
    registry = registry_config["registry"]
    owner = application.get("owner", "developer")
    app_name = application["id"]
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")

    # Remove docker.io/ prefix for consistency with Docker Hub conventions
    if not registry or registry == DEFAULT_REGISTRY:
        # Docker Hub: use username/app-name format
        username = registry_config["username"] or owner
        return f"{username}/{app_name}-{service_name}:build-{timestamp}"
    else:
        return f"{registry}/{owner}/{app_name}-{service_name}:build-{timestamp}"


def clone_repository(github_url: str, work_dir: Path) -> tuple[bool, str]:
    """Clone GitHub repository to work_dir.

    Returns (success, message).
    """
    work_dir.mkdir(parents=True, exist_ok=True)

    # Remove existing contents if any
    for item in work_dir.iterdir():
        if item.is_dir():
            shutil.rmtree(item, ignore_errors=True)
        else:
            item.unlink(missing_ok=True)

    # Clone the repository
    success, output = _run_command(
        ["git", "clone", "--depth", "1", github_url, "."],
        cwd=work_dir,
        timeout=180,
    )

    if not success:
        # Try with https:// prefix if missing
        if not github_url.startswith("http"):
            github_url = f"https://github.com/{github_url}"
            success, output = _run_command(
                ["git", "clone", "--depth", "1", github_url, "."],
                cwd=work_dir,
                timeout=180,
            )

    return success, output


def find_dockerfile(work_dir: Path) -> Path | None:
    """Find Dockerfile in work_dir.

    Checks in order:
    1. {work_dir}/Dockerfile
    2. {work_dir}/docker/Dockerfile
    3. {work_dir}/.docker/Dockerfile
    """
    candidates = [
        work_dir / "Dockerfile",
        work_dir / "docker" / "Dockerfile",
        work_dir / ".docker" / "Dockerfile",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def generate_default_dockerfile(work_dir: Path, service: dict[str, Any]) -> Path:
    """Generate a default Dockerfile for common project types.

    Detects language based on files present and creates appropriate Dockerfile.
    """
    dockerfile_path = work_dir / "Dockerfile"

    # Check for common project indicators
    has_package_json = (work_dir / "package.json").exists()
    has_requirements = (work_dir / "requirements.txt").exists()
    has_pyproject = (work_dir / "pyproject.toml").exists()
    has_go_mod = (work_dir / "go.mod").exists()
    has_pom = (work_dir / "pom.xml").exists()
    has_index_html = (work_dir / "index.html").exists()
    has_composer = (work_dir / "composer.json").exists()
    has_php_files = list(work_dir.rglob("*.php"))

    if has_composer:
        # PHP with Composer
        content = """FROM php:8.2-apache
WORKDIR /var/www/html
RUN apt-get update && apt-get install -y \\
    libzip-dev zip unzip git \\
    && docker-php-ext-install pdo pdo_mysql zip \\
    && a2enmod rewrite
COPY --from=composer:2 /usr/bin/composer /usr/bin/composer
COPY composer.json composer.lock ./
RUN composer install --no-dev --optimize-autoloader
COPY . .
RUN chown -R www-data:www-data /var/www/html
EXPOSE 80
"""
    elif has_php_files:
        # Plain PHP (no composer) - static/API PHP app
        content = """FROM php:8.2-apache
WORKDIR /var/www/html
RUN apt-get update && apt-get install -y \\
    libzip-dev zip \\
    && docker-php-ext-install pdo pdo_mysql zip mysqli \\
    && a2enmod rewrite
COPY . .
RUN chown -R www-data:www-data /var/www/html
EXPOSE 80
"""
    elif has_package_json:
        # Node.js / Next.js / React
        content = """FROM node:18-alpine
WORKDIR /app
COPY package*.json ./
RUN npm ci --only=production 2>/dev/null || npm install
COPY . .
EXPOSE 3000
CMD ["npm", "start"]
"""
    elif has_requirements or has_pyproject:
        # Python
        if has_requirements:
            install_cmd = "pip install --no-cache-dir -r requirements.txt"
        else:
            install_cmd = "pip install --no-cache-dir ."
        content = f"""FROM python:3.11-slim
WORKDIR /app
COPY . .
RUN {install_cmd}
EXPOSE 8000
CMD ["python", "app.py"]
"""
    elif has_go_mod:
        content = """FROM golang:1.21-alpine AS builder
WORKDIR /app
COPY go.mod go.sum ./
RUN go mod download
COPY . .
RUN go build -o app .

FROM alpine:3.18
WORKDIR /app
COPY --from=builder /app/app .
EXPOSE 8080
CMD ["./app"]
"""
    elif has_pom:
        content = """FROM maven:3.8-openjdk-17 AS builder
WORKDIR /app
COPY pom.xml .
RUN mvn dependency:go-offline
COPY src ./src
RUN mvn package -DskipTests

FROM openjdk:17-slim
WORKDIR /app
COPY --from=builder /app/target/*.jar app.jar
EXPOSE 8080
CMD ["java", "-jar", "app.jar"]
"""
    else:
        # Generic: serve static files or simple web app
        port = service.get("container_port", 80)
        content = f"""FROM nginx:stable-alpine
COPY . /usr/share/nginx/html
EXPOSE {port}
"""

    dockerfile_path.write_text(content, encoding="utf-8")
    return dockerfile_path


def _docker_available() -> bool:
    """Check if Docker daemon is reachable."""
    import subprocess
    for bin_name in ["docker", "docker.exe"]:
        try:
            result = subprocess.run(
                [bin_name, "version", "--format", "{{.Server.Version}}"],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0 and result.stdout.strip():
                return True
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
    return False


def scan_source_code(work_dir: Path) -> list[dict]:
    """Scan source code for common deployment anti-patterns.

    Returns a list of issues, each with keys:
      file, line, pattern, message, suggestion
    """
    import re

    issues: list[dict] = []

    # Patterns that cause failures when deployed to Kubernetes
    SCAN_RULES = [
        {
            "id": "localhost_url",
            "regex": re.compile(
                r'(https?://(localhost|127\.0\.0\.1)(:\d+)?(/[^\s\'"`)]*)?)',
                re.IGNORECASE,
            ),
            "extensions": {".js", ".html", ".php", ".ts", ".jsx", ".tsx"},
            "message": "URL hardcoded trỏ vào localhost — sẽ lỗi khi deploy lên Kubernetes",
            "suggestion": "Dùng relative path (vd: '../backend/api.php') hoặc biến môi trường thay vì URL tuyệt đối localhost",
        },
        {
            "id": "empty_db_password",
            "regex": re.compile(
                r'''\$password\s*=\s*['"]{2}''',
                re.IGNORECASE,
            ),
            "extensions": {".php"},
            "message": "Mật khẩu MySQL để trống ('') — kết nối sẽ thất bại nếu MySQL trên Kubernetes yêu cầu password",
            "suggestion": "Dùng getenv('DB_PASSWORD') ?: '' để đọc password từ biến môi trường được inject khi deploy",
        },
        {
            "id": "db_localhost",
            "regex": re.compile(
                r'''\$host\s*=\s*['"]localhost['"]''',
                re.IGNORECASE,
            ),
            "extensions": {".php"},
            "message": "Host MySQL hardcoded là 'localhost' — trong Kubernetes MySQL chạy pod riêng, không thể dùng localhost",
            "suggestion": "Dùng getenv('DB_HOST') ?: 'localhost' để nhận host từ biến môi trường (platform tự inject)",
        },
    ]

    scan_extensions = {ext for rule in SCAN_RULES for ext in rule["extensions"]}

    # ── Extra check: SQL tables referenced in PHP but no schema/init SQL file ──
    sql_files = list(work_dir.rglob("*.sql"))
    has_sql_init = any(
        f for f in sql_files
        if not any(
            p.startswith(".") or p in {"node_modules", "vendor"}
            for p in f.relative_to(work_dir).parts
        )
    )
    if not has_sql_init:
        import re as _re
        table_pattern = _re.compile(
            r"(?:FROM|INTO|UPDATE|TABLE)\s+`?([A-Za-z_][A-Za-z0-9_]+)`?",
            _re.IGNORECASE,
        )
        tables_found: dict[str, dict] = {}  # table_name -> {file, line, snippet}
        for php_file in work_dir.rglob("*.php"):
            rel_parts = php_file.relative_to(work_dir).parts
            if any(p.startswith(".") or p in {"node_modules", "vendor"} for p in rel_parts):
                continue
            try:
                text = php_file.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                for m in table_pattern.finditer(line):
                    tname = m.group(1)
                    if tname.upper() in {"SELECT", "WHERE", "SET", "FROM", "TABLE", "JOIN"}:
                        continue
                    if tname not in tables_found:
                        tables_found[tname] = {
                            "file": str(php_file.relative_to(work_dir)),
                            "line": lineno,
                            "snippet": line.strip()[:120],
                        }
        if tables_found:
            for tname, loc in tables_found.items():
                issues.append({
                    "file": loc["file"],
                    "line": loc["line"],
                    "rule": "missing_sql_schema",
                    "message": (
                        f"Bảng MySQL '{tname}' được dùng trong code nhưng không có file schema SQL (.sql) nào trong repo — "
                        f"bảng sẽ không tồn tại khi deploy, dẫn đến lỗi 'Table doesn't exist'"
                    ),
                    "suggestion": (
                        "Thêm file init.sql (hoặc schema.sql) vào repo chứa lệnh CREATE TABLE cho tất cả các bảng. "
                        "Platform sẽ tự động mount và chạy file này khi khởi động MySQL container."
                    ),
                    "snippet": loc["snippet"],
                })

    for file_path in work_dir.rglob("*"):
        # Skip hidden dirs (.git, .env), node_modules, vendor
        rel_parts = file_path.relative_to(work_dir).parts
        if any(p.startswith(".") or p in {"node_modules", "vendor", "__pycache__"} for p in rel_parts):
            continue

        if not file_path.is_file():
            continue

        suffix = file_path.suffix.lower()
        if suffix not in scan_extensions:
            continue

        try:
            text = file_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        for rule in SCAN_RULES:
            if suffix not in rule["extensions"]:
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                # Skip pure comment lines
                stripped = line.strip()
                if stripped.startswith("//") or stripped.startswith("#") or stripped.startswith("*"):
                    continue
                if rule["regex"].search(line):
                    issues.append({
                        "file": str(file_path.relative_to(work_dir)),
                        "line": lineno,
                        "rule": rule["id"],
                        "message": rule["message"],
                        "suggestion": rule["suggestion"],
                        "snippet": line.strip()[:120],
                    })

    return issues


def build_docker_image(
    work_dir: Path,
    image_tag: str,
    dockerfile: Path | None = None,
) -> tuple[bool, str]:
    """Build Docker image from work_dir.

    Args:
        work_dir: Directory containing source code and Dockerfile
        image_tag: Full image tag (e.g., username/app:tag)
        dockerfile: Path to Dockerfile (auto-detected if None)

    Returns (success, output).
    """
    if not _docker_available():
        return False, "Docker is not available — cannot build image"
    args = ["docker", "build"]

    if dockerfile and dockerfile.exists():
        args.extend(["-f", str(dockerfile)])

    args.extend(["-t", image_tag, "."])

    success, output = _run_command(args, cwd=work_dir, timeout=600)
    return success, output


def push_docker_image(
    image_tag: str,
    registry_config: dict[str, str],
) -> tuple[bool, str]:
    """Push Docker image to registry.

    Handles Docker Hub login if credentials provided.

    Returns (success, output).
    """
    if not _docker_available():
        return False, "Docker is not available — cannot push image"
    # Login to registry if credentials available
    username = registry_config.get("username", "")
    password = registry_config.get("password", "") or registry_config.get("token", "")
    registry = registry_config.get("registry", DEFAULT_REGISTRY)

    if username and password:
        login_success, login_output = _run_command(
            [
                "docker", "login", registry,
                "--username", username,
                "--password-stdin",
            ],
            timeout=60,
            # Can't use --password-stdin without piping, use --password
        )
        # Retry with --password flag
        if not login_success:
            login_success, login_output = _run_command(
                [
                    "docker", "login", registry,
                    "--username", username,
                    "--password", password,
                ],
                timeout=60,
            )
        if not login_success:
            return False, f"Docker login failed: {login_output}"

    # Push image
    success, output = _run_command(["docker", "push", image_tag], timeout=300)

    # Logout for security
    if username:
        _run_command(["docker", "logout", registry], timeout=10)

    return success, output


def build_from_github(
    application: dict[str, Any],
    pipeline_run: dict[str, Any],
) -> dict[str, Any]:
    """Full GitHub source build pipeline: clone → build → test → push.

    Updates pipeline_run stages in-place with real status and messages.

    Returns updated pipeline_run.
    """
    from app.modules.pipeline.engine import _update_stage, _save_pipeline_run
    from app.modules.applications.service import add_activity, save_application

    github_url = application.get("github_url", "")
    registry_config = _get_registry_config(application)
    build_id = f"{application['id']}-{pipeline_run['id'][-8:]}"
    work_dir = _ensure_build_dir() / build_id

    # === CLONE ================================================================
    _update_stage(pipeline_run, "SOURCE", "Running", f"Cloning {github_url}...")
    clone_success, clone_output = clone_repository(github_url, work_dir)

    if not clone_success:
        _update_stage(pipeline_run, "SOURCE", "Failed", f"Git clone failed: {clone_output[:200]}")
        add_activity(application, "PIPELINE", f"Git clone thất bại: {clone_output[:200]}", "Failed")
        _save_pipeline_run(pipeline_run)
        return pipeline_run

    _update_stage(pipeline_run, "SOURCE", "Done", f"Cloned {github_url} → {work_dir}")
    add_activity(application, "PIPELINE", f"SOURCE: Đã clone {github_url}", "Done")

    # === SCAN =================================================================
    _update_stage(pipeline_run, "BUILD", "Running", "Đang quét mã nguồn để phát hiện lỗi phổ biến...")
    scan_issues = scan_source_code(work_dir)
    if scan_issues:
        # Group by rule to create a compact summary
        by_rule: dict[str, list[dict]] = {}
        for issue in scan_issues:
            by_rule.setdefault(issue["rule"], []).append(issue)

        warning_lines: list[str] = []
        for rule_id, rule_issues in by_rule.items():
            for iss in rule_issues:
                warning_lines.append(
                    f"⚠ [{iss['file']}:{iss['line']}] {iss['message']}\n"
                    f"   Code: {iss['snippet']}\n"
                    f"   → {iss['suggestion']}"
                )

        summary = f"Phát hiện {len(scan_issues)} vấn đề trong mã nguồn:\n" + "\n".join(warning_lines)
        add_activity(application, "PIPELINE", summary, "Warning")
        # Log each issue individually so developer sees them clearly in activity feed
        for iss in scan_issues:
            add_activity(
                application,
                "SCAN",
                f"[{iss['file']}:{iss['line']}] {iss['message']} | Code: {iss['snippet']} | Gợi ý: {iss['suggestion']}",
                "Warning",
            )
    else:
        add_activity(application, "PIPELINE", "SCAN: Không phát hiện vấn đề nào trong mã nguồn", "Done")

    # === BUILD ================================================================
    services = application.get("services", [])
    built_images: list[tuple[str, str]] = []  # [(service_name, image_tag)]

    for service in services:
        service_name = service["name"]
        image_tag = _image_tag(application, service_name, registry_config)

        _update_stage(pipeline_run, "BUILD", "Running", f"Building {service_name} → {image_tag}...")

        dockerfile = find_dockerfile(work_dir)
        if not dockerfile:
            dockerfile = generate_default_dockerfile(work_dir, service)
            add_activity(
                application,
                "BUILD",
                f"Không tìm thấy Dockerfile trong repo, đã tự động tạo Dockerfile cho {service_name}",
                "Done",
            )

        build_success, build_output = build_docker_image(work_dir, image_tag, dockerfile)
        if not build_success:
            _update_stage(pipeline_run, "BUILD", "Failed", f"Docker build failed for {service_name}: {build_output[:200]}")
            add_activity(application, "PIPELINE", f"BUILD thất bại cho {service_name}: {build_output[:200]}", "Failed")
            _save_pipeline_run(pipeline_run)
            return pipeline_run

        built_images.append((service_name, image_tag))
        add_activity(application, "BUILD", f"Đã build Docker image {image_tag} cho {service_name}", "Done")

    _update_stage(
        pipeline_run, "BUILD", "Done",
        f"Built {len(built_images)} image(s): {', '.join(tag for _, tag in built_images)}",
    )
    add_activity(application, "PIPELINE", f"BUILD: Đã build {len(built_images)} image thành công", "Done")

    # === TEST =================================================================
    _update_stage(pipeline_run, "TEST", "Running", "Testing built images...")
    test_results: list[str] = []
    for service_name, image_tag in built_images:
        # Quick smoke test: run container and check if it starts
        test_container_name = f"test-{application['id']}-{service_name}-{int(time.time())}"
        success, output = _run_command(
            [
                "docker", "run", "--rm", "--name", test_container_name,
                "-d", image_tag,
            ],
            timeout=60,
        )
        if success:
            # Container started, give it a moment then check
            time.sleep(2)
            check_success, check_output = _run_command(
                ["docker", "inspect", test_container_name, "--format", "{{.State.Status}}"],
                timeout=10,
            )
            if check_success and "running" in check_output.lower():
                test_results.append(f"{service_name}: ✓ running")
            else:
                test_results.append(f"{service_name}: started but status={check_output[:50]}")
            # Stop and remove test container
            _run_command(["docker", "stop", test_container_name], timeout=10)
        else:
            test_results.append(f"{service_name}: ✗ failed to start — {output[:100]}")

    _update_stage(pipeline_run, "TEST", "Done", "; ".join(test_results))
    add_activity(application, "PIPELINE", f"TEST: {', '.join(test_results)}", "Done" if all("✓" in r for r in test_results) else "Warning")

    # === PUSH =================================================================
    # Check if registry credentials are valid before attempting push
    if not _has_valid_registry_credentials(registry_config):
        _update_stage(pipeline_run, "PUSH", "Skipped",
                      "No valid registry credentials — skip push (will use public image + hostPath fallback)")
        add_activity(application, "PIPELINE", "PUSH: Không có credentials registry hợp lệ — bỏ qua push", "Done")
        # Store work_dir for fallback in _run_pipeline
        pipeline_run["_work_dir"] = str(work_dir)
        return pipeline_run

    _update_stage(pipeline_run, "PUSH", "Running", "Pushing images to registry...")
    push_results: list[str] = []
    all_pushed = True

    for service_name, image_tag in built_images:
        push_success, push_output = push_docker_image(image_tag, registry_config)
        if push_success:
            push_results.append(f"{service_name}: ✓ pushed {image_tag}")
        else:
            push_results.append(f"{service_name}: ✗ push failed — {push_output[:100]}")
            all_pushed = False

    if all_pushed:
        _update_stage(pipeline_run, "PUSH", "Done", "; ".join(push_results))
        add_activity(application, "PIPELINE", "PUSH: Đã push tất cả images thành công", "Done")
    else:
        _update_stage(pipeline_run, "PUSH", "Failed", "; ".join(push_results))
        add_activity(application, "PIPELINE", f"PUSH: Có image push thất bại: {'; '.join(push_results)}", "Failed")
        _save_pipeline_run(pipeline_run)
        return pipeline_run

    # Update application services with built image tags
    for service_name, image_tag in built_images:
        for svc in application["services"]:
            if svc["name"] == service_name:
                svc["image"] = image_tag

    pipeline_run["image"] = built_images[0][1] if built_images else ""
    application["docker_image"] = built_images[0][1] if built_images else application.get("docker_image", "")
    save_application(application)

    # Store work_dir on pipeline_run so _run_pipeline can sync to worker if needed
    pipeline_run["_work_dir"] = str(work_dir)

    return pipeline_run
