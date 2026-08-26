"""PostgreSQL backend for the delivery store.

This module mirrors the public API of ``app.delivery_store`` but persists to
PostgreSQL via SQLAlchemy Core (psycopg2). It is selected at runtime by
``delivery_store`` when ``DATABASE_URL`` points to ``postgresql://`` and no
explicit SQLite ``path`` is supplied, so the SQLite dev/test path is untouched.

Key differences from the SQLite implementation:

- ``payload TEXT`` → ``payload JSONB`` (read back with ``payload::text``).
- ``BEGIN IMMEDIATE`` claim/reserve → ``SELECT ... FOR UPDATE SKIP LOCKED`` and
  ``pg_advisory_xact_lock``.
- ``INSERT OR REPLACE`` / ``INSERT OR IGNORE`` → ``ON CONFLICT ... DO UPDATE`` /
  ``DO NOTHING``.
- No ``PRAGMA`` statements (PostgreSQL manages WAL and foreign keys natively).
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text

from app.config import BASE_DIR
from app.delivery_store import (
    MIGRATION_NAME,
    _backup_json_files,
    _configuration_payload,
    _deployment_payload,
    _interrupt_expired_run,
    _manifest_without_secrets,
    _payload,
)
from app.json_store import is_list_of_dicts, read_json, write_json

SCHEMA = """
CREATE TABLE IF NOT EXISTS delivery_migrations (
    name TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS applications (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    owner TEXT NOT NULL DEFAULT '',
    user_id INTEGER,
    namespace TEXT NOT NULL UNIQUE,
    source_type TEXT NOT NULL,
    repository_url TEXT NOT NULL DEFAULT '',
    default_branch TEXT NOT NULL DEFAULT 'main',
    requested_ref TEXT NOT NULL DEFAULT '',
    build_context TEXT NOT NULL DEFAULT '.',
    dockerfile_path TEXT NOT NULL DEFAULT 'Dockerfile',
    current_deployment_id TEXT,
    status TEXT NOT NULL DEFAULT 'Draft',
    url TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    payload JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS application_services (
    application_id TEXT NOT NULL,
    name TEXT NOT NULL,
    image TEXT NOT NULL DEFAULT '',
    required INTEGER NOT NULL DEFAULT 1,
    container_port INTEGER NOT NULL DEFAULT 80,
    service_type TEXT NOT NULL DEFAULT 'ClusterIP',
    health_path TEXT NOT NULL DEFAULT '',
    payload JSONB NOT NULL,
    PRIMARY KEY (application_id, name),
    FOREIGN KEY (application_id) REFERENCES applications(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS pipeline_runs (
    id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL,
    status TEXT NOT NULL,
    trigger_type TEXT NOT NULL DEFAULT 'Manual',
    repository_url TEXT NOT NULL DEFAULT '',
    branch TEXT NOT NULL DEFAULT '',
    commit_sha TEXT NOT NULL DEFAULT '',
    commit_author TEXT NOT NULL DEFAULT '',
    commit_message TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL DEFAULT 'system',
    actor_id INTEGER,
    actor_role TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    finished_at TEXT NOT NULL DEFAULT '',
    payload JSONB NOT NULL,
    FOREIGN KEY (application_id) REFERENCES applications(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_pipeline_runs_application_created
ON pipeline_runs(application_id, created_at DESC);

CREATE TABLE IF NOT EXISTS pipeline_stages (
    pipeline_run_id TEXT NOT NULL,
    name TEXT NOT NULL,
    position INTEGER NOT NULL,
    status TEXT NOT NULL,
    output TEXT NOT NULL DEFAULT '',
    started_at TEXT NOT NULL DEFAULT '',
    finished_at TEXT NOT NULL DEFAULT '',
    payload JSONB NOT NULL,
    PRIMARY KEY (pipeline_run_id, name),
    FOREIGN KEY (pipeline_run_id) REFERENCES pipeline_runs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS deployments (
    id TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    application_id TEXT NOT NULL,
    pipeline_run_id TEXT,
    previous_deployment_id TEXT,
    rollback_of_deployment_id TEXT,
    repository_url TEXT NOT NULL DEFAULT '',
    branch TEXT NOT NULL DEFAULT '',
    commit_sha TEXT NOT NULL DEFAULT '',
    manifest TEXT NOT NULL DEFAULT '',
    deployment_mode TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT 'system',
    actor_id INTEGER,
    namespace TEXT NOT NULL,
    url TEXT NOT NULL DEFAULT '',
    verify_status TEXT NOT NULL DEFAULT 'Pending',
    status TEXT NOT NULL DEFAULT 'Pending',
    started_at TEXT NOT NULL DEFAULT '',
    finished_at TEXT NOT NULL DEFAULT '',
    payload JSONB NOT NULL,
    UNIQUE(application_id, version),
    FOREIGN KEY (application_id) REFERENCES applications(id) ON DELETE CASCADE,
    FOREIGN KEY (pipeline_run_id) REFERENCES pipeline_runs(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS ix_deployments_application_version
ON deployments(application_id, version DESC);

CREATE TABLE IF NOT EXISTS deployment_services (
    deployment_id TEXT NOT NULL,
    service_name TEXT NOT NULL,
    image_tag TEXT NOT NULL,
    image_digest TEXT NOT NULL DEFAULT '',
    required INTEGER NOT NULL DEFAULT 1,
    payload JSONB NOT NULL,
    PRIMARY KEY (deployment_id, service_name),
    FOREIGN KEY (deployment_id) REFERENCES deployments(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    job_type TEXT NOT NULL,
    title TEXT NOT NULL,
    target TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT 'system',
    actor_id INTEGER,
    actor_role TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT '',
    finished_at TEXT NOT NULL DEFAULT '',
    payload JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id TEXT PRIMARY KEY,
    actor TEXT NOT NULL DEFAULT 'anonymous',
    actor_id INTEGER,
    role TEXT NOT NULL DEFAULT '',
    ip TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    target TEXT NOT NULL,
    result TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT '',
    payload JSONB NOT NULL
);

CREATE TABLE IF NOT EXISTS webhook_deliveries (
    delivery_id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    commit_sha TEXT NOT NULL DEFAULT '',
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    status TEXT NOT NULL DEFAULT 'Accepted',
    payload JSONB NOT NULL,
    FOREIGN KEY (application_id) REFERENCES applications(id) ON DELETE CASCADE
);
"""


def _database_url() -> str:
    return os.getenv("DATABASE_URL", "").strip()


# Reuse a single SQLAlchemy engine + connection pool for the whole process.
# Creating a new engine per call leaks a new pool (and its connections) each
# time, which quickly exhausts PostgreSQL's connection limit.
_engine_instance = None


def _engine():
    global _engine_instance
    if _engine_instance is None:
        url = _database_url()
        if not url:
            raise RuntimeError("DATABASE_URL is required for the PostgreSQL backend")
        _engine_instance = create_engine(
            url,
            pool_pre_ping=True,
            future=True,
            pool_size=5,
            max_overflow=10,
            pool_recycle=3600,
        )
    return _engine_instance


def initialize_schema(path: Path | None = None) -> None:
    with _engine().begin() as conn:
        conn.execute(text(SCHEMA))


def check_database(path: Path | None = None) -> None:
    initialize_schema(path)
    with _engine().begin() as conn:
        conn.execute(text("SELECT 1"))


# ---------------------------------------------------------------------------
# upsert helpers (operate on an open SQLAlchemy connection)
# ---------------------------------------------------------------------------

def _upsert_application(conn, application: dict[str, Any], *, insert_only: bool = False) -> int:
    conflict = "DO NOTHING" if insert_only else """DO UPDATE SET
        name=EXCLUDED.name, owner=EXCLUDED.owner, user_id=EXCLUDED.user_id,
        namespace=EXCLUDED.namespace, source_type=EXCLUDED.source_type,
        repository_url=EXCLUDED.repository_url, default_branch=EXCLUDED.default_branch,
        requested_ref=EXCLUDED.requested_ref, build_context=EXCLUDED.build_context,
        dockerfile_path=EXCLUDED.dockerfile_path,
        current_deployment_id=EXCLUDED.current_deployment_id,
        status=EXCLUDED.status, url=EXCLUDED.url, created_at=EXCLUDED.created_at,
        updated_at=EXCLUDED.updated_at, payload=EXCLUDED.payload"""
    result = conn.execute(
        text(f"""INSERT INTO applications(
            id, name, owner, user_id, namespace, source_type, repository_url,
            default_branch, requested_ref, build_context, dockerfile_path,
            current_deployment_id, status, url, created_at, updated_at, payload
        ) VALUES (:id, :name, :owner, :user_id, :namespace, :source_type, :repository_url,
            :default_branch, :requested_ref, :build_context, :dockerfile_path,
            :current_deployment_id, :status, :url, :created_at, :updated_at,
            CAST(:payload AS JSONB))
        ON CONFLICT (id) {conflict}"""),
        {
            "id": application["id"],
            "name": application.get("name", application["id"]),
            "owner": application.get("owner", ""),
            "user_id": application.get("user_id"),
            "namespace": application.get("namespace", application["id"]),
            "source_type": application.get("source_type", "docker"),
            "repository_url": application.get("github_url", ""),
            "default_branch": application.get("default_branch", "main"),
            "requested_ref": application.get("requested_ref", ""),
            "build_context": application.get("build_context", "."),
            "dockerfile_path": application.get("dockerfile_path", "Dockerfile"),
            "current_deployment_id": application.get("current_deployment_id"),
            "status": application.get("status", "Draft"),
            "url": application.get("url", ""),
            "created_at": application.get("created_at", ""),
            "updated_at": application.get("updated_at", ""),
            "payload": _configuration_payload(application),
        },
    )
    return result.rowcount or 0


def _upsert_service(conn, application_id: str, service: dict[str, Any], *, insert_only: bool = False) -> int:
    conflict = "DO NOTHING" if insert_only else """DO UPDATE SET
        image=EXCLUDED.image, required=EXCLUDED.required,
        container_port=EXCLUDED.container_port, service_type=EXCLUDED.service_type,
        health_path=EXCLUDED.health_path, payload=EXCLUDED.payload"""
    result = conn.execute(
        text(f"""INSERT INTO application_services(
            application_id, name, image, required, container_port,
            service_type, health_path, payload
        ) VALUES (:application_id, :name, :image, :required, :container_port,
            :service_type, :health_path, CAST(:payload AS JSONB))
        ON CONFLICT (application_id, name) {conflict}"""),
        {
            "application_id": application_id,
            "name": service["name"],
            "image": service.get("image", ""),
            "required": int(service.get("required", True)),
            "container_port": int(service.get("container_port", 80)),
            "service_type": service.get("service_type", "ClusterIP"),
            "health_path": service.get("health_path", ""),
            "payload": _configuration_payload(service),
        },
    )
    return result.rowcount or 0


def _upsert_pipeline_run(conn, run: dict[str, Any], *, insert_only: bool = False) -> int:
    conflict = "DO NOTHING" if insert_only else """DO UPDATE SET
        application_id=EXCLUDED.application_id, status=EXCLUDED.status,
        trigger_type=EXCLUDED.trigger_type, repository_url=EXCLUDED.repository_url,
        branch=EXCLUDED.branch, commit_sha=EXCLUDED.commit_sha,
        commit_author=EXCLUDED.commit_author, commit_message=EXCLUDED.commit_message,
        actor=EXCLUDED.actor, actor_id=EXCLUDED.actor_id,
        actor_role=EXCLUDED.actor_role, created_at=EXCLUDED.created_at,
        updated_at=EXCLUDED.updated_at, finished_at=EXCLUDED.finished_at,
        payload=EXCLUDED.payload"""
    result = conn.execute(
        text(f"""INSERT INTO pipeline_runs(
            id, application_id, status, trigger_type, repository_url, branch,
            commit_sha, commit_author, commit_message, actor, actor_id,
            actor_role, created_at, updated_at, finished_at, payload
        ) VALUES (:id, :application_id, :status, :trigger_type, :repository_url, :branch,
            :commit_sha, :commit_author, :commit_message, :actor, :actor_id,
            :actor_role, :created_at, :updated_at, :finished_at, CAST(:payload AS JSONB))
        ON CONFLICT (id) {conflict}"""),
        {
            "id": run["id"],
            "application_id": run["application_id"],
            "status": run.get("status", "Running"),
            "trigger_type": run.get("trigger_type", "Manual"),
            "repository_url": run.get("repository", run.get("repository_url", "")),
            "branch": run.get("branch", ""),
            "commit_sha": run.get("commit_sha", ""),
            "commit_author": run.get("commit_author", ""),
            "commit_message": run.get("commit_message", ""),
            "actor": run.get("actor", "system"),
            "actor_id": run.get("actor_id"),
            "actor_role": run.get("actor_role", ""),
            "created_at": run.get("created_at", ""),
            "updated_at": run.get("updated_at", ""),
            "finished_at": run.get("finished_at", ""),
            "payload": _payload(run),
        },
    )
    return result.rowcount or 0


def _upsert_pipeline_stage(conn, run_id: str, position: int, stage: dict[str, Any], *, insert_only: bool = False) -> int:
    conflict = "DO NOTHING" if insert_only else """DO UPDATE SET
        position=EXCLUDED.position, status=EXCLUDED.status, output=EXCLUDED.output,
        started_at=EXCLUDED.started_at, finished_at=EXCLUDED.finished_at,
        payload=EXCLUDED.payload"""
    result = conn.execute(
        text(f"""INSERT INTO pipeline_stages(
            pipeline_run_id, name, position, status, output, started_at,
            finished_at, payload
        ) VALUES (:run_id, :name, :position, :status, :output, :started_at,
            :finished_at, CAST(:payload AS JSONB))
        ON CONFLICT (pipeline_run_id, name) {conflict}"""),
        {
            "run_id": run_id,
            "name": stage["name"],
            "position": position,
            "status": stage.get("status", "Waiting"),
            "output": stage.get("output", stage.get("message", "")),
            "started_at": stage.get("started_at", ""),
            "finished_at": stage.get("finished_at", ""),
            "payload": _payload(stage),
        },
    )
    return result.rowcount or 0


def _upsert_job(conn, job: dict[str, Any], *, insert_only: bool = False) -> int:
    conflict = "DO NOTHING" if insert_only else """DO UPDATE SET
        job_type=EXCLUDED.job_type, title=EXCLUDED.title, target=EXCLUDED.target,
        status=EXCLUDED.status, actor=EXCLUDED.actor, actor_id=EXCLUDED.actor_id,
        actor_role=EXCLUDED.actor_role, created_at=EXCLUDED.created_at,
        updated_at=EXCLUDED.updated_at, finished_at=EXCLUDED.finished_at,
        payload=EXCLUDED.payload"""
    result = conn.execute(
        text(f"""INSERT INTO jobs(
            id, job_type, title, target, status, actor, actor_id, actor_role,
            created_at, updated_at, finished_at, payload
        ) VALUES (:id, :job_type, :title, :target, :status, :actor, :actor_id,
            :actor_role, :created_at, :updated_at, :finished_at, CAST(:payload AS JSONB))
        ON CONFLICT (id) {conflict}"""),
        {
            "id": job["id"],
            "job_type": job.get("type", ""),
            "title": job.get("title", ""),
            "target": job.get("target", ""),
            "status": job.get("status", "Running"),
            "actor": job.get("actor", "system"),
            "actor_id": job.get("actor_id"),
            "actor_role": job.get("actor_role", ""),
            "created_at": job.get("created_at", ""),
            "updated_at": job.get("updated_at", ""),
            "finished_at": job.get("finished_at", ""),
            "payload": _payload(job),
        },
    )
    return result.rowcount or 0


def _upsert_audit(conn, audit: dict[str, Any], *, insert_only: bool = False) -> int:
    conflict = "DO NOTHING" if insert_only else """DO UPDATE SET
        actor=EXCLUDED.actor, actor_id=EXCLUDED.actor_id, role=EXCLUDED.role,
        ip=EXCLUDED.ip, action=EXCLUDED.action, target=EXCLUDED.target,
        result=EXCLUDED.result, created_at=EXCLUDED.created_at, payload=EXCLUDED.payload"""
    result = conn.execute(
        text(f"""INSERT INTO audit_logs(
            id, actor, actor_id, role, ip, action, target, result, created_at, payload
        ) VALUES (:id, :actor, :actor_id, :role, :ip, :action, :target, :result,
            :created_at, CAST(:payload AS JSONB))
        ON CONFLICT (id) {conflict}"""),
        {
            "id": audit["id"],
            "actor": audit.get("user", audit.get("actor", "anonymous")),
            "actor_id": audit.get("user_id", audit.get("actor_id")),
            "role": audit.get("role", ""),
            "ip": audit.get("ip", ""),
            "action": audit.get("action", ""),
            "target": audit.get("target", ""),
            "result": audit.get("result", ""),
            "created_at": audit.get("created_at", audit.get("time", "")),
            "payload": _payload(audit),
        },
    )
    return result.rowcount or 0


def _upsert_deployment(conn, deployment: dict[str, Any]) -> None:
    safe_manifest = _manifest_without_secrets(str(deployment.get("manifest", "")))
    conn.execute(
        text("""INSERT INTO deployments(
            id, version, application_id, pipeline_run_id, previous_deployment_id,
            rollback_of_deployment_id, repository_url, branch, commit_sha,
            manifest, deployment_mode, actor, actor_id, namespace, url,
            verify_status, status, started_at, finished_at, payload
        ) VALUES (:id, :version, :application_id, :pipeline_run_id, :previous_deployment_id,
            :rollback_of_deployment_id, :repository_url, :branch, :commit_sha,
            :manifest, :deployment_mode, :actor, :actor_id, :namespace, :url,
            :verify_status, :status, :started_at, :finished_at, CAST(:payload AS JSONB))
        ON CONFLICT (id) DO UPDATE SET
            pipeline_run_id=EXCLUDED.pipeline_run_id,
            previous_deployment_id=EXCLUDED.previous_deployment_id,
            rollback_of_deployment_id=EXCLUDED.rollback_of_deployment_id,
            repository_url=EXCLUDED.repository_url, branch=EXCLUDED.branch,
            commit_sha=EXCLUDED.commit_sha, manifest=EXCLUDED.manifest,
            deployment_mode=EXCLUDED.deployment_mode, actor=EXCLUDED.actor,
            actor_id=EXCLUDED.actor_id, namespace=EXCLUDED.namespace,
            url=EXCLUDED.url, verify_status=EXCLUDED.verify_status,
            status=EXCLUDED.status, started_at=EXCLUDED.started_at,
            finished_at=EXCLUDED.finished_at, payload=EXCLUDED.payload"""),
        {
            "id": deployment["id"],
            "version": deployment["version"],
            "application_id": deployment["application_id"],
            "pipeline_run_id": deployment.get("pipeline_run_id"),
            "previous_deployment_id": deployment.get("previous_deployment_id"),
            "rollback_of_deployment_id": deployment.get("rollback_of_deployment_id"),
            "repository_url": deployment.get("repository", ""),
            "branch": deployment.get("branch", ""),
            "commit_sha": deployment.get("commit_sha", ""),
            "manifest": safe_manifest,
            "deployment_mode": deployment.get("deployment_mode", "Production"),
            "actor": deployment.get("actor", "system"),
            "actor_id": deployment.get("actor_id"),
            "namespace": deployment.get("namespace", ""),
            "url": deployment.get("url", ""),
            "verify_status": deployment.get("verify_status", "Pending"),
            "status": deployment.get("status", "Pending"),
            "started_at": deployment.get("started_at", ""),
            "finished_at": deployment.get("finished_at", ""),
            "payload": _deployment_payload(deployment),
        },
    )


def _replace_deployment_services(conn, deployment: dict[str, Any]) -> None:
    conn.execute(
        text("DELETE FROM deployment_services WHERE deployment_id = :did"),
        {"did": deployment["id"]},
    )
    for service in deployment.get("services", []):
        conn.execute(
            text("""INSERT INTO deployment_services(
                deployment_id, service_name, image_tag, image_digest, required, payload
            ) VALUES (:did, :name, :image, :digest, :required, CAST(:payload AS JSONB))
            ON CONFLICT (deployment_id, service_name) DO UPDATE SET
                image_tag=EXCLUDED.image_tag, image_digest=EXCLUDED.image_digest,
                required=EXCLUDED.required, payload=EXCLUDED.payload"""),
            {
                "did": deployment["id"],
                "name": service["service"],
                "image": service.get("image", ""),
                "digest": service.get("digest", ""),
                "required": int(service.get("required", True)),
                "payload": _payload(service),
            },
        )


# ---------------------------------------------------------------------------
# public API (mirrors app.delivery_store)
# ---------------------------------------------------------------------------

def migrate_json_state(
    applications_file: Path,
    pipeline_file: Path,
    jobs_file: Path,
    audit_file: Path,
    *,
    path: Path | None = None,
    backup_dir: Path | None = None,
) -> dict[str, int]:
    initialize_schema(path)
    files = [applications_file, pipeline_file, jobs_file, audit_file]
    default_backup_dir = Path(
        os.getenv("PLATFORM_DATA_DIR", str(applications_file.parent))
    ) / "phase0-backup"
    _backup_json_files(files, backup_dir or default_backup_dir)

    applications = read_json(applications_file, [], is_list_of_dicts)
    pipelines = read_json(pipeline_file, [], is_list_of_dicts)
    jobs = read_json(jobs_file, [], is_list_of_dicts)
    audits = read_json(audit_file, [], is_list_of_dicts)
    imported = {"applications": 0, "services": 0, "pipeline_runs": 0, "pipeline_stages": 0, "jobs": 0, "audit_logs": 0}

    with _engine().begin() as conn:
        marker = conn.execute(
            text("SELECT 1 FROM delivery_migrations WHERE name = :name"),
            {"name": MIGRATION_NAME},
        ).first()
        if marker:
            return imported

        for application in applications:
            imported["applications"] += _upsert_application(conn, application, insert_only=True)
            for service in application.get("services", []):
                imported["services"] += _upsert_service(conn, application["id"], service, insert_only=True)

        for run in pipelines:
            exists = conn.execute(
                text("SELECT 1 FROM applications WHERE id = :id"),
                {"id": run.get("application_id")},
            ).first()
            if not exists:
                continue
            imported["pipeline_runs"] += _upsert_pipeline_run(conn, run, insert_only=True)
            for position, stage in enumerate(run.get("stages", [])):
                imported["pipeline_stages"] += _upsert_pipeline_stage(
                    conn, run["id"], position, stage, insert_only=True
                )

        for job in jobs:
            imported["jobs"] += _upsert_job(conn, job, insert_only=True)
        for audit in audits:
            imported["audit_logs"] += _upsert_audit(conn, audit, insert_only=True)

        conn.execute(
            text("INSERT INTO delivery_migrations(name) VALUES (:name) ON CONFLICT DO NOTHING"),
            {"name": MIGRATION_NAME},
        )
    return imported


def migrate_default_json_state(path: Path | None = None) -> dict[str, int]:
    data_dir = BASE_DIR / "app" / "data"
    return migrate_json_state(
        data_dir / "applications.json",
        data_dir / "pipeline_runs.json",
        data_dir / "jobs.json",
        data_dir / "audit_logs.json",
        path=path,
    )


def replace_applications(applications: list[dict[str, Any]], path: Path | None = None) -> None:
    initialize_schema(path)
    ids = {application["id"] for application in applications}
    with _engine().begin() as conn:
        existing = {row[0] for row in conn.execute(text("SELECT id FROM applications"))}
        for removed_id in existing - ids:
            conn.execute(text("DELETE FROM applications WHERE id = :id"), {"id": removed_id})
        for application in applications:
            _upsert_application(conn, application)
            conn.execute(
                text("DELETE FROM application_services WHERE application_id = :id"),
                {"id": application["id"]},
            )
            for service in application.get("services", []):
                _upsert_service(conn, application["id"], service)


def list_applications(path: Path | None = None) -> list[dict[str, Any]]:
    initialize_schema(path)
    with _engine().begin() as conn:
        rows = conn.execute(
            text("SELECT payload::text AS payload FROM applications ORDER BY created_at")
        ).mappings().all()
    return [json.loads(row["payload"]) for row in rows]


def upsert_pipeline_run(run: dict[str, Any], path: Path | None = None) -> None:
    initialize_schema(path)
    with _engine().begin() as conn:
        _upsert_pipeline_run(conn, run)
        conn.execute(
            text("DELETE FROM pipeline_stages WHERE pipeline_run_id = :id"),
            {"id": run["id"]},
        )
        for position, stage in enumerate(run.get("stages", [])):
            _upsert_pipeline_stage(conn, run["id"], position, stage)


def list_pipeline_runs(
    application_id: str | None = None, path: Path | None = None
) -> list[dict[str, Any]]:
    initialize_schema(path)
    query = "SELECT payload::text AS payload FROM pipeline_runs"
    params: dict[str, Any] = {}
    if application_id:
        query += " WHERE application_id = :application_id"
        params["application_id"] = application_id
    query += " ORDER BY created_at DESC, id DESC"
    with _engine().begin() as conn:
        rows = conn.execute(text(query), params).mappings().all()
    return [json.loads(row["payload"]) for row in rows]


def get_pipeline_run(run_id: str, path: Path | None = None) -> dict[str, Any] | None:
    initialize_schema(path)
    with _engine().begin() as conn:
        row = conn.execute(
            text("SELECT payload::text AS payload FROM pipeline_runs WHERE id = :id"),
            {"id": run_id},
        ).mappings().first()
    return json.loads(row["payload"]) if row else None


def _lease_seconds(value: int | None = None) -> int:
    return value or max(30, int(os.getenv("PIPELINE_LEASE_SECONDS", "300")))


def mark_pipeline_running(
    run_id: str, worker_id: str, path: Path | None = None,
    *, lease_seconds: int | None = None,
) -> dict[str, Any] | None:
    """Atomically change Queued to Running; duplicate deliveries get ``None``."""
    if not worker_id.strip():
        raise ValueError("worker_id is required")
    lease_seconds = _lease_seconds(lease_seconds)
    initialize_schema(path)
    with _engine().begin() as conn:
        row = conn.execute(
            text(
                "SELECT payload::text AS payload FROM pipeline_runs "
                "WHERE id = :id AND status = 'Queued' FOR UPDATE"
            ),
            {"id": run_id},
        ).mappings().first()
        if not row:
            return None
        run = json.loads(row["payload"])
        now = datetime.now(timezone.utc)
        timestamp = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        run.update({
            "status": "Running",
            "worker_id": worker_id,
            "claimed_at": timestamp,
            "heartbeat_at": timestamp,
            "lease_expires_at": (now + timedelta(seconds=lease_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "updated_at": timestamp,
            "attempt": int(run.get("attempt", 0) or 0) + 1,
        })
        _upsert_pipeline_run(conn, run)
        return run


def renew_pipeline_lease(
    run_id: str, worker_id: str, path: Path | None = None,
    *, lease_seconds: int | None = None,
) -> bool:
    """Extend an owned lease without replacing concurrently updated stage data."""
    lease_seconds = _lease_seconds(lease_seconds)
    initialize_schema(path)
    now = datetime.now(timezone.utc)
    heartbeat_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    lease_expires_at = (now + timedelta(seconds=lease_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _engine().begin() as conn:
        result = conn.execute(
            text("""UPDATE pipeline_runs
                SET updated_at = :heartbeat_at,
                    payload = jsonb_set(
                        jsonb_set(
                            jsonb_set(payload, '{heartbeat_at}', to_jsonb(CAST(:heartbeat_at AS text)), true),
                            '{lease_expires_at}', to_jsonb(CAST(:lease_expires_at AS text)), true),
                        '{updated_at}', to_jsonb(CAST(:heartbeat_at AS text)), true)
                WHERE id = :id AND status = 'Running'
                  AND payload->>'worker_id' = :worker_id"""),
            {
                "heartbeat_at": heartbeat_at,
                "lease_expires_at": lease_expires_at,
                "id": run_id,
                "worker_id": worker_id,
            },
        )
    return result.rowcount == 1


def recover_expired_pipeline_runs(path: Path | None = None) -> int:
    initialize_schema(path)
    now = datetime.now(timezone.utc)
    recovered = 0
    with _engine().begin() as conn:
        rows = conn.execute(
            text(
                "SELECT payload::text AS payload FROM pipeline_runs "
                "WHERE status = 'Running' FOR UPDATE SKIP LOCKED"
            )
        ).mappings().all()
        for row in rows:
            run = json.loads(row["payload"])
            raw_expiry = str(run.get("lease_expires_at", ""))
            if not raw_expiry:
                continue
            try:
                expiry = datetime.fromisoformat(raw_expiry.replace("Z", "+00:00"))
            except ValueError:
                continue
            if expiry > now:
                continue
            recovered += 1
            _interrupt_expired_run(run, now)
            _upsert_pipeline_run(conn, run)
            for position, stage in enumerate(run.get("stages", [])):
                _upsert_pipeline_stage(conn, run["id"], position, stage)
    return recovered

def claim_next_pipeline_run(
    worker_id: str, path: Path | None = None
) -> dict[str, Any] | None:
    if not worker_id.strip():
        raise ValueError("worker_id is required")
    initialize_schema(path)
    with _engine().begin() as conn:
        row = conn.execute(
            text(
                "SELECT payload::text AS payload FROM pipeline_runs "
                "WHERE status = 'Queued' ORDER BY created_at, id "
                "FOR UPDATE SKIP LOCKED LIMIT 1"
            )
        ).mappings().first()
        if not row:
            return None
        run = json.loads(row["payload"])
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        run["status"] = "Running"
        run["worker_id"] = worker_id
        run["claimed_at"] = now
        run["updated_at"] = now
        run["attempt"] = int(run.get("attempt", 0) or 0) + 1
        _upsert_pipeline_run(conn, run)
        return run


def has_active_pipeline(application_id: str, path: Path | None = None) -> bool:
    initialize_schema(path)
    with _engine().begin() as conn:
        row = conn.execute(
            text(
                "SELECT 1 FROM pipeline_runs WHERE application_id = :id "
                "AND status IN ('Queued', 'Running') LIMIT 1"
            ),
            {"id": application_id},
        ).first()
    return row is not None


def reserve_pipeline_run(run: dict[str, Any], path: Path | None = None) -> None:
    initialize_schema(path)
    with _engine().begin() as conn:
        conn.execute(text("SELECT pg_advisory_xact_lock(hashtext('platform:pipeline:reserve'))"))
        active = conn.execute(
            text(
                "SELECT id FROM pipeline_runs WHERE application_id = :id "
                "AND status IN ('Queued', 'Running') LIMIT 1"
            ),
            {"id": run["application_id"]},
        ).mappings().first()
        if active:
            raise ValueError(
                f"Application đã có pipeline đang chạy hoặc chờ: {active['id']}"
            )
        try:
            max_concurrent = max(1, int(os.getenv("PIPELINE_MAX_CONCURRENT", "1")))
        except ValueError:
            max_concurrent = 1
        active_count = conn.execute(
            text("SELECT COUNT(*) FROM pipeline_runs WHERE status IN ('Queued', 'Running')")
        ).scalar()
        if active_count >= max_concurrent:
            raise ValueError(
                "Build Worker đang đạt giới hạn "
                f"{max_concurrent} pipeline đồng thời. Vui lòng thử lại sau."
            )
        _upsert_pipeline_run(conn, run)
        for position, stage in enumerate(run.get("stages", [])):
            _upsert_pipeline_stage(conn, run["id"], position, stage)


def replace_jobs(jobs: list[dict[str, Any]], path: Path | None = None) -> None:
    initialize_schema(path)
    with _engine().begin() as conn:
        for job in jobs:
            _upsert_job(conn, job)


def list_jobs(path: Path | None = None) -> list[dict[str, Any]]:
    initialize_schema(path)
    with _engine().begin() as conn:
        rows = conn.execute(
            text("SELECT payload::text AS payload FROM jobs ORDER BY created_at DESC")
        ).mappings().all()
    return [json.loads(row["payload"]) for row in rows]


def replace_audit_logs(logs: list[dict[str, Any]], path: Path | None = None) -> None:
    initialize_schema(path)
    with _engine().begin() as conn:
        for audit in logs:
            _upsert_audit(conn, audit)


def list_audit_logs(path: Path | None = None) -> list[dict[str, Any]]:
    initialize_schema(path)
    with _engine().begin() as conn:
        rows = conn.execute(
            text("SELECT payload::text AS payload FROM audit_logs ORDER BY created_at DESC")
        ).mappings().all()
    return [json.loads(row["payload"]) for row in rows]


def create_deployment_record(
    deployment: dict[str, Any], path: Path | None = None
) -> dict[str, Any]:
    initialize_schema(path)
    with _engine().begin() as conn:
        conn.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:app))"),
            {"app": deployment["application_id"]},
        )
        version = conn.execute(
            text(
                "SELECT COALESCE(MAX(version), 0) + 1 FROM deployments "
                "WHERE application_id = :app"
            ),
            {"app": deployment["application_id"]},
        ).scalar()
        record = {
            **deployment,
            "id": deployment.get("id") or f"deployment-{uuid.uuid4()}",
            "version": version,
        }
        _upsert_deployment(conn, record)
        _replace_deployment_services(conn, record)
        return record


def update_deployment_record(
    deployment: dict[str, Any], path: Path | None = None
) -> dict[str, Any]:
    initialize_schema(path)
    with _engine().begin() as conn:
        _upsert_deployment(conn, deployment)
        _replace_deployment_services(conn, deployment)
    return deployment


def get_deployment(deployment_id: str, path: Path | None = None) -> dict[str, Any] | None:
    initialize_schema(path)
    with _engine().begin() as conn:
        row = conn.execute(
            text("SELECT payload::text AS payload FROM deployments WHERE id = :id"),
            {"id": deployment_id},
        ).mappings().first()
    return json.loads(row["payload"]) if row else None


def list_deployments(
    application_id: str | None = None, path: Path | None = None
) -> list[dict[str, Any]]:
    initialize_schema(path)
    query = "SELECT payload::text AS payload FROM deployments"
    params: dict[str, Any] = {}
    if application_id:
        query += " WHERE application_id = :application_id"
        params["application_id"] = application_id
    query += " ORDER BY version DESC"
    with _engine().begin() as conn:
        rows = conn.execute(text(query), params).mappings().all()
    return [json.loads(row["payload"]) for row in rows]


def claim_webhook_delivery(
    delivery_id: str,
    application_id: str,
    event_type: str,
    commit_sha: str,
    metadata: dict[str, Any],
    path: Path | None = None,
) -> bool:
    initialize_schema(path)
    with _engine().begin() as conn:
        result = conn.execute(
            text("""INSERT INTO webhook_deliveries(
                delivery_id, application_id, event_type, commit_sha, payload
            ) VALUES (:delivery_id, :application_id, :event_type, :commit_sha, CAST(:payload AS JSONB))
            ON CONFLICT (delivery_id) DO NOTHING"""),
            {
                "delivery_id": delivery_id,
                "application_id": application_id,
                "event_type": event_type,
                "commit_sha": commit_sha,
                "payload": _payload(metadata),
            },
        )
    return result.rowcount == 1
