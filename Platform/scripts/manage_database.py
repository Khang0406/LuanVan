"""Safe Alembic lifecycle commands for the Platform database.

Examples:
    python scripts/manage_database.py upgrade
    python scripts/manage_database.py current
    python scripts/manage_database.py check
    python scripts/manage_database.py adopt-existing
    python scripts/manage_database.py downgrade <revision>
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from dotenv import load_dotenv
from sqlalchemy import create_engine, inspect


BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))
load_dotenv(BASE_DIR / ".env")
BASELINE = "0001_platform_baseline"

BASELINE_EXPECTED_COLUMNS = {
    "delivery_migrations": {"name", "applied_at"},
    "applications": {"id", "name", "namespace", "source_type", "payload"},
    "application_services": {"application_id", "name", "image", "payload"},
    "pipeline_runs": {"id", "application_id", "status", "payload"},
    "pipeline_stages": {"pipeline_run_id", "name", "position", "payload"},
    "deployments": {"id", "version", "application_id", "deployment_mode", "payload"},
    "deployment_services": {"deployment_id", "service_name", "image_tag", "payload"},
    "jobs": {"id", "job_type", "title", "status", "payload"},
    "audit_logs": {"id", "action", "target", "result", "payload"},
    "webhook_deliveries": {"delivery_id", "application_id", "event_type", "payload"},
    "users": {"id", "username", "password_hash", "role", "created_at"},
}

EXPECTED_COLUMNS = {
    **BASELINE_EXPECTED_COLUMNS,
    "users": BASELINE_EXPECTED_COLUMNS["users"]
    | {"email", "status", "email_verified_at", "status_changed_at"},
    "email_verification_tokens": {
        "id", "user_id", "token_hash", "expires_at", "used_at", "created_at", "request_ip"
    },
}


def alembic_config() -> Config:
    return Config(str(BASE_DIR / "alembic.ini"))


def database_url() -> str:
    value = os.getenv("DATABASE_URL", "").strip()
    if not value:
        raise SystemExit("DATABASE_URL chưa được cấu hình; từ chối thay đổi database.")
    return value


def schema_problems(engine, expected_columns=EXPECTED_COLUMNS) -> list[str]:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    problems: list[str] = []
    for table, expected in expected_columns.items():
        if table not in tables:
            problems.append(f"thiếu bảng {table}")
            continue
        actual = {column["name"] for column in inspector.get_columns(table)}
        missing = expected - actual
        if missing:
            problems.append(f"bảng {table} thiếu cột: {', '.join(sorted(missing))}")
    return problems


def revisions(config: Config, engine) -> tuple[str | None, str]:
    with engine.connect() as connection:
        current = MigrationContext.configure(connection).get_current_revision()
    head = ScriptDirectory.from_config(config).get_current_head()
    return current, head


def run_check(config: Config, engine) -> int:
    problems = schema_problems(engine)
    current, head = revisions(config, engine)
    if problems:
        for problem in problems:
            print(f"ERROR: {problem}")
    if current != head:
        problems.append("migration revision chưa ở head")
        print(f"ERROR: revision hiện tại={current or 'none'}, head={head}")
    if problems:
        return 1
    print(f"OK: schema đầy đủ, revision={current}")
    return 0


def adopt_existing(config: Config, engine) -> None:
    problems = schema_problems(engine, BASELINE_EXPECTED_COLUMNS)
    if problems:
        details = "; ".join(problems)
        raise SystemExit(f"Schema hiện hữu không khớp baseline: {details}")
    current, _head = revisions(config, engine)
    if current:
        raise SystemExit(f"Database đã được Alembic quản lý tại revision {current}.")
    command.stamp(config, BASELINE)
    print(f"Đã ghi nhận schema hiện hữu tại revision {BASELINE}; không sửa dữ liệu.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("upgrade", "downgrade", "current", "history", "check", "adopt-existing")
    )
    parser.add_argument("revision", nargs="?", help="Revision cho downgrade (ví dụ: -1)")
    args = parser.parse_args()
    url = database_url()
    config = alembic_config()
    # Alembic interpolation treats percent signs specially.
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    engine = create_engine(url, future=True)

    if args.action == "upgrade":
        command.upgrade(config, "head")
        return run_check(config, engine)
    if args.action == "downgrade":
        if not args.revision:
            parser.error("downgrade yêu cầu revision; không tự động chọn mốc dữ liệu")
        command.downgrade(config, args.revision)
        return 0
    if args.action == "current":
        command.current(config, verbose=True)
        return 0
    if args.action == "history":
        command.history(config, verbose=True)
        return 0
    if args.action == "adopt-existing":
        adopt_existing(config, engine)
        print("Tiếp theo chạy 'python scripts/manage_database.py upgrade'.")
        return 0
    return run_check(config, engine)


if __name__ == "__main__":
    raise SystemExit(main())
