import json
from datetime import datetime
from typing import Any

from flask import has_request_context
from flask_login import current_user

from app.config import BASE_DIR

DATA_DIR = BASE_DIR / "app" / "data"
JOBS_FILE = DATA_DIR / "jobs.json"


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _ensure_jobs_file() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not JOBS_FILE.exists():
        JOBS_FILE.write_text("[]", encoding="utf-8")


def load_jobs(limit: int | None = 200) -> list[dict[str, Any]]:
    _ensure_jobs_file()
    with JOBS_FILE.open("r", encoding="utf-8") as file:
        jobs = json.load(file)
    jobs = sorted(jobs, key=lambda item: item.get("created_at", ""), reverse=True)
    return jobs[:limit] if limit else jobs


def save_jobs(jobs: list[dict[str, Any]]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with JOBS_FILE.open("w", encoding="utf-8") as file:
        json.dump(jobs, file, ensure_ascii=False, indent=2)


def command_to_text(command: list[str] | tuple[str, ...] | str | None) -> str:
    if not command:
        return ""
    if isinstance(command, str):
        return command
    return " ".join(str(part) for part in command)


def _actor_payload(actor: Any | None = None) -> dict[str, Any]:
    actor = actor or (current_user if has_request_context() else None)
    if getattr(actor, "is_authenticated", False):
        return {
            "actor": getattr(actor, "username", "unknown"),
            "actor_id": getattr(actor, "id", None),
            "actor_role": getattr(actor, "role", ""),
        }
    return {"actor": "system", "actor_id": None, "actor_role": ""}


def create_job(
    job_type: str,
    title: str,
    target: str = "",
    command: list[str] | tuple[str, ...] | str | None = None,
    steps: list[dict[str, Any]] | None = None,
    actor: Any | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = _now()
    job = {
        "id": f"job-{datetime.now().strftime('%Y%m%d%H%M%S%f')}",
        "type": job_type,
        "title": title,
        "target": target,
        "status": "Running",
        "command": command_to_text(command),
        "output": "",
        "steps": steps or [],
        "created_at": now,
        "updated_at": now,
        "finished_at": "",
        "metadata": metadata or {},
        **_actor_payload(actor),
    }
    jobs = load_jobs(limit=None)
    jobs.append(job)
    save_jobs(jobs)
    return job


def finish_job(
    job_id: str,
    success: bool,
    output: str = "",
    message: str = "",
    steps: list[dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    jobs = load_jobs(limit=None)
    now = _now()
    for index, job in enumerate(jobs):
        if job.get("id") == job_id:
            job["status"] = "Success" if success else "Failed"
            job["output"] = output or message
            job["message"] = message
            job["updated_at"] = now
            job["finished_at"] = now
            if steps is not None:
                job["steps"] = steps
            jobs[index] = job
            save_jobs(jobs)
            return job
    return None


def record_completed_job(
    job_type: str,
    title: str,
    target: str,
    success: bool,
    output: str = "",
    command: list[str] | tuple[str, ...] | str | None = None,
    steps: list[dict[str, Any]] | None = None,
    actor: Any | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    job = create_job(job_type, title, target, command, steps, actor, metadata)
    return finish_job(job["id"], success, output, output, steps) or job


def load_accessible_jobs(user: Any, limit: int | None = 200) -> list[dict[str, Any]]:
    jobs = load_jobs(limit=None)
    if not getattr(user, "is_admin", False):
        jobs = [job for job in jobs if job.get("actor_id") == getattr(user, "id", None)]
    jobs = sorted(jobs, key=lambda item: item.get("created_at", ""), reverse=True)
    return jobs[:limit] if limit else jobs


def pipeline_run_to_job(run: dict[str, Any]) -> dict[str, Any]:
    lines: list[str] = []
    for stage in run.get("stages", []):
        status = stage.get("status", "")
        message = stage.get("message", "")
        if status != "Waiting" or message:
            lines.append(f"[{status}] {stage.get('name', '')}: {message}")

    status = run.get("status", "Running")
    return {
        "id": run.get("id", ""),
        "type": "Pipeline",
        "title": f"CI/CD Pipeline - {run.get('application_name', run.get('application_id', 'application'))}",
        "target": run.get("application_name", run.get("application_id", "")),
        "status": status,
        "command": f"trigger_pipeline {run.get('application_id', '')}",
        "output": "\n".join(lines),
        "steps": run.get("stages", []),
        "created_at": run.get("created_at", ""),
        "updated_at": run.get("updated_at", ""),
        "finished_at": run.get("updated_at", "") if status in {"Success", "Failed"} else "",
        "actor": run.get("actor", "Platform"),
        "actor_id": run.get("actor_id"),
        "actor_role": run.get("actor_role", ""),
        "metadata": {"application_id": run.get("application_id", "")},
    }
