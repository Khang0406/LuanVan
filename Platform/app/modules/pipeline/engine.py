import json
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import BASE_DIR
from app.modules.applications.service import add_activity, find_application, save_application
from app.modules.deployments.kubectl import deploy_application, delete_application_workloads
from app.modules.pipeline.build import build_from_github

DATA_DIR = BASE_DIR / "app" / "data"
PIPELINE_FILE = DATA_DIR / "pipeline_runs.json"

PIPELINE_STAGES = [
    "SOURCE",
    "BUILD",
    "TEST",
    "PUSH",
    "DEPLOY",
    "VERIFY",
]


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _ensure_pipeline_file() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not PIPELINE_FILE.exists():
        PIPELINE_FILE.write_text("[]", encoding="utf-8")


def load_pipeline_runs(application_id: str | None = None) -> list[dict[str, Any]]:
    _ensure_pipeline_file()
    with PIPELINE_FILE.open("r", encoding="utf-8") as f:
        runs = json.load(f)

    if application_id:
        runs = [r for r in runs if r["application_id"] == application_id]
    return sorted(runs, key=lambda r: r["created_at"], reverse=True)


def save_pipeline_runs(runs: list[dict[str, Any]]) -> None:
    _ensure_pipeline_file()
    with PIPELINE_FILE.open("w", encoding="utf-8") as f:
        json.dump(runs, f, ensure_ascii=False, indent=2)


def _save_pipeline_run(run: dict[str, Any]) -> None:
    runs = load_pipeline_runs()
    for i, existing in enumerate(runs):
        if existing["id"] == run["id"]:
            runs[i] = run
            save_pipeline_runs(runs)
            return
    runs.append(run)
    save_pipeline_runs(runs)


def _update_stage(run: dict[str, Any], stage_name: str, status: str, message: str = "") -> dict[str, Any]:
    now = _now()
    for stage in run["stages"]:
        if stage["name"] == stage_name:
            stage["status"] = status
            stage["message"] = message
            if status in ("Running",) and not stage["started_at"]:
                stage["started_at"] = now
            if status in ("Done", "Failed", "Skipped"):
                stage["finished_at"] = now
            break
    run["updated_at"] = now
    _save_pipeline_run(run)
    return run


def _deploy_mysql_if_needed(
    application: dict[str, Any],
    source_dir: Path | None = None,
    pipeline_run: dict[str, Any] | None = None,
) -> tuple[str, str, str] | None:
    """Deploy MySQL + import schema if PHP project has a .sql file.

    Uses ConfigMap + /docker-entrypoint-initdb.d/ (MySQL's native auto-import)
    instead of fragile kubectl exec import. This guarantees SQL schema is
    applied on every fresh MySQL initialization.

    Returns (mysql_name, db_name, mysql_password) if MySQL was deployed, None otherwise.
    """
    import subprocess

    if source_dir is None or not source_dir.exists():
        return None

    sql_files = list(source_dir.glob("*.sql")) + list(source_dir.rglob("*.sql"))
    if not sql_files:
        return None  # no SQL, skip

    sql_file = sql_files[0]
    sql_basename = sql_file.name
    sql_content = sql_file.read_text(encoding="utf-8")
    # Indent SQL content by 4 spaces for YAML block scalar embedding
    indented_sql = "\n".join("    " + line for line in sql_content.splitlines())

    db_name = "map-project"
    mysql_password = "luanvan123"
    namespace = application["namespace"]
    app_id = application["id"]
    mysql_name = f"{app_id}-mysql"
    cm_name = f"{mysql_name}-init-sql"

    # 1) Tạo Kubernetes manifest cho MySQL: ConfigMap (SQL schema) +
    #    PersistentVolumeClaim + Deployment (với volume mount initdb) + Service
    mysql_manifest = f"""apiVersion: v1
kind: Namespace
metadata:
  name: {namespace}
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: {cm_name}
  namespace: {namespace}
  labels:
    app.kubernetes.io/name: {mysql_name}
    app.kubernetes.io/part-of: {app_id}
data:
  {sql_basename}: |
{indented_sql}
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: {mysql_name}-pvc
  namespace: {namespace}
spec:
  accessModes:
    - ReadWriteOnce
  resources:
    requests:
      storage: 1Gi
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {mysql_name}
  namespace: {namespace}
  labels:
    app.kubernetes.io/name: {mysql_name}
    app.kubernetes.io/part-of: {app_id}
spec:
  replicas: 1
  selector:
    matchLabels:
      app.kubernetes.io/name: {mysql_name}
  template:
    metadata:
      labels:
        app.kubernetes.io/name: {mysql_name}
        app.kubernetes.io/part-of: {app_id}
    spec:
      containers:
        - name: mysql
          image: mysql:8.0
          env:
            - name: MYSQL_ROOT_PASSWORD
              value: "{mysql_password}"
            - name: MYSQL_DATABASE
              value: "{db_name}"
          ports:
            - containerPort: 3306
          volumeMounts:
            - name: mysql-data
              mountPath: /var/lib/mysql
            - name: mysql-init-sql
              mountPath: /docker-entrypoint-initdb.d
      volumes:
        - name: mysql-data
          persistentVolumeClaim:
            claimName: {mysql_name}-pvc
        - name: mysql-init-sql
          configMap:
            name: {cm_name}
---
apiVersion: v1
kind: Service
metadata:
  name: {mysql_name}
  namespace: {namespace}
  labels:
    app.kubernetes.io/name: {mysql_name}
    app.kubernetes.io/part-of: {app_id}
spec:
  type: ClusterIP
  selector:
    app.kubernetes.io/name: {mysql_name}
  ports:
    - port: 3306
      targetPort: 3306
"""

    manifest_path = Path(BASE_DIR) / "k8s" / "generated" / f"{app_id}-mysql.yaml"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(mysql_manifest, encoding="utf-8")

    # 2) Apply MySQL manifest (namespace → ConfigMap → PVC → Deployment → Service)
    result = subprocess.run(
        ["kubectl", "apply", "--validate=false", "-f", str(manifest_path)],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        add_activity(application, "PIPELINE",
                     f"Deploy MySQL that bai: {result.stderr.strip()[:200]}", "Warning")
        return None
    add_activity(application, "PIPELINE",
                 f"Da deploy MySQL pod + ConfigMap init-sql: {mysql_name}", "Done")

    # 3) Wait for MySQL pod to be ready
    #    MySQL auto-imports SQL from /docker-entrypoint-initdb.d/ on first start
    if pipeline_run:
        _update_stage(pipeline_run, "DEPLOY", "Running",
                      "Waiting for MySQL pod to be ready (up to 120s, auto-importing SQL)...")
    wait_result = subprocess.run(
        ["kubectl", "wait", "pod", "-n", namespace,
         "-l", f"app.kubernetes.io/name={mysql_name}",
         "--for=condition=Ready", "--timeout=120s"],
        capture_output=True, text=True, timeout=130,
    )
    mysql_ready = wait_result.returncode == 0

    if not mysql_ready:
        add_activity(application, "PIPELINE",
                     "MySQL pod chua ready sau 120s — kiem tra thu cong", "Warning")
        return (mysql_name, db_name, mysql_password)

    add_activity(application, "PIPELINE",
                 f"MySQL pod da ready, SQL schema da duoc auto-import tu /docker-entrypoint-initdb.d/{sql_basename}",
                 "Done")

    return (mysql_name, db_name, mysql_password)


def _patch_db_php_in_pod(application: dict[str, Any], mysql_name: str, db_name: str, mysql_password: str) -> None:
    """Patch db.php inside running app container to point to correct MySQL host."""
    import subprocess

    namespace = application["namespace"]
    app_id = application["id"]
    db_content = f"""<?php
$host = '{mysql_name}';
$user = 'root';
$password = '{mysql_password}';
$database = '{db_name}';
$conn = new mysqli($host, $user, $password, $database);
if ($conn->connect_error) {{
    die('Connection failed: ' . $conn->connect_error);
}}
$conn->set_charset('utf8mb4');
?>"""

    all_ok = True
    for svc in application.get("services", []):
        deploy_name = f"{app_id}-{svc['name']}"
        # Get pod name for this deployment
        pod_result = subprocess.run(
            ["kubectl", "get", "pod", "-n", namespace, "-l", f"app.kubernetes.io/name={deploy_name}",
             "-o", "jsonpath={{.items[0].metadata.name}}"],
            capture_output=True, text=True, timeout=15,
        )
        pod_name = pod_result.stdout.strip()
        if not pod_name:
            add_activity(application, "PIPELINE", f"Khong tim thay pod cho {deploy_name} de patch db.php", "Warning")
            all_ok = False
            continue

        for php_path in ["/var/www/html/backend/db.php", "/var/www/html/frontend/db.php", "/var/www/html/db.php"]:
            import os
            parent_dir = os.path.dirname(php_path)
            patch_cmd = [
                "kubectl", "exec", "-n", namespace, pod_name, "--",
                "bash", "-c", f"mkdir -p {parent_dir} && cat > {php_path} << 'HEREDOC_EOF'\n{db_content}\nHEREDOC_EOF"
            ]
            patch_result = subprocess.run(patch_cmd, capture_output=True, text=True, timeout=15)
            if patch_result.returncode == 0:
                add_activity(application, "PIPELINE", f"Da patch {php_path} trong pod {pod_name}: host={mysql_name}", "Done")
                all_ok = True
                break
        else:
            add_activity(application, "PIPELINE", f"Khong tim thay duong dan db.php trong pod {pod_name}", "Warning")
            all_ok = False

    if all_ok:
        add_activity(application, "PIPELINE", f"Da patch tat ca db.php: host={mysql_name}, password={mysql_password}", "Done")

    # NOTE: mysqli installation is handled in the main DEPLOY flow (after rollout),
    # using apt-get fast path + skip-if-already-loaded check.
    # Do NOT install here to avoid duplicate 2-5min compile blocking DEPLOY stage.


def _patch_db_php_on_worker(application: dict[str, Any], mysql_name: str, db_name: str,
                             mysql_password: str, worker_ip: str, remote_path: str) -> None:
    """Patch db.php on worker node filesystem (for hostPath fallback)."""
    import subprocess
    import tempfile
    import os

    db_content = f"""<?php
$host = '{mysql_name}';
$user = 'root';
$password = '{mysql_password}';
$database = '{db_name}';
$conn = new mysqli($host, $user, $password, $database);
if ($conn->connect_error) {{
    die('Connection failed: ' . $conn->connect_error);
}}
$conn->set_charset('utf8mb4');
?>"""

    # Create temporary local file
    with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.php') as temp_file:
        temp_file.write(db_content)
        temp_file_path = temp_file.name

    try:
        # Copy to remote temp path
        dest_temp = "/home/khang/db_temp.php"
        scp_cmd = f"scp -o StrictHostKeyChecking=no {temp_file_path} khang@{worker_ip}:{dest_temp}"
        scp_result = subprocess.run(scp_cmd, shell=True, timeout=15, capture_output=True, text=True)
        if scp_result.returncode != 0:
            add_activity(application, "PIPELINE", f"Copy db.php temp len worker that bai: {scp_result.stderr.strip()[:150]}", "Warning")
            return

        all_ok = True
        for php_path in [f"{remote_path}/backend/db.php", f"{remote_path}/frontend/db.php"]:
            # SSH and copy remote temp to destination, then change ownership to www-data
            parent_dir = os.path.dirname(php_path)
            db_patch_cmd = (
                f"ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 khang@{worker_ip} "
                f"\"if [ -d '{parent_dir}' ]; then "
                f"sudo cp {dest_temp} {php_path} && "
                f"sudo chown www-data:www-data {php_path}; "
                f"fi\""
            )
            db_patch_result = subprocess.run(db_patch_cmd, shell=True, timeout=15, capture_output=True, text=True)
            if db_patch_result.returncode != 0:
                add_activity(application, "PIPELINE", f"Patch {php_path} that bai: {db_patch_result.stderr.strip()[:150]}", "Warning")
                all_ok = False

        # Cleanup remote temp
        ssh_cleanup = f"ssh -o StrictHostKeyChecking=no khang@{worker_ip} 'rm -f {dest_temp}'"
        subprocess.run(ssh_cleanup, shell=True, timeout=10)

        if all_ok:
            add_activity(application, "PIPELINE", f"Da patch db.php tren worker: host={mysql_name}, password={mysql_password}", "Done")

        # NOTE: mysqli installation is handled in the main deploy flow (rollout restart block)
        # to avoid a duplicate 300s install here that causes DEPLOY stage to appear hung.

    finally:
        # Cleanup local temp
        if os.path.exists(temp_file_path):
            os.remove(temp_file_path)


def _run_pipeline(pipeline_run: dict[str, Any]) -> None:
    """Execute pipeline stages sequentially in a background thread."""
    application = find_application(pipeline_run["application_id"])
    if not application:
        _update_stage(pipeline_run, "SOURCE", "Failed", "Application not found")
        return

    # === STAGE 1: SOURCE ======================================================
    _update_stage(pipeline_run, "SOURCE", "Running", "Checking source...")
    time.sleep(0.5)  # simulate work
    source_type = application.get("source_type", "docker")
    if source_type == "docker":
        image = application["services"][0]["image"]
        _update_stage(
            pipeline_run, "SOURCE", "Done",
            f"Docker image: {image}"
        )
        pipeline_run["image"] = image
    elif source_type == "github":
        github_url = application.get("github_url", "")
        _update_stage(
            pipeline_run, "SOURCE", "Done",
            f"GitHub repository: {github_url}"
        )
        # Phase 4 will handle actual build; for now build stage is skip-aware
    else:
        _update_stage(pipeline_run, "SOURCE", "Failed", f"Unknown source type: {source_type}")
        return

    add_activity(application, "PIPELINE", f"Pipeline {pipeline_run['id']}: SOURCE stage completed", "Done")

    # Flag for hostPath fallback deployment (set to True only inside fallback block)
    using_hostpath = False

    # === STAGES 2-4: BUILD/TEST/PUSH (docker vs github) =======================
    if source_type == "docker":
        # ---- Docker image flow: skip build/push, quick test only ----
        _update_stage(pipeline_run, "BUILD", "Skipped", "Docker image provided — skip build")
        add_activity(application, "PIPELINE", f"Pipeline {pipeline_run['id']}: BUILD stage completed", "Done")

        _update_stage(pipeline_run, "TEST", "Running", "Running container startup & health checks...")
        time.sleep(1.0)
        try:
            import subprocess
            image = pipeline_run.get("image", application["services"][0]["image"])
            result = subprocess.run(
                ["docker", "image", "inspect", image],
                capture_output=True, text=True, timeout=30,
            )
            if result.returncode == 0:
                _update_stage(pipeline_run, "TEST", "Done", "Docker image exists locally")
            else:
                _update_stage(pipeline_run, "TEST", "Done", "Image not cached locally — will pull from registry")
        except Exception:
            _update_stage(pipeline_run, "TEST", "Done", "Docker not available — skipping test")
        add_activity(application, "PIPELINE", f"Pipeline {pipeline_run['id']}: TEST stage completed", "Done")

        _update_stage(pipeline_run, "PUSH", "Skipped", "Docker image from registry — skip push")
        add_activity(application, "PIPELINE", f"Pipeline {pipeline_run['id']}: PUSH stage completed", "Done")
    else:
        # ---- GitHub source: full clone → build → test → push ----
        pipeline_run = build_from_github(application, pipeline_run)

        # Check if BUILD or PUSH needs fallback to public image + hostPath
        build_stage = next((s for s in pipeline_run["stages"] if s["name"] == "BUILD"), None)
        push_stage = next((s for s in pipeline_run["stages"] if s["name"] == "PUSH"), None)

        build_failed = build_stage and build_stage["status"] == "Failed"
        push_failed = push_stage and push_stage["status"] == "Failed"
        push_skipped = push_stage and push_stage["status"] == "Skipped"

        # Detect if BUILD failed due to Docker being unavailable (binary missing,
        # daemon not running, WSL 2 integration not enabled, etc.)
        build_msg = build_stage.get("message", "") if build_stage else ""
        is_docker_unavailable = (
            "Docker is not available" in build_msg
            or "command 'docker' could not be found" in build_msg.lower()
            or "Command not found: docker" in build_msg
            or "WSL 2 distro" in build_msg
            or "docker desktop" in build_msg.lower()
        )
        build_non_docker = build_failed and not is_docker_unavailable

        # Only hard-fail if BUILD failed for a non-Docker reason
        if build_non_docker:
            pipeline_run["status"] = "Failed"
            _save_pipeline_run(pipeline_run)
            save_application(application)
            return

        # Need fallback if: (a) BUILD/PUSH failed, or (b) PUSH skipped due to no valid credentials
        need_fallback = build_failed or push_failed or (push_skipped and "No valid registry credentials" in push_stage.get("message", ""))
        if need_fallback:
            if build_failed:
                reason = "Docker không khả dụng"
            elif push_skipped:
                reason = "Không có credentials registry hợp lệ — bỏ qua push"
            else:
                reason = "Push registry thất bại (lỗi đăng nhập hoặc registry không khả dụng)"
            add_activity(application, "PIPELINE", f"{reason} — chuyển sang triển khai bằng image public + hostPath", "Warning")
            if build_failed:
                _update_stage(pipeline_run, "BUILD", "Skipped", "Docker not available — using public image with hostPath")
                _update_stage(pipeline_run, "TEST", "Skipped", "Docker not available — skip test")
                _update_stage(pipeline_run, "PUSH", "Skipped", "Docker not available — skip push")
            elif push_failed:
                # PUSH failed but BUILD succeeded — mark as skipped with fallback
                _update_stage(pipeline_run, "PUSH", "Skipped", f"Push failed — using public image fallback: {push_stage.get('message', '')[:100]}")
            # If push_skipped (no valid credentials): keep the "Skipped" message already set by build_from_github

            # ---- Fallback: detect project type, set public image, sync code to worker ----
            _update_stage(pipeline_run, "DEPLOY", "Running", "Đang chuẩn bị triển khai với image public + hostPath...")

            # Determine public image based on project type
            # Prefer work_dir stored by build_from_github; fallback to glob
            work_dir_str = pipeline_run.get("_work_dir", "")
            if work_dir_str:
                source_dir = Path(work_dir_str)
            else:
                build_base = Path(BASE_DIR) / "app" / "data" / "builds"
                build_dirs = sorted(build_base.glob(f"{application['id']}-*"), key=lambda p: p.stat().st_mtime, reverse=True) if build_base.exists() else []
                source_dir = build_dirs[0] if build_dirs else None

            public_image = "nginx:stable-alpine"  # default
            container_mount = "/usr/share/nginx/html"

            if source_dir and source_dir.exists():
                has_php = bool(list(source_dir.glob("*.php")) + list(source_dir.rglob("*.php")))
                has_composer = (source_dir / "composer.json").exists()
                has_package_json = (source_dir / "package.json").exists()
                has_python = (source_dir / "requirements.txt").exists() or (source_dir / "pyproject.toml").exists()

                if has_php or has_composer:
                    public_image = "php:8.2-apache"
                    container_mount = "/var/www/html"
                elif has_package_json:
                    public_image = "node:18-alpine"
                    container_mount = "/app"
                elif has_python:
                    public_image = "python:3.11-slim"
                    container_mount = "/app"

            # Find worker node and sync source code
            worker_node = "khang-virtualbox"
            worker_ip = "192.168.56.12"
            try:
                import subprocess
                import json as _json
                # Get worker node name from kubectl
                result = subprocess.run(
                    ["kubectl", "get", "nodes", "-o", "json", "-l", "node-role.kubernetes.io/control-plane!=true"],
                    capture_output=True, text=True, timeout=15,
                )
                if result.returncode == 0:
                    nodes_data = _json.loads(result.stdout)
                    worker_items = nodes_data.get("items", [])
                    if worker_items:
                        worker_node = worker_items[0]["metadata"]["name"]
                        addrs = worker_items[0].get("status", {}).get("addresses", [])
                        for addr in addrs:
                            if addr.get("type") == "InternalIP":
                                worker_ip = addr["address"]
                                break
            except Exception:
                pass  # fallback to defaults

            # Sync source to worker (show errors so failures are visible)
            remote_path = f"/opt/{application['id']}"
            mkdir_cmd = f"ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 khang@{worker_ip} 'sudo mkdir -p {remote_path} && sudo chown -R khang:khang {remote_path}'"
            tar_cmd = f"tar czf - -C {source_dir} . 2>/dev/null | ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 khang@{worker_ip} 'tar xzf - -C {remote_path}/'"
            try:
                mkdir_result = subprocess.run(mkdir_cmd, shell=True, timeout=30, capture_output=True, text=True)
                if mkdir_result.returncode != 0:
                    add_activity(application, "PIPELINE", f"Không tạo được thư mục trên worker: {mkdir_result.stderr.strip()[:200]}", "Warning")
                elif source_dir and source_dir.exists():
                    tar_result = subprocess.run(tar_cmd, shell=True, timeout=60, capture_output=True, text=True)
                    if tar_result.returncode != 0:
                        add_activity(application, "PIPELINE", f"Không đồng bộ được source lên worker: {tar_result.stderr.strip()[:200]}", "Warning")
                    else:
                        add_activity(application, "PIPELINE", f"Đã đồng bộ source code lên worker {worker_node}:{remote_path}", "Done")

                        # Nếu source có frontend/index.php và không có index.html ở root,
                        # LUÔN ghi đè index.php ở root với chdir vào frontend/ để fix:
                        # - Lỗi 403 (Apache không tìm thấy index)
                        # - Lỗi đường dẫn tương đối (./css, ../backend, ...)
                        # - Lỗi PHP parse từ pipeline cũ (file index.php lỗi còn tồn tại)
                        index_fix_cmd = (
                            f"ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 khang@{worker_ip} "
                            f"\"if [ -f {remote_path}/frontend/index.php ] && [ ! -f {remote_path}/index.html ]; then "
                            f"rm -f {remote_path}/.htaccess && "
                            f"printf '<?php chdir(__DIR__ . \\\"/frontend\\\"); require \\\"index.php\\\";' > {remote_path}/index.php; "
                            f"fi\""
                        )
                        index_fix_result = subprocess.run(index_fix_cmd, shell=True, timeout=15, capture_output=True, text=True)
                        if index_fix_result.returncode == 0:
                            add_activity(application, "PIPELINE", "Da tao (hoac ghi de) index.php root -> frontend/ de fix duong dan", "Done")
                        else:
                            add_activity(application, "PIPELINE", f"Khong tao duoc index.php: {index_fix_result.stderr.strip()[:150]}", "Warning")
                        # Tạo symlink css/ js/ & các file .html ở root → frontend/ để browser load được static files
                        # Vì chdir chỉ fix PHP include, còn browser request /css/styles.css hay /login.html thì
                        # Apache serve trực tiếp từ filesystem tại DocumentRoot, cần symlink để trỏ vào frontend/
                        symlink_cmd = (
                            f"ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 khang@{worker_ip} "
                            f"\"cd {remote_path} && "
                            f"rm -rf css js 2>/dev/null; "
                            f"rm -f *.html 2>/dev/null; "
                            f"ln -sfn frontend/css css && "
                            f"ln -sfn frontend/js js && "
                            f"for f in frontend/*.html; do "
                            f"[ -f \\\"\\$f\\\" ] && ln -sfn \\\"\\$f\\\" \\\"\\$(basename \\\"\\$f\\\")\\\"; "
                            f"done\""
                        )
                        symlink_result = subprocess.run(symlink_cmd, shell=True, timeout=15, capture_output=True, text=True)
                        if symlink_result.returncode == 0:
                            add_activity(application, "PIPELINE", "Da tao symlink css/, js/ & *.html -> frontend/ de load static files", "Done")
                        else:
                            add_activity(application, "PIPELINE", f"Khong tao duoc symlink: {symlink_result.stderr.strip()[:150]}", "Warning")
                        # NOTE: MySQL deployment + db.php patching is now done in the
                        # post-deploy section (after delete_application_workloads + deploy_application)
                        # to avoid deploying MySQL twice (once here, deleted, then again later).

                        # Sửa permission cho Apache user (www-data) đọc được source
                        chown_cmd = f"ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 khang@{worker_ip} 'sudo chown -R www-data:www-data {remote_path}'"
                        chown_result = subprocess.run(chown_cmd, shell=True, timeout=15, capture_output=True, text=True)
                        if chown_result.returncode == 0:
                            add_activity(application, "PIPELINE", "Da sua permission source code cho Apache (www-data)", "Done")
                        else:
                            add_activity(application, "PIPELINE", f"Khong sua duoc permission: {chown_result.stderr.strip()[:150]}", "Warning")

            except Exception as e:
                add_activity(application, "PIPELINE", f"Cảnh báo: không đồng bộ được source lên worker: {e}", "Warning")

            # Update service config with public image + hostPath
            using_hostpath = True
            for svc in application["services"]:
                svc["image"] = public_image
                svc["host_path"] = remote_path
                svc["container_mount"] = container_mount
                svc["node_name"] = worker_node

            pipeline_run["image"] = public_image
            add_activity(application, "PIPELINE", f"Sử dụng image public {public_image} + hostPath từ {worker_node}:{remote_path}", "Done")

    # === STAGE 5: DEPLOY ======================================================
    # Xoá workloads cũ trước khi deploy mới để tránh conflict resource
    _update_stage(pipeline_run, "DEPLOY", "Running", "Deleting old workloads before redeploy...")
    delete_success, delete_output = delete_application_workloads(application)
    if delete_success:
        add_activity(application, "PIPELINE", f"Đã xoá workloads cũ: {delete_output[:150]}", "Done")
    else:
        add_activity(application, "PIPELINE", f"Xoá workloads cũ thất bại (có thể là lần đầu deploy): {delete_output[:150]}", "Warning")
    # K3s cần vài giây để xoá pods/PVC resources cũ (namespace đã được giữ lại nên không cần chờ lâu)
    time.sleep(5)

    _update_stage(pipeline_run, "DEPLOY", "Running", "Deploying to K3s cluster...")
    success, output = deploy_application(application)
    if success:
        # Force restart pods when using hostPath so they pick up freshly synced files.
        # kubectl apply may report "unchanged" because the manifest is identical,
        # but the hostPath volume content was just updated via rsync.
        if using_hostpath:
            try:
                from app.modules.deployments.kubectl import run_kubectl
                namespace = application["namespace"]
                for svc in application.get("services", []):
                    deploy_name = f"{application['id']}-{svc['name']}"
                    restart_ok, restart_out = run_kubectl(
                        ["rollout", "restart", f"deployment/{deploy_name}", "-n", namespace],
                        timeout=60,
                    )
                    add_activity(application, "PIPELINE",
                        f"Rollout restart {deploy_name}: {'OK' if restart_ok else 'Failed'} {restart_out[:150]}",
                        "Done" if restart_ok else "Warning")
                # Wait for rollout to complete — new pod must be Ready before installing mysqli
                for svc in application.get("services", []):
                    deploy_name = f"{application['id']}-{svc['name']}"
                    _update_stage(pipeline_run, "DEPLOY", "Running", f"Waiting for rollout {deploy_name}...")
                    wait_ok, wait_out = run_kubectl(
                        ["rollout", "status", f"deployment/{deploy_name}", "-n", namespace, "--timeout=90s"],
                        timeout=100,
                    )
                    add_activity(application, "PIPELINE",
                        f"Rollout status {deploy_name}: {'Ready' if wait_ok else 'Timeout'} {wait_out[:150]}",
                        "Done" if wait_ok else "Warning")
            except Exception as re:
                add_activity(application, "PIPELINE", f"Rollout restart error: {re}", "Warning")

            # Cài mysqli extension cho PHP container nếu dùng image php:8.2-apache
            # php:8.2-apache mặc định KHÔNG có mysqli — cần chạy docker-php-ext-install
            # Tối ưu: bỏ qua nếu mysqli đã được load sẵn (tránh mất 2-5 phút compile lại)
            if public_image and "php" in public_image:
                _update_stage(pipeline_run, "DEPLOY", "Running", "Checking/installing mysqli extension...")
                namespace = application["namespace"]
                for svc in application.get("services", []):
                    deploy_name = f"{application['id']}-{svc['name']}"
                    try:
                        # Get all RUNNING pod names for this deployment
                        # Filter Running only — terminating pods cause exit code 143 (SIGTERM)
                        pod_result = subprocess.run(
                            ["kubectl", "get", "pod", "-n", namespace, "-l", f"app.kubernetes.io/name={deploy_name}",
                             "-o", "json"],
                            capture_output=True, text=True, timeout=15,
                        )
                        if pod_result.returncode != 0 or not pod_result.stdout.strip():
                            continue
                        pods_data = json.loads(pod_result.stdout)
                        pod_names = [
                            item["metadata"]["name"] for item in pods_data.get("items", [])
                            if item.get("status", {}).get("phase") == "Running"
                        ]
                        for pod_name in pod_names:
                            # Check if mysqli already loaded — skip expensive compile if yes
                            check_result = subprocess.run(
                                ["kubectl", "exec", "-n", namespace, pod_name, "--",
                                 "php", "-r", "exit(extension_loaded('mysqli') ? 0 : 1);"],
                                capture_output=True, text=True, timeout=10,
                            )
                            if check_result.returncode == 0:
                                add_activity(application, "PIPELINE",
                                    f"mysqli already loaded in pod {pod_name} — skipping install", "Done")
                                # Just reload Apache to be safe
                                subprocess.run(
                                    ["kubectl", "exec", "-n", namespace, pod_name, "--",
                                     "bash", "-c",
                                     "apache2ctl -k graceful 2>/dev/null || service apache2 reload 2>/dev/null || true"],
                                    capture_output=True, text=True, timeout=15,
                                )
                                continue

                            # mysqli not loaded — try fast apt-get first, fallback to compile
                            _update_stage(pipeline_run, "DEPLOY", "Running",
                                f"Installing mysqli extension in {pod_name}...")
                            install_result = subprocess.run(
                                ["kubectl", "exec", "-n", namespace, pod_name, "--",
                                 "bash", "-c",
                                 # 1) Try apt-get (fast, ~15-30s on Debian/php:8.2-apache)
                                 "apt-get update -qq && apt-get install -y --no-install-recommends php-mysql 2>/dev/null "
                                 "|| docker-php-ext-install mysqli; "   # 2) fallback: compile from source
                                 "docker-php-ext-enable mysqli 2>/dev/null || true; "
                                 "apache2ctl -k graceful 2>/dev/null || service apache2 reload 2>/dev/null || true"],
                                capture_output=True, text=True, timeout=180,  # reduced: 3min max
                            )
                            if install_result.returncode == 0:
                                add_activity(application, "PIPELINE",
                                    f"Đã cài mysqli extension cho pod {pod_name}", "Done")
                            else:
                                add_activity(application, "PIPELINE",
                                    f"Cài mysqli thất bại: {install_result.stderr.strip()[:150]}", "Warning")
                    except Exception as ex:
                        add_activity(application, "PIPELINE", f"Lỗi khi cài mysqli: {ex}", "Warning")

        _update_stage(pipeline_run, "DEPLOY", "Running", "Setting up database if needed...")

        # === Post-DEPLOY: MySQL + db.php cho PHP projects (tất cả các path) ===
        # After app pod is running, detect if source has SQL → deploy MySQL + patch db.php
        # NOTE: delete_application_workloads() above wiped ALL resources (including any
        # previously-deployed MySQL), so we ALWAYS need to redeploy MySQL here.
        mysql_already_deployed = False
        work_dir_str = pipeline_run.get("_work_dir", "")
        if not work_dir_str:
            # Fallback: glob latest build dir (manual uploads don't set _work_dir)
            build_base = Path(BASE_DIR) / "app" / "data" / "builds"
            build_dirs = sorted(build_base.glob(f"{application['id']}-*"), key=lambda p: p.stat().st_mtime, reverse=True) if build_base.exists() else []
            if build_dirs:
                work_dir_str = str(build_dirs[0])
        if work_dir_str:
            source_dir = Path(work_dir_str)
            # Check if source has .sql files BEFORE asking _deploy_mysql_if_needed
            sql_files = list(source_dir.glob("*.sql")) + list(source_dir.rglob("*.sql"))
            if sql_files:
                mysql_name = f"{application['id']}-mysql"
                db_name = "map-project"
                mysql_password = "luanvan123"
                namespace = application["namespace"]

                if not mysql_already_deployed:
                    _update_stage(pipeline_run, "DEPLOY", "Running", "Deploying MySQL database...")
                    _deploy_mysql_if_needed(application, source_dir, pipeline_run)

                # Wait briefly for app pod to be ready before patching
                time.sleep(5)
                _update_stage(pipeline_run, "DEPLOY", "Running", "Patching db.php configuration...")
                if using_hostpath:
                    _patch_db_php_on_worker(application, mysql_name, db_name, mysql_password, worker_ip, remote_path)
                else:
                    _patch_db_php_in_pod(application, mysql_name, db_name, mysql_password)

                # Cài mysqli extension cho PHP container (image-based deploys only)
                # Chỉ chạy nếu chưa cài ở bước trước (hostPath flow đã cài ở trên)
                if not using_hostpath:
                    _update_stage(pipeline_run, "DEPLOY", "Running", "Checking/installing mysqli extension...")
                    try:
                        for svc in application.get("services", []):
                            deploy_name = f"{application['id']}-{svc['name']}"
                            # Wait for at least 1 pod to be Running (then install on ALL pods)
                            pod_names = []
                            for _ in range(10):
                                pod_result = subprocess.run(
                                    ["kubectl", "get", "pod", "-n", namespace, "-l", f"app.kubernetes.io/name={deploy_name}",
                                     "-o", "json"],
                                    capture_output=True, text=True, timeout=15
                                )
                                if pod_result.returncode == 0 and pod_result.stdout.strip():
                                    pods_data = json.loads(pod_result.stdout)
                                    items = pods_data.get("items", [])
                                    all_running = all(
                                        item.get("status", {}).get("phase") == "Running"
                                        for item in items
                                    ) if items else False
                                    all_names = [item["metadata"]["name"] for item in items]
                                    if all_running and all_names:
                                        pod_names = all_names
                                        break
                                time.sleep(5)

                            for pod_name in pod_names:
                                # Check if mysqli already loaded — skip expensive compile if yes
                                check_result = subprocess.run(
                                    ["kubectl", "exec", "-n", namespace, pod_name, "--",
                                     "php", "-r", "exit(extension_loaded('mysqli') ? 0 : 1);"],
                                    capture_output=True, text=True, timeout=10,
                                )
                                if check_result.returncode == 0:
                                    add_activity(application, "PIPELINE",
                                        f"mysqli already loaded in pod {pod_name} — skipping install", "Done")
                                    continue

                                _update_stage(pipeline_run, "DEPLOY", "Running",
                                    f"Installing mysqli in {pod_name}...")
                                install_result = subprocess.run(
                                    ["kubectl", "exec", "-n", namespace, pod_name, "--",
                                     "bash", "-c",
                                     # Try apt-get first (fast), fallback to compile
                                     "apt-get update -qq && apt-get install -y --no-install-recommends php-mysql 2>/dev/null "
                                     "|| docker-php-ext-install mysqli; "
                                     "docker-php-ext-enable mysqli 2>/dev/null || true; "
                                     "apache2ctl -k graceful 2>/dev/null || service apache2 reload 2>/dev/null || true"],
                                    capture_output=True, text=True, timeout=180,
                                )
                                if install_result.returncode == 0:
                                    add_activity(application, "PIPELINE", f"Đã cài và kích hoạt mysqli extension thành công trên pod {pod_name}", "Done")
                                else:
                                    add_activity(application, "PIPELINE", f"Cài mysqli thất bại trên pod {pod_name}: {install_result.stderr.strip()[:150]}", "Warning")
                    except Exception as ex:
                        add_activity(application, "PIPELINE", f"Lỗi trong quá trình cài mysqli: {ex}", "Warning")

        _update_stage(pipeline_run, "DEPLOY", "Done", output[:200])
        add_activity(application, "PIPELINE", "Deploy thành công lên K3s cluster", "Done")
    else:
        _update_stage(pipeline_run, "DEPLOY", "Failed", output[:200])
        add_activity(application, "PIPELINE", f"Deploy thất bại: {output[:200]}", "Failed")
        pipeline_run["status"] = "Failed"
        _save_pipeline_run(pipeline_run)
        save_application(application)
        return

    # === STAGE 6: VERIFY ======================================================
    _update_stage(pipeline_run, "VERIFY", "Running", "Quick pod health check...")
    namespace = application["namespace"]
    app_id = application["id"]

    # Lightweight: single kubectl get pods -o json, parse phases in Python
    # DEPLOY stage already waited for rollout, so pods should be ready immediately
    pods_running = 0
    total_pods = 0
    verify_message = ""
    import json as _json
    for attempt in range(10):
        try:
            result = subprocess.run(
                ["kubectl", "get", "pods", "-n", namespace,
                 "-l", "app.kubernetes.io/part-of=" + app_id,
                 "-o", "json"],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode == 0 and result.stdout.strip():
                data = _json.loads(result.stdout)
                items = data.get("items", [])
                total_pods = len(items)
                pods_running = sum(
                    1 for item in items
                    if item.get("status", {}).get("phase") == "Running"
                )
                if pods_running > 0:
                    verify_message = f"{pods_running}/{total_pods} pod(s) running"
                    break
                verify_message = f"{pods_running}/{total_pods} pod(s) running (waiting...)"
            else:
                verify_message = f"kubectl returned error (attempt {attempt + 1}/10)"
        except Exception:
            verify_message = f"kubectl query failed (attempt {attempt + 1}/10)"
        if attempt < 9:
            _update_stage(pipeline_run, "VERIFY", "Running", verify_message)
            time.sleep(3.0)

    if pods_running > 0:
        _update_stage(pipeline_run, "VERIFY", "Done", verify_message)
        add_activity(application, "PIPELINE", f"VERIFY: {verify_message}", "Done")
        pipeline_run["status"] = "Success"
    else:
        _update_stage(pipeline_run, "VERIFY", "Failed",
            f"0 running pods after deploy; last status: {verify_message}")
        add_activity(application, "PIPELINE",
            f"VERIFY: No running pods found — check cluster", "Failed")
        pipeline_run["status"] = "Failed"

    _save_pipeline_run(pipeline_run)
    save_application(application)


def trigger_pipeline(application_id: str) -> dict[str, Any]:
    """Create a pipeline run and start execution in background thread."""
    application = find_application(application_id)
    if not application:
        raise ValueError(f"Application {application_id} not found")

    run_id = f"run-{application_id}-{datetime.now().strftime('%Y%m%d%H%M%S')}"
    now = _now()

    stages = []
    for stage_name in PIPELINE_STAGES:
        stages.append({
            "name": stage_name,
            "status": "Waiting",
            "message": "",
            "started_at": "",
            "finished_at": "",
        })

    pipeline_run: dict[str, Any] = {
        "id": run_id,
        "application_id": application_id,
        "application_name": application.get("name", application_id),
        "status": "Running",
        "stages": stages,
        "image": "",
        "created_at": now,
        "updated_at": now,
    }

    _save_pipeline_run(pipeline_run)
    add_activity(application, "PIPELINE", f"Pipeline {run_id} started (6 stages)", "Running")

    # Run pipeline in background thread to avoid blocking HTTP request
    thread = threading.Thread(target=_run_pipeline, args=(pipeline_run,), daemon=True)
    thread.start()

    return pipeline_run


def get_latest_pipeline_run(application_id: str) -> dict[str, Any] | None:
    runs = load_pipeline_runs(application_id)
    return runs[0] if runs else None


def load_all_pipeline_events() -> list[dict[str, Any]]:
    """Return flat list of pipeline events for CI/CD overview page."""
    runs = load_pipeline_runs()
    events: list[dict[str, Any]] = []
    for run in runs:
        for stage in run["stages"]:
            if stage["status"] in ("Done", "Failed", "Running"):
                events.append({
                    "time": stage.get("finished_at", "") or stage.get("started_at", "") or run["created_at"],
                    "actor": run["application_name"],
                    "event": f"{stage['name']} stage",
                    "status": stage["status"],
                })
    return sorted(events, key=lambda e: e["time"], reverse=True)