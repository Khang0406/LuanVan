import json
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from app.config import BASE_DIR
from app.modules.applications.service import add_activity, find_application, save_application
from app.modules.deployments.kubectl import (
    deploy_application,
    delete_application_workloads,
    verify_application,
)
from app.modules.deployments.manifest import build_manifest
from app.modules.deployments.service import (
    begin_pipeline_deployment,
    finish_pipeline_deployment,
)
from app.delivery_store import (
    list_pipeline_runs as list_pipeline_runs_from_db,
    migrate_default_json_state,
    reserve_pipeline_run,
    upsert_pipeline_run,
)
from app.json_store import is_list_of_dicts, mask_secrets, normalize_status, read_json, update_json, write_json
from app.modules.monitoring.k8s_manifests import deploy_servicemonitor
from app.modules.pipeline.build import build_from_github, test_docker_image

DATA_DIR = BASE_DIR / "app" / "data"
PIPELINE_FILE = DATA_DIR / "pipeline_runs.json"
DEFAULT_PIPELINE_FILE = PIPELINE_FILE

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
    if PIPELINE_FILE == DEFAULT_PIPELINE_FILE:
        migrate_default_json_state()
        return list_pipeline_runs_from_db(application_id)
    runs = read_json(PIPELINE_FILE, [], is_list_of_dicts)

    if application_id:
        runs = [r for r in runs if r["application_id"] == application_id]
    return sorted(runs, key=lambda r: r["created_at"], reverse=True)


def save_pipeline_runs(runs: list[dict[str, Any]]) -> None:
    if not is_list_of_dicts(runs):
        raise ValueError("pipeline runs must be a list of objects")
    safe_runs = mask_secrets(runs)
    if PIPELINE_FILE == DEFAULT_PIPELINE_FILE:
        migrate_default_json_state()
        for run in safe_runs:
            upsert_pipeline_run(run)
        return
    write_json(PIPELINE_FILE, safe_runs)


def _save_pipeline_run(run: dict[str, Any]) -> None:
    safe_run = mask_secrets(run)

    if PIPELINE_FILE == DEFAULT_PIPELINE_FILE:
        migrate_default_json_state()
        upsert_pipeline_run(safe_run)
        return

    def upsert(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for index, existing in enumerate(runs):
            if existing.get("id") == safe_run.get("id"):
                runs[index] = safe_run
                break
        else:
            runs.append(safe_run)
        return runs

    update_json(PIPELINE_FILE, [], upsert, is_list_of_dicts)


def _update_stage(run: dict[str, Any], stage_name: str, status: str, message: str = "") -> dict[str, Any]:
    now = _now()
    for stage in run["stages"]:
        if stage["name"] == stage_name:
            stage["status"] = status
            stage["message"] = mask_secrets(message)
            if status in ("Running",) and not stage["started_at"]:
                stage["started_at"] = now
            if status in ("Done", "Failed", "Skipped"):
                stage["finished_at"] = now
            break
    run["updated_at"] = now
    _save_pipeline_run(run)
    if status in {"Done", "Failed", "Skipped"}:
        try:
            from app.modules.audit.service import record_audit

            record_audit(
                f"PIPELINE_{stage_name}",
                run.get("application_id", ""),
                "SUCCESS" if status in {"Done", "Skipped"} else "FAILED",
                message,
                user=run.get("actor", "system"),
                metadata={"pipeline_run_id": run.get("id"), "stage_status": status},
            )
        except Exception:
            pass
    return run


def _finish_pipeline_job(pipeline_run: dict[str, Any]) -> None:
    job_id = pipeline_run.get("job_id")
    if not job_id:
        return
    try:
        from app.modules.jobs.service import finish_job

        output = "\n\n".join(
            f"[{stage.get('status')}] {stage.get('name')}\n{stage.get('message', '')}"
            for stage in pipeline_run.get("stages", [])
        )
        technical_sections = []
        if pipeline_run.get("source_output"):
            technical_sections.append(f"SOURCE OUTPUT\n{pipeline_run['source_output']}")
        for name, value in pipeline_run.get("build_outputs", {}).items():
            technical_sections.append(f"BUILD OUTPUT [{name}]\n{value}")
        for name, value in pipeline_run.get("push_outputs", {}).items():
            technical_sections.append(f"PUSH OUTPUT [{name}]\n{value}")
        if technical_sections:
            output = f"{output}\n\n" + "\n\n".join(technical_sections)
        finish_job(
            job_id,
            pipeline_run.get("status") == "Success",
            output,
            pipeline_run.get("status", ""),
            pipeline_run.get("stages", []),
        )
    except Exception:
        pass


def _stop_failed_pipeline(
    pipeline_run: dict[str, Any], failed_stage: str, application: dict[str, Any]
) -> None:
    now = _now()
    for stage in pipeline_run.get("stages", []):
        if stage.get("status") == "Waiting":
            stage["status"] = "Skipped"
            stage["message"] = f"Skipped because {failed_stage} failed."
            stage["finished_at"] = now
    pipeline_run["status"] = "Failed"
    pipeline_run["finished_at"] = now
    pipeline_run["updated_at"] = now
    _save_pipeline_run(pipeline_run)
    add_activity(
        application, "PIPELINE",
        f"Pipeline {pipeline_run['id']} stopped because {failed_stage} failed.", "Failed",
    )
    save_application(application)
    _finish_pipeline_job(pipeline_run)


def recover_interrupted_pipeline_runs() -> int:
    """Close runs left active by a previous process without re-deploying them."""
    now = _now()
    recovered = 0

    if PIPELINE_FILE == DEFAULT_PIPELINE_FILE:
        runs = load_pipeline_runs()
        for run in runs:
            if normalize_status(run.get("status"), "Running") != "Running":
                continue
            recovered += 1
            run["status"] = "Interrupted"
            run["updated_at"] = now
            for stage in run.get("stages", []):
                status = normalize_status(stage.get("status"))
                if status == "Running":
                    stage["status"] = "Interrupted"
                    stage["message"] = "Pipeline interrupted by platform restart."
                    stage["finished_at"] = now
                elif status == "Waiting":
                    stage["status"] = "Skipped"
                    stage["message"] = "Skipped because the previous pipeline was interrupted."
                    stage["finished_at"] = now
            run["finished_at"] = now
            _save_pipeline_run(run)
        return recovered

    def recover(runs: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
        nonlocal recovered
        for run in runs:
            if normalize_status(run.get("status"), "Running") != "Running":
                continue
            recovered += 1
            run["status"] = "Interrupted"
            run["updated_at"] = now
            for stage in run.get("stages", []):
                status = normalize_status(stage.get("status"))
                if status == "Running":
                    stage["status"] = "Interrupted"
                    stage["message"] = "Pipeline interrupted by platform restart."
                    stage["finished_at"] = now
                elif status == "Waiting":
                    stage["status"] = "Skipped"
                    stage["message"] = "Skipped because the previous pipeline was interrupted."
                    stage["finished_at"] = now
        return runs if recovered else None

    update_json(PIPELINE_FILE, [], recover, is_list_of_dicts)
    return recovered

def _deploy_databases(
    application: dict[str, Any],
    source_dir: Path | None = None,
    pipeline_run: dict[str, Any] | None = None,
) -> None:
    """Deploy managed databases and wait for readiness.

    Uses the generic databases[] array from the application model.
    Each database can specify: name, image, port, password_key, env, storage_size,
    storage_class, kind (Deployment/StatefulSet), replicas, init_sql, init_job.
    The manifest is generated by build_manifest() which handles all resource creation.
    """
    import subprocess

    databases = application.get("databases", [])

    if not databases and source_dir and source_dir.exists():
        sql_files = list(source_dir.glob("*.sql")) + list(source_dir.rglob("*.sql"))
        if sql_files and application.get("source_type") == "github":
            sql_file = sql_files[0]
            sql_content = sql_file.read_text(encoding="utf-8")
            db_name = sql_file.stem or "app"
            databases = [{
                "name": "mysql",
                "image": "mysql:8.0",
                "port": 3306,
                "kind": "StatefulSet",
                "replicas": 1,
                "storage_size": "1Gi",
                "env": [
                    {"name": "MYSQL_ROOT_PASSWORD", "value": "luanvan123"},
                    {"name": "MYSQL_DATABASE", "value": db_name},
                ],
                "init_sql": {"name": sql_file.name, "content": sql_content},
            }]
            application["databases"] = databases

    if not databases:
        return

    for db in databases:
        db_name = db.get("name", "mysql")
        db_app_name = f"{application['id']}-{db_name}"
        namespace = application["namespace"]

        check_result = subprocess.run(
            ["kubectl", "get", "pods", "-n", namespace,
             "-l", f"app.kubernetes.io/name={db_app_name}",
             "-o", "json"],
            capture_output=True, text=True, timeout=10,
        )
        already_running = False
        if check_result.returncode == 0 and check_result.stdout.strip():
            try:
                import json
                pods = json.loads(check_result.stdout).get("items", [])
                already_running = all(
                    p.get("status", {}).get("phase") == "Running"
                    and all(c.get("ready") for c in p.get("status", {}).get("containerStatuses", []))
                    for p in pods
                )
            except Exception:
                pass

        if already_running:
            add_activity(application, "PIPELINE",
                         f"Database {db_name} already running — skipping deploy", "Done")
            continue

        if pipeline_run:
            _update_stage(pipeline_run, "DEPLOY", "Running",
                          f"Waiting for database {db_name} to be ready (up to 120s)...")

        wait_result = subprocess.run(
            ["kubectl", "wait", "pod", "-n", namespace,
             "-l", f"app.kubernetes.io/name={db_app_name}",
             "--for=condition=Ready", "--timeout=120s"],
            capture_output=True, text=True, timeout=130,
        )
        if wait_result.returncode != 0:
            add_activity(application, "PIPELINE",
                         f"Database {db_name} not ready after 120s — check manually", "Warning")
        else:
            add_activity(application, "PIPELINE",
                         f"Database {db_name} is Ready", "Done")


def _run_pipeline(pipeline_run: dict[str, Any]) -> None:
    """Execute pipeline stages sequentially in the standalone worker."""
    application = find_application(pipeline_run["application_id"])
    if not application:
        _update_stage(pipeline_run, "SOURCE", "Failed", "Application not found")
        pipeline_run["status"] = "Failed"
        pipeline_run["finished_at"] = _now()
        _save_pipeline_run(pipeline_run)
        _finish_pipeline_job(pipeline_run)
        return
    pipeline_run["status"] = "Running"
    _save_pipeline_run(pipeline_run)

    # === STAGE 1: SOURCE ======================================================
    source_type = application.get("source_type", "docker")
    if source_type == "docker":
        _update_stage(pipeline_run, "SOURCE", "Running", "Validating Docker image source...")
        image = application["services"][0]["image"]
        if not image or image.endswith(":latest"):
            _update_stage(
                pipeline_run, "SOURCE", "Failed",
                "Docker image is missing or uses mutable :latest tag.",
            )
            _stop_failed_pipeline(pipeline_run, "SOURCE", application)
            return
        _update_stage(
            pipeline_run, "SOURCE", "Done",
            f"Docker image: {image}"
        )
        pipeline_run["image"] = image
    elif source_type == "github":
        # SOURCE is executed by build_from_github and is only marked Done after
        # clone, ref checkout and commit SHA extraction have completed.
        pass
    else:
        _update_stage(pipeline_run, "SOURCE", "Failed", f"Unknown source type: {source_type}")
        _stop_failed_pipeline(pipeline_run, "SOURCE", application)
        return

    if source_type == "docker":
        add_activity(application, "PIPELINE", f"Pipeline {pipeline_run['id']}: SOURCE stage completed", "Done")

    # Flag for hostPath fallback deployment (deprecated — kept for type stability)
    using_hostpath = False

    # === STAGES 2-4: BUILD/TEST/PUSH (docker vs github) =======================
    if source_type == "docker":
        # ---- Docker image flow: skip build/push, run real container tests ----
        _update_stage(pipeline_run, "BUILD", "Skipped", "Docker image provided — skip build")
        add_activity(application, "PIPELINE", f"Pipeline {pipeline_run['id']}: BUILD stage completed", "Done")

        _update_stage(pipeline_run, "TEST", "Running", "Running container startup & health checks...")
        test_results = []
        tests_ok = True
        for service in application.get("services", []):
            if not service.get("required", True):
                continue
            test_ok, test_output = test_docker_image(
                application, service, service.get("image", "")
            )
            tests_ok = tests_ok and test_ok
            test_results.append(f"{service['name']}: {test_output}")
        if not tests_ok or not test_results:
            _update_stage(pipeline_run, "TEST", "Failed", "; ".join(test_results) or "No required service to test.")
            _stop_failed_pipeline(pipeline_run, "TEST", application)
            return
        _update_stage(pipeline_run, "TEST", "Done", "; ".join(test_results))
        add_activity(application, "PIPELINE", f"Pipeline {pipeline_run['id']}: TEST stage completed", "Done")

        _update_stage(pipeline_run, "PUSH", "Skipped", "Docker image from registry — skip push")
        add_activity(application, "PIPELINE", f"Pipeline {pipeline_run['id']}: PUSH stage completed", "Done")
    else:
        # ---- GitHub source: full clone → build → test → push ----
        pipeline_run = build_from_github(application, pipeline_run)

        # Stop immediately when a required source/build/test stage fails.
        source_stage = next((s for s in pipeline_run["stages"] if s["name"] == "SOURCE"), None)
        build_stage = next((s for s in pipeline_run["stages"] if s["name"] == "BUILD"), None)
        test_stage = next((s for s in pipeline_run["stages"] if s["name"] == "TEST"), None)
        push_stage = next((s for s in pipeline_run["stages"] if s["name"] == "PUSH"), None)

        if source_stage and source_stage["status"] == "Failed":
            _stop_failed_pipeline(pipeline_run, "SOURCE", application)
            return
        build_failed = build_stage and build_stage["status"] == "Failed"
        test_failed = test_stage and test_stage["status"] == "Failed"
        push_failed = push_stage and push_stage["status"] == "Failed"
        push_skipped = push_stage and push_stage["status"] == "Skipped"
        fallback_allowed = (
            application.get("deployment_mode") == "development"
            and application.get("development_fallback_enabled") is True
        )

        if test_failed:
            _stop_failed_pipeline(pipeline_run, "TEST", application)
            return

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
        if build_failed and (not is_docker_unavailable or not fallback_allowed):
            _stop_failed_pipeline(pipeline_run, "BUILD", application)
            return
        if push_failed and not fallback_allowed:
            _stop_failed_pipeline(pipeline_run, "PUSH", application)
            return

        # Need fallback if: (a) BUILD/PUSH failed, or (b) PUSH skipped due to no valid credentials
        need_fallback = fallback_allowed and (
            build_failed or push_failed or
            (push_skipped and "Development fallback" in push_stage.get("message", ""))
        )
        if need_fallback:
            if build_failed:
                reason = "Docker không khả dụng"
            elif push_skipped:
                reason = "Không có credentials registry hợp lệ — bỏ qua push"
            else:
                reason = "Push registry thất bại (lỗi đăng nhập hoặc registry không khả dụng)"
            add_activity(application, "PIPELINE", f"{reason} — chuyển sang triển khai bằng image public + hostPath", "Warning")
            if build_failed:
                _update_stage(pipeline_run, "TEST", "Skipped", "Docker not available — skip test")
                _update_stage(pipeline_run, "PUSH", "Skipped", "Docker not available — skip push")
            # If push_skipped (no valid credentials): keep the "Skipped" message already set by build_from_github

            # ---- Fallback: detect project type, set public image for services ----
            _update_stage(pipeline_run, "DEPLOY", "Running", "Using public image fallback for development mode...")

            work_dir_str = pipeline_run.get("_work_dir", "")
            if work_dir_str:
                source_dir = Path(work_dir_str)
            else:
                build_base = Path(BASE_DIR) / "app" / "data" / "builds"
                build_dirs = sorted(build_base.glob(f"{application['id']}-*"), key=lambda p: p.stat().st_mtime, reverse=True) if build_base.exists() else []
                source_dir = build_dirs[0] if build_dirs else None

            public_image = "nginx:stable-alpine"

            if source_dir and source_dir.exists():
                has_php = bool(list(source_dir.glob("*.php")) + list(source_dir.rglob("*.php")))
                has_composer = (source_dir / "composer.json").exists()
                has_package_json = (source_dir / "package.json").exists()
                has_python = (source_dir / "requirements.txt").exists() or (source_dir / "pyproject.toml").exists()

                if has_php or has_composer:
                    public_image = "php:8.2-apache"
                elif has_package_json:
                    public_image = "node:18-alpine"
                elif has_python:
                    public_image = "python:3.11-slim"

            for svc in application["services"]:
                svc["image"] = public_image
            pipeline_run["image"] = public_image
            add_activity(application, "PIPELINE", f"Sử dụng image public {public_image} (development fallback)", "Done")

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
        _update_stage(pipeline_run, "DEPLOY", "Running", "Setting up databases if configured...")
        try:
            _deploy_databases(application, source_dir=None, pipeline_run=pipeline_run)
        except Exception as db_exc:
            add_activity(application, "PIPELINE", f"Database setup warning: {db_exc}", "Warning")

        try:
            deployment_record = begin_pipeline_deployment(
                application, pipeline_run, build_manifest(application)
            )
        except Exception as exc:
            _update_stage(
                pipeline_run, "DEPLOY", "Failed",
                f"Kubernetes apply succeeded but deployment record creation failed: {exc}",
            )
            _stop_failed_pipeline(pipeline_run, "DEPLOY", application)
            return

        _update_stage(
            pipeline_run, "DEPLOY", "Done",
            f"deployment=v{deployment_record['version']} id={deployment_record['id']}\n{output}",
        )
        add_activity(application, "PIPELINE", "Deploy thành công lên K3s cluster", "Done")

        # Auto-create ServiceMonitor for Prometheus scraping
        try:
            sm_result = deploy_servicemonitor(application)
            if sm_result:
                add_activity(application, "PIPELINE", f"ServiceMonitor created: {sm_result}", "Done")
            else:
                add_activity(application, "PIPELINE", "ServiceMonitor skipped (no services or already exists)", "Done")
        except Exception as sm_exc:
            add_activity(application, "PIPELINE", f"ServiceMonitor creation failed: {sm_exc}", "Warning")
    else:
        _update_stage(pipeline_run, "DEPLOY", "Failed", output[:200])
        add_activity(application, "PIPELINE", f"Deploy thất bại: {output[:200]}", "Failed")
        _stop_failed_pipeline(pipeline_run, "DEPLOY", application)
        return

    # === STAGE 6: VERIFY ======================================================
    verify_timeout = int(application.get("verify_timeout_seconds", 180))
    _update_stage(
        pipeline_run, "VERIFY", "Running",
        f"Verifying rollout, replicas, pod/container readiness, Services and health (timeout={verify_timeout}s)...",
    )
    verify_ok, verify_message, verify_details = verify_application(
        application, timeout_seconds=verify_timeout
    )
    finish_pipeline_deployment(
        application, deployment_record, success=verify_ok,
        verify_message=verify_message, verify_details=verify_details,
    )

    if verify_ok:
        _update_stage(pipeline_run, "VERIFY", "Done", verify_message)
        add_activity(application, "PIPELINE", f"VERIFY: {verify_message}", "Done")
        pipeline_run["status"] = (
            "DevelopmentFallback"
            if pipeline_run.get("deployment_mode") == "Development fallback"
            else "Success"
        )
    else:
        _update_stage(pipeline_run, "VERIFY", "Failed", verify_message)
        add_activity(application, "PIPELINE",
            f"VERIFY failed: {verify_message}", "Failed")
        pipeline_run["status"] = "Failed"

    pipeline_run["finished_at"] = _now()
    _save_pipeline_run(pipeline_run)
    save_application(application)
    _finish_pipeline_job(pipeline_run)


def _run_pipeline_safely(pipeline_run: dict[str, Any]) -> None:
    """Ensure unexpected worker exceptions leave a retryable terminal run."""
    try:
        _run_pipeline(pipeline_run)
    except Exception as exc:
        application = find_application(pipeline_run.get("application_id", ""))
        active_stage = next(
            (
                stage.get("name", "PIPELINE")
                for stage in pipeline_run.get("stages", [])
                if stage.get("status") == "Running"
            ),
            "PIPELINE",
        )
        safe_message = mask_secrets(f"Unexpected pipeline error: {exc}")
        if active_stage != "PIPELINE":
            _update_stage(pipeline_run, active_stage, "Failed", safe_message)
        if application:
            _stop_failed_pipeline(pipeline_run, active_stage, application)
        else:
            pipeline_run["status"] = "Failed"
            pipeline_run["finished_at"] = _now()
            pipeline_run["error"] = safe_message
            _save_pipeline_run(pipeline_run)
            _finish_pipeline_job(pipeline_run)


def trigger_pipeline(
    application_id: str,
    actor: Any | None = None,
    *,
    trigger_type: str = "Manual",
    requested_ref: str = "",
    commit_author: str = "",
    commit_message: str = "",
) -> dict[str, Any]:
    """Persist a durable queued run for the standalone worker."""
    application = find_application(application_id)
    if not application:
        raise ValueError(f"Application {application_id} not found")

    run_id = f"run-{application_id}-{uuid.uuid4()}"
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
        "status": "Queued",
        "stages": stages,
        "image": "",
        "created_at": now,
        "updated_at": now,
        "trigger_type": trigger_type,
        "requested_ref": requested_ref or application.get("requested_ref", ""),
        "commit_author": commit_author,
        "commit_message": commit_message,
    }
    if actor and getattr(actor, "is_authenticated", False):
        pipeline_run["actor"] = getattr(actor, "username", "unknown")
        pipeline_run["actor_id"] = getattr(actor, "id", None)
        pipeline_run["actor_role"] = getattr(actor, "role", "")

    reserve_pipeline_run(pipeline_run)
    from app.modules.jobs.service import create_job

    job = create_job(
        "Pipeline", f"CI/CD Pipeline - {application.get('name', application_id)}",
        application_id, command=f"pipeline {run_id}", actor=actor,
        metadata={"application_id": application_id, "pipeline_run_id": run_id},
    )
    pipeline_run["job_id"] = job["id"]
    # Attach the Job ID to the reserved database record.
    _save_pipeline_run(pipeline_run)
    add_activity(application, "PIPELINE", f"Pipeline {run_id} queued (6 stages)", "Pending")
    save_application(application)

    return pipeline_run


def get_latest_pipeline_run(application_id: str) -> dict[str, Any] | None:
    runs = load_pipeline_runs(application_id)
    return runs[0] if runs else None


def retry_pipeline(
    pipeline_run_id: str, actor: Any | None = None
) -> dict[str, Any]:
    original = next(
        (run for run in load_pipeline_runs() if run.get("id") == pipeline_run_id), None
    )
    if not original:
        raise ValueError("Pipeline run không tồn tại.")
    if original.get("status") not in {"Failed", "Interrupted"}:
        raise ValueError("Chỉ pipeline Failed hoặc Interrupted mới có thể retry.")
    return trigger_pipeline(
        original["application_id"],
        actor=actor,
        trigger_type="Retry",
        requested_ref=original.get("commit_sha") or original.get("requested_ref", ""),
        commit_author=original.get("commit_author", ""),
        commit_message=original.get("commit_message", ""),
    )


def load_all_pipeline_events(application_ids: set[str] | None = None) -> list[dict[str, Any]]:
    """Return flat events, including pipeline records created before Phase 4."""
    runs = load_pipeline_runs()
    events: list[dict[str, Any]] = []
    for run in runs:
        application_id = str(run.get("application_id") or "unknown-application")
        if application_ids is not None and application_id not in application_ids:
            continue
        application_name = str(run.get("application_name") or application_id)
        created_at = str(run.get("created_at") or "")
        stages = run.get("stages")
        if not isinstance(stages, list):
            continue
        for stage in stages:
            if not isinstance(stage, dict):
                continue
            status = stage.get("status", "")
            if status in ("Done", "Failed", "Running"):
                events.append({
                    "time": (
                        stage.get("finished_at", "")
                        or stage.get("started_at", "")
                        or created_at
                    ),
                    "actor": application_name,
                    "event": f"{stage.get('name', 'PIPELINE')} stage",
                    "status": status,
                })
    return sorted(events, key=lambda e: e["time"], reverse=True)
