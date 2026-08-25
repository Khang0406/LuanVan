"""SQLite persistence for application delivery and CI/CD state.

The platform historically stored delivery state in JSON files.  This module
keeps those files as a compatibility mirror while making SQLite the durable,
normalized source of truth. It deliberately uses the standard ``sqlite3``
module so the standalone pipeline worker does not depend on a Flask
application context.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import uuid
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import yaml

from app.config import BASE_DIR
from app.json_store import is_list_of_dicts, mask_secrets, read_json, write_json


DEFAULT_DATABASE_PATH = BASE_DIR / "instance" / "app.db"
MIGRATION_NAME = "phase1-json-import-v1"


def database_path() -> Path:
    configured = os.getenv("DELIVERY_DATABASE_PATH", "").strip()
    return Path(configured).expanduser() if configured else DEFAULT_DATABASE_PATH


def _use_postgres(path: Path | None = None) -> bool:
    """Select the PostgreSQL backend when DATABASE_URL is a postgres URL and no
    explicit SQLite path is given.

    An explicit ``path`` is the unit-test contract and always means SQLite, so
    the existing 100+ SQLite tests are unaffected by production PostgreSQL.
    """
    if path is not None:
        return False
    url = os.getenv("DATABASE_URL", "").strip().lower()
    return url.startswith("postgresql") or url.startswith("postgres")


def _pg_backend():
    """Lazily import the PostgreSQL backend to avoid a circular import."""
    from app import delivery_store_pg

    return delivery_store_pg


def _connect(path: Path | None = None) -> sqlite3.Connection:
    target = path or database_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(target, timeout=30)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    return connection


def check_database(path: Path | None = None) -> None:
    """Raise when the delivery database cannot be opened and queried."""
    if _use_postgres(path):
        return _pg_backend().check_database(path)
    initialize_schema(path)
    with _connect(path) as connection:
        connection.execute("SELECT 1").fetchone()


SCHEMA = """
CREATE TABLE IF NOT EXISTS delivery_migrations (
    name TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS application_services (
    application_id TEXT NOT NULL,
    name TEXT NOT NULL,
    image TEXT NOT NULL DEFAULT '',
    required INTEGER NOT NULL DEFAULT 1,
    container_port INTEGER NOT NULL DEFAULT 80,
    service_type TEXT NOT NULL DEFAULT 'ClusterIP',
    health_path TEXT NOT NULL DEFAULT '',
    payload TEXT NOT NULL,
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
    payload TEXT NOT NULL,
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
    payload TEXT NOT NULL,
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
    payload TEXT NOT NULL,
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
    payload TEXT NOT NULL,
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
    payload TEXT NOT NULL
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
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS webhook_deliveries (
    delivery_id TEXT PRIMARY KEY,
    application_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    commit_sha TEXT NOT NULL DEFAULT '',
    received_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    status TEXT NOT NULL DEFAULT 'Accepted',
    payload TEXT NOT NULL,
    FOREIGN KEY (application_id) REFERENCES applications(id) ON DELETE CASCADE
);
"""


def initialize_schema(path: Path | None = None) -> None:
    if _use_postgres(path):
        return _pg_backend().initialize_schema(path)
    with _connect(path) as connection:
        connection.executescript(SCHEMA)


def _payload(value: dict[str, Any]) -> str:
    return json.dumps(mask_secrets(value), ensure_ascii=False)


_CONFIG_REFERENCE_FIELDS = {
    "credential_ref",
    "image_pull_secret",
    "secret_name",
    "secret_ref",
    "secret_refs",
    "secrets",
}


def _mask_configuration(value: Any) -> Any:
    """Mask inline values while preserving non-sensitive Secret references."""
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        env_name = str(value.get("name", ""))
        for key, item in value.items():
            normalized_key = str(key).lower()
            if normalized_key in _CONFIG_REFERENCE_FIELDS:
                result[key] = deepcopy(item)
            elif normalized_key in {
                "password",
                "passwd",
                "pwd",
                "token",
                "api_key",
                "authorization",
            }:
                result[key] = "***"
            elif normalized_key == "value" and any(
                marker in env_name.lower()
                for marker in (
                    "password",
                    "passwd",
                    "pwd",
                    "token",
                    "secret",
                    "api_key",
                    "authorization",
                )
            ):
                result[key] = "***"
            else:
                result[key] = _mask_configuration(item)
        return result
    if isinstance(value, list):
        return [_mask_configuration(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_mask_configuration(item) for item in value)
    return value


def _configuration_payload(value: dict[str, Any]) -> str:
    return json.dumps(_mask_configuration(value), ensure_ascii=False)


def _manifest_without_secrets(manifest: str) -> str:
    if not manifest.strip():
        return ""
    try:
        loaded_documents = [
            document for document in yaml.safe_load_all(manifest) if document
        ]
    except yaml.YAMLError:
        return mask_secrets(manifest)
    if any(not isinstance(document, dict) for document in loaded_documents):
        return mask_secrets(manifest)
    documents = [
        document
        for document in loaded_documents
        if document.get("kind") != "Secret"
    ]
    return yaml.safe_dump_all(documents, sort_keys=False)


def _deployment_payload(deployment: dict[str, Any]) -> str:
    safe_deployment = deepcopy(deployment)
    manifest = _manifest_without_secrets(str(safe_deployment.pop("manifest", "")))
    safe_deployment = mask_secrets(safe_deployment)
    safe_deployment["manifest"] = manifest
    return json.dumps(safe_deployment, ensure_ascii=False)


def _decode(row: sqlite3.Row) -> dict[str, Any]:
    return json.loads(row["payload"])


def _backup_json_files(files: Iterable[Path], backup_dir: Path) -> None:
    backup_dir.mkdir(parents=True, exist_ok=True)
    for source in files:
        if not source.exists():
            continue
        destination = backup_dir / source.name
        if not destination.exists():
            shutil.copy2(source, destination)
            destination.chmod(0o600)


def migrate_json_state(
    applications_file: Path,
    pipeline_file: Path,
    jobs_file: Path,
    audit_file: Path,
    *,
    path: Path | None = None,
    backup_dir: Path | None = None,
) -> dict[str, int]:
    """Import legacy JSON exactly once per database.

    Inserts are keyed by stable record identifiers, so even deleting the
    migration marker and re-running the import cannot create duplicates.
    """
    if _use_postgres(path):
        return _pg_backend().migrate_json_state(
            applications_file, pipeline_file, jobs_file, audit_file,
            path=path, backup_dir=backup_dir,
        )
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

    # Move legacy inline registry secrets into the permission-restricted
    # credential store before application payloads are masked for SQLite.
    if path is None or path == database_path():
        from app.registry_credentials import import_legacy_application_credentials

        import_legacy_application_credentials(applications)
        legacy_changed = False
        for application in applications:
            registry = application.get("registry")
            if not isinstance(registry, dict):
                continue
            if registry.get("password") or registry.get("token"):
                registry["credential_ref"] = registry.get(
                    "credential_ref", f"application:{application['id']}"
                )
                registry.pop("password", None)
                registry.pop("token", None)
                legacy_changed = True
        if legacy_changed:
            write_json(applications_file, applications)

    with _connect(path) as connection:
        marker = connection.execute(
            "SELECT 1 FROM delivery_migrations WHERE name = ?", (MIGRATION_NAME,)
        ).fetchone()
        if marker:
            return imported

        for application in applications:
            imported["applications"] += _upsert_application(connection, application, insert_only=True)
            for service in application.get("services", []):
                imported["services"] += _upsert_service(
                    connection, application["id"], service, insert_only=True
                )

        for run in pipelines:
            if not connection.execute(
                "SELECT 1 FROM applications WHERE id = ?", (run.get("application_id"),)
            ).fetchone():
                continue
            imported["pipeline_runs"] += _upsert_pipeline_run(connection, run, insert_only=True)
            for position, stage in enumerate(run.get("stages", [])):
                imported["pipeline_stages"] += _upsert_pipeline_stage(
                    connection, run["id"], position, stage, insert_only=True
                )

        for job in jobs:
            imported["jobs"] += _upsert_job(connection, job, insert_only=True)
        for audit in audits:
            imported["audit_logs"] += _upsert_audit(connection, audit, insert_only=True)

        connection.execute(
            "INSERT OR IGNORE INTO delivery_migrations(name) VALUES (?)", (MIGRATION_NAME,)
        )
    return imported


def migrate_default_json_state(path: Path | None = None) -> dict[str, int]:
    if _use_postgres(path):
        return _pg_backend().migrate_default_json_state(path)
    data_dir = BASE_DIR / "app" / "data"
    return migrate_json_state(
        data_dir / "applications.json",
        data_dir / "pipeline_runs.json",
        data_dir / "jobs.json",
        data_dir / "audit_logs.json",
        path=path,
    )


def _upsert_application(
    connection: sqlite3.Connection, application: dict[str, Any], *, insert_only: bool = False
) -> int:
    conflict = "DO NOTHING" if insert_only else """DO UPDATE SET
        name=excluded.name, owner=excluded.owner, user_id=excluded.user_id,
        namespace=excluded.namespace, source_type=excluded.source_type,
        repository_url=excluded.repository_url, default_branch=excluded.default_branch,
        requested_ref=excluded.requested_ref, build_context=excluded.build_context,
        dockerfile_path=excluded.dockerfile_path,
        current_deployment_id=excluded.current_deployment_id,
        status=excluded.status, url=excluded.url, created_at=excluded.created_at,
        updated_at=excluded.updated_at, payload=excluded.payload"""
    cursor = connection.execute(
        f"""INSERT INTO applications(
            id, name, owner, user_id, namespace, source_type, repository_url,
            default_branch, requested_ref, build_context, dockerfile_path,
            current_deployment_id, status, url, created_at, updated_at, payload
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) {conflict}""",
        (
            application["id"], application.get("name", application["id"]),
            application.get("owner", ""), application.get("user_id"),
            application.get("namespace", application["id"]),
            application.get("source_type", "docker"),
            application.get("github_url", ""), application.get("default_branch", "main"),
            application.get("requested_ref", ""), application.get("build_context", "."),
            application.get("dockerfile_path", "Dockerfile"),
            application.get("current_deployment_id"), application.get("status", "Draft"),
            application.get("url", ""), application.get("created_at", ""),
            application.get("updated_at", ""), _configuration_payload(application),
        ),
    )
    return cursor.rowcount


def _upsert_service(
    connection: sqlite3.Connection,
    application_id: str,
    service: dict[str, Any],
    *,
    insert_only: bool = False,
) -> int:
    verb = "INSERT OR IGNORE" if insert_only else "INSERT OR REPLACE"
    cursor = connection.execute(
        f"""{verb} INTO application_services(
            application_id, name, image, required, container_port,
            service_type, health_path, payload
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            application_id, service["name"], service.get("image", ""),
            int(service.get("required", True)), int(service.get("container_port", 80)),
            service.get("service_type", "ClusterIP"), service.get("health_path", ""),
            _configuration_payload(service),
        ),
    )
    return cursor.rowcount


def replace_applications(applications: list[dict[str, Any]], path: Path | None = None) -> None:
    if _use_postgres(path):
        return _pg_backend().replace_applications(applications, path)
    initialize_schema(path)
    ids = {application["id"] for application in applications}
    with _connect(path) as connection:
        existing = {row["id"] for row in connection.execute("SELECT id FROM applications")}
        for removed_id in existing - ids:
            connection.execute("DELETE FROM applications WHERE id = ?", (removed_id,))
        for application in applications:
            _upsert_application(connection, application)
            connection.execute(
                "DELETE FROM application_services WHERE application_id = ?", (application["id"],)
            )
            for service in application.get("services", []):
                _upsert_service(connection, application["id"], service)


def list_applications(path: Path | None = None) -> list[dict[str, Any]]:
    if _use_postgres(path):
        return _pg_backend().list_applications(path)
    initialize_schema(path)
    with _connect(path) as connection:
        rows = connection.execute("SELECT payload FROM applications ORDER BY created_at").fetchall()
    return [_decode(row) for row in rows]


def _upsert_pipeline_run(
    connection: sqlite3.Connection, run: dict[str, Any], *, insert_only: bool = False
) -> int:
    conflict = "DO NOTHING" if insert_only else """DO UPDATE SET
        application_id=excluded.application_id, status=excluded.status,
        trigger_type=excluded.trigger_type, repository_url=excluded.repository_url,
        branch=excluded.branch, commit_sha=excluded.commit_sha,
        commit_author=excluded.commit_author, commit_message=excluded.commit_message,
        actor=excluded.actor, actor_id=excluded.actor_id,
        actor_role=excluded.actor_role, created_at=excluded.created_at,
        updated_at=excluded.updated_at, finished_at=excluded.finished_at,
        payload=excluded.payload"""
    cursor = connection.execute(
        f"""INSERT INTO pipeline_runs(
            id, application_id, status, trigger_type, repository_url, branch,
            commit_sha, commit_author, commit_message, actor, actor_id,
            actor_role, created_at, updated_at, finished_at, payload
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) {conflict}""",
        (
            run["id"], run["application_id"], run.get("status", "Running"),
            run.get("trigger_type", "Manual"), run.get("repository", run.get("repository_url", "")),
            run.get("branch", ""), run.get("commit_sha", ""), run.get("commit_author", ""),
            run.get("commit_message", ""), run.get("actor", "system"), run.get("actor_id"),
            run.get("actor_role", ""), run.get("created_at", ""), run.get("updated_at", ""),
            run.get("finished_at", ""), _payload(run),
        ),
    )
    return cursor.rowcount


def _upsert_pipeline_stage(
    connection: sqlite3.Connection,
    run_id: str,
    position: int,
    stage: dict[str, Any],
    *,
    insert_only: bool = False,
) -> int:
    verb = "INSERT OR IGNORE" if insert_only else "INSERT OR REPLACE"
    cursor = connection.execute(
        f"""{verb} INTO pipeline_stages(
            pipeline_run_id, name, position, status, output, started_at,
            finished_at, payload
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            run_id, stage["name"], position, stage.get("status", "Waiting"),
            stage.get("output", stage.get("message", "")), stage.get("started_at", ""),
            stage.get("finished_at", ""), _payload(stage),
        ),
    )
    return cursor.rowcount


def upsert_pipeline_run(run: dict[str, Any], path: Path | None = None) -> None:
    if _use_postgres(path):
        return _pg_backend().upsert_pipeline_run(run, path)
    initialize_schema(path)
    with _connect(path) as connection:
        _upsert_pipeline_run(connection, run)
        connection.execute("DELETE FROM pipeline_stages WHERE pipeline_run_id = ?", (run["id"],))
        for position, stage in enumerate(run.get("stages", [])):
            _upsert_pipeline_stage(connection, run["id"], position, stage)


def list_pipeline_runs(
    application_id: str | None = None, path: Path | None = None
) -> list[dict[str, Any]]:
    if _use_postgres(path):
        return _pg_backend().list_pipeline_runs(application_id, path)
    initialize_schema(path)
    query = "SELECT payload FROM pipeline_runs"
    params: tuple[Any, ...] = ()
    if application_id:
        query += " WHERE application_id = ?"
        params = (application_id,)
    query += " ORDER BY created_at DESC, id DESC"
    with _connect(path) as connection:
        rows = connection.execute(query, params).fetchall()
    return [_decode(row) for row in rows]


def get_pipeline_run(run_id: str, path: Path | None = None) -> dict[str, Any] | None:
    """Load a single pipeline run by id (used by the Celery worker)."""
    if _use_postgres(path):
        return _pg_backend().get_pipeline_run(run_id, path)
    initialize_schema(path)
    with _connect(path) as connection:
        row = connection.execute(
            "SELECT payload FROM pipeline_runs WHERE id = ?", (run_id,)
        ).fetchone()
    return _decode(row) if row else None


def mark_pipeline_running(
    run_id: str, worker_id: str, path: Path | None = None
) -> dict[str, Any] | None:
    """Transition a queued run to Running, recording worker/attempt metadata.

    Celery's broker already guarantees single delivery; this only mirrors the
    observability fields the old SQLite claim loop used to write.
    """
    if _use_postgres(path):
        return _pg_backend().mark_pipeline_running(run_id, worker_id, path)
    run = get_pipeline_run(run_id, path=path)
    if not run:
        return None
    if run.get("status") == "Running":
        return run
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    run["status"] = "Running"
    run["worker_id"] = worker_id
    run["claimed_at"] = now
    run["updated_at"] = now
    run["attempt"] = int(run.get("attempt", 0) or 0) + 1
    upsert_pipeline_run(run, path=path)
    return run


def claim_next_pipeline_run(
    worker_id: str, path: Path | None = None
) -> dict[str, Any] | None:
    """Atomically claim the oldest queued run for one standalone worker."""
    if _use_postgres(path):
        return _pg_backend().claim_next_pipeline_run(worker_id, path)
    if not worker_id.strip():
        raise ValueError("worker_id is required")
    initialize_schema(path)
    connection = _connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT payload FROM pipeline_runs WHERE status = 'Queued' "
            "ORDER BY created_at ASC, id ASC LIMIT 1"
        ).fetchone()
        if not row:
            connection.commit()
            return None
        run = _decode(row)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        run["status"] = "Running"
        run["worker_id"] = worker_id
        run["claimed_at"] = now
        run["updated_at"] = now
        run["attempt"] = int(run.get("attempt", 0) or 0) + 1
        _upsert_pipeline_run(connection, run)
        connection.commit()
        return run
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def has_active_pipeline(application_id: str, path: Path | None = None) -> bool:
    if _use_postgres(path):
        return _pg_backend().has_active_pipeline(application_id, path)
    initialize_schema(path)
    with _connect(path) as connection:
        row = connection.execute(
            "SELECT 1 FROM pipeline_runs WHERE application_id = ? AND status IN ('Queued', 'Running') LIMIT 1",
            (application_id,),
        ).fetchone()
    return row is not None


def reserve_pipeline_run(run: dict[str, Any], path: Path | None = None) -> None:
    """Atomically enforce per-application and platform-wide pipeline limits."""
    if _use_postgres(path):
        return _pg_backend().reserve_pipeline_run(run, path)
    initialize_schema(path)
    connection = _connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        active = connection.execute(
            "SELECT id FROM pipeline_runs WHERE application_id = ? "
            "AND status IN ('Queued', 'Running') LIMIT 1",
            (run["application_id"],),
        ).fetchone()
        if active:
            raise ValueError(
                f"Application đã có pipeline đang chạy hoặc chờ: {active['id']}"
            )
        try:
            max_concurrent = max(
                1, int(os.getenv("PIPELINE_MAX_CONCURRENT", "1"))
            )
        except ValueError:
            max_concurrent = 1
        active_count = connection.execute(
            "SELECT COUNT(*) FROM pipeline_runs "
            "WHERE status IN ('Queued', 'Running')"
        ).fetchone()[0]
        if active_count >= max_concurrent:
            raise ValueError(
                "Build Worker đang đạt giới hạn "
                f"{max_concurrent} pipeline đồng thời. Vui lòng thử lại sau."
            )
        _upsert_pipeline_run(connection, run)
        for position, stage in enumerate(run.get("stages", [])):
            _upsert_pipeline_stage(connection, run["id"], position, stage)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _upsert_job(
    connection: sqlite3.Connection, job: dict[str, Any], *, insert_only: bool = False
) -> int:
    verb = "INSERT OR IGNORE" if insert_only else "INSERT OR REPLACE"
    cursor = connection.execute(
        f"""{verb} INTO jobs(
            id, job_type, title, target, status, actor, actor_id, actor_role,
            created_at, updated_at, finished_at, payload
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            job["id"], job.get("type", ""), job.get("title", ""), job.get("target", ""),
            job.get("status", "Running"), job.get("actor", "system"), job.get("actor_id"),
            job.get("actor_role", ""), job.get("created_at", ""), job.get("updated_at", ""),
            job.get("finished_at", ""), _payload(job),
        ),
    )
    return cursor.rowcount


def replace_jobs(jobs: list[dict[str, Any]], path: Path | None = None) -> None:
    if _use_postgres(path):
        return _pg_backend().replace_jobs(jobs, path)
    initialize_schema(path)
    with _connect(path) as connection:
        for job in jobs:
            _upsert_job(connection, job)


def list_jobs(path: Path | None = None) -> list[dict[str, Any]]:
    if _use_postgres(path):
        return _pg_backend().list_jobs(path)
    initialize_schema(path)
    with _connect(path) as connection:
        rows = connection.execute("SELECT payload FROM jobs ORDER BY created_at DESC").fetchall()
    return [_decode(row) for row in rows]


def _upsert_audit(
    connection: sqlite3.Connection, audit: dict[str, Any], *, insert_only: bool = False
) -> int:
    verb = "INSERT OR IGNORE" if insert_only else "INSERT OR REPLACE"
    cursor = connection.execute(
        f"""{verb} INTO audit_logs(
            id, actor, actor_id, role, ip, action, target, result, created_at, payload
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            audit["id"], audit.get("user", audit.get("actor", "anonymous")),
            audit.get("user_id", audit.get("actor_id")), audit.get("role", ""),
            audit.get("ip", ""), audit.get("action", ""), audit.get("target", ""),
            audit.get("result", ""), audit.get("created_at", audit.get("time", "")),
            _payload(audit),
        ),
    )
    return cursor.rowcount


def replace_audit_logs(logs: list[dict[str, Any]], path: Path | None = None) -> None:
    if _use_postgres(path):
        return _pg_backend().replace_audit_logs(logs, path)
    initialize_schema(path)
    with _connect(path) as connection:
        for audit in logs:
            _upsert_audit(connection, audit)


def list_audit_logs(path: Path | None = None) -> list[dict[str, Any]]:
    if _use_postgres(path):
        return _pg_backend().list_audit_logs(path)
    initialize_schema(path)
    with _connect(path) as connection:
        rows = connection.execute("SELECT payload FROM audit_logs ORDER BY created_at DESC").fetchall()
    return [_decode(row) for row in rows]


def _upsert_deployment(connection: sqlite3.Connection, deployment: dict[str, Any]) -> None:
    safe_manifest = _manifest_without_secrets(str(deployment.get("manifest", "")))
    connection.execute(
        """INSERT INTO deployments(
            id, version, application_id, pipeline_run_id, previous_deployment_id,
            rollback_of_deployment_id, repository_url, branch, commit_sha,
            manifest, deployment_mode, actor, actor_id, namespace, url,
            verify_status, status, started_at, finished_at, payload
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            pipeline_run_id=excluded.pipeline_run_id,
            previous_deployment_id=excluded.previous_deployment_id,
            rollback_of_deployment_id=excluded.rollback_of_deployment_id,
            repository_url=excluded.repository_url, branch=excluded.branch,
            commit_sha=excluded.commit_sha, manifest=excluded.manifest,
            deployment_mode=excluded.deployment_mode, actor=excluded.actor,
            actor_id=excluded.actor_id, namespace=excluded.namespace,
            url=excluded.url, verify_status=excluded.verify_status,
            status=excluded.status, started_at=excluded.started_at,
            finished_at=excluded.finished_at, payload=excluded.payload""",
        (
            deployment["id"], deployment["version"], deployment["application_id"],
            deployment.get("pipeline_run_id"), deployment.get("previous_deployment_id"),
            deployment.get("rollback_of_deployment_id"), deployment.get("repository", ""),
            deployment.get("branch", ""), deployment.get("commit_sha", ""),
            safe_manifest, deployment.get("deployment_mode", "Production"),
            deployment.get("actor", "system"), deployment.get("actor_id"),
            deployment.get("namespace", ""), deployment.get("url", ""),
            deployment.get("verify_status", "Pending"), deployment.get("status", "Pending"),
            deployment.get("started_at", ""), deployment.get("finished_at", ""),
            _deployment_payload(deployment),
        ),
    )


def _replace_deployment_services(
    connection: sqlite3.Connection, deployment: dict[str, Any]
) -> None:
    connection.execute(
        "DELETE FROM deployment_services WHERE deployment_id = ?", (deployment["id"],)
    )
    for service in deployment.get("services", []):
        connection.execute(
            """INSERT INTO deployment_services(
                deployment_id, service_name, image_tag, image_digest, required, payload
            ) VALUES (?, ?, ?, ?, ?, ?)""",
            (
                deployment["id"], service["service"], service.get("image", ""),
                service.get("digest", ""), int(service.get("required", True)),
                _payload(service),
            ),
        )


def create_deployment_record(
    deployment: dict[str, Any], path: Path | None = None
) -> dict[str, Any]:
    """Allocate an application-scoped version and insert atomically."""
    if _use_postgres(path):
        return _pg_backend().create_deployment_record(deployment, path)
    initialize_schema(path)
    connection = _connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        version = connection.execute(
            "SELECT COALESCE(MAX(version), 0) + 1 FROM deployments WHERE application_id = ?",
            (deployment["application_id"],),
        ).fetchone()[0]
        record = {
            **deployment,
            "id": deployment.get("id") or f"deployment-{uuid.uuid4()}",
            "version": version,
        }
        _upsert_deployment(connection, record)
        _replace_deployment_services(connection, record)
        connection.commit()
        return record
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def update_deployment_record(
    deployment: dict[str, Any], path: Path | None = None
) -> dict[str, Any]:
    if _use_postgres(path):
        return _pg_backend().update_deployment_record(deployment, path)
    initialize_schema(path)
    with _connect(path) as connection:
        _upsert_deployment(connection, deployment)
        _replace_deployment_services(connection, deployment)
    return deployment


def get_deployment(deployment_id: str, path: Path | None = None) -> dict[str, Any] | None:
    if _use_postgres(path):
        return _pg_backend().get_deployment(deployment_id, path)
    initialize_schema(path)
    with _connect(path) as connection:
        row = connection.execute(
            "SELECT payload FROM deployments WHERE id = ?", (deployment_id,)
        ).fetchone()
    return _decode(row) if row else None


def list_deployments(
    application_id: str | None = None, path: Path | None = None
) -> list[dict[str, Any]]:
    if _use_postgres(path):
        return _pg_backend().list_deployments(application_id, path)
    initialize_schema(path)
    query = "SELECT payload FROM deployments"
    params: tuple[Any, ...] = ()
    if application_id:
        query += " WHERE application_id = ?"
        params = (application_id,)
    query += " ORDER BY version DESC"
    with _connect(path) as connection:
        rows = connection.execute(query, params).fetchall()
    return [_decode(row) for row in rows]


def claim_webhook_delivery(
    delivery_id: str,
    application_id: str,
    event_type: str,
    commit_sha: str,
    metadata: dict[str, Any],
    path: Path | None = None,
) -> bool:
    if _use_postgres(path):
        return _pg_backend().claim_webhook_delivery(
            delivery_id, application_id, event_type, commit_sha, metadata, path
        )
    initialize_schema(path)
    with _connect(path) as connection:
        cursor = connection.execute(
            """INSERT OR IGNORE INTO webhook_deliveries(
                delivery_id, application_id, event_type, commit_sha, payload
            ) VALUES (?, ?, ?, ?, ?)""",
            (delivery_id, application_id, event_type, commit_sha, _payload(metadata)),
        )
    return cursor.rowcount == 1
