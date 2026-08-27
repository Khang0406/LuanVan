"""One-time migration: SQLite (``instance/app.db``) → PostgreSQL.

Usage (with ``DATABASE_URL`` set, or the default local Docker Compose):

    python scripts/migrate_sqlite_to_postgres.py

The script is destructive on the *target* PostgreSQL schema (drops and
recreates ``public``) because the container may already hold test data. The
source SQLite file is never modified.

Data migrated:
- users (id, username, password_hash, role, created_at) — ids preserved so
  ``applications.user_id`` references stay valid.
- applications + application_services
- pipeline_runs + pipeline_stages
- deployments + deployment_services
- jobs
- audit_logs
- delivery_migrations marker (prevents JSON re-import on next boot)
"""

from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE_DIR))

SQLITE_PATH = BASE_DIR / "instance" / "app.db"
DATABASE_URL = os.environ.get(
    "DATABASE_URL",
    "postgresql+psycopg2://platform:platform@localhost:5432/platform",
)


def _parse_created_at(value) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def main() -> None:
    os.environ["DATABASE_URL"] = DATABASE_URL

    if not SQLITE_PATH.exists():
        raise SystemExit(f"Không tìm thấy SQLite DB: {SQLITE_PATH}")

    # --- 1. Read every record from SQLite -----------------------------------
    conn = sqlite3.connect(SQLITE_PATH)
    conn.row_factory = sqlite3.Row
    users = [
        dict(row)
        for row in conn.execute(
            "SELECT id, username, password_hash, role, created_at FROM users ORDER BY id"
        ).fetchall()
    ]
    conn.close()

    from app.delivery_store import (
        list_applications,
        list_audit_logs,
        list_deployments,
        list_jobs,
        list_pipeline_runs,
    )

    apps = list_applications(path=SQLITE_PATH)
    runs = list_pipeline_runs(path=SQLITE_PATH)
    deployments = list_deployments(path=SQLITE_PATH)
    jobs = list_jobs(path=SQLITE_PATH)
    audits = list_audit_logs(path=SQLITE_PATH)

    # --- 2. Recreate PostgreSQL schema (clean slate) ------------------------
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import create_engine, text

    engine = create_engine(DATABASE_URL, future=True)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    alembic_config = Config(str(BASE_DIR / "alembic.ini"))
    alembic_config.set_main_option(
        "sqlalchemy.url", DATABASE_URL.replace("%", "%%")
    )
    command.upgrade(alembic_config, "head")

    # --- 3. Migrate delivery data -------------------------------------------
    from app.delivery_store_pg import (
        replace_applications,
        replace_audit_logs,
        replace_jobs,
        update_deployment_record,
        upsert_pipeline_run,
    )

    replace_applications(apps)
    for run in runs:
        upsert_pipeline_run(run)
    for deployment in deployments:
        update_deployment_record(deployment)
    replace_jobs(jobs)
    replace_audit_logs(audits)

    # --- 4. Create users table (via SQLAlchemy) and migrate users -----------
    from flask import Flask

    from app.db import db
    from app.models import User

    flask_app = Flask(__name__)
    flask_app.config["SQLALCHEMY_DATABASE_URI"] = DATABASE_URL
    flask_app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
    db.init_app(flask_app)
    with flask_app.app_context():
        if User.query.count() == 0:
            for user in users:
                db.session.add(
                    User(
                        id=user["id"],
                        username=user["username"],
                        password_hash=user["password_hash"],
                        role=user["role"],
                        created_at=_parse_created_at(user["created_at"]),
                    )
                )
            db.session.commit()
        # Keep the SERIAL sequence ahead of the imported explicit ids.
        db.session.execute(
            text(
                "SELECT setval(pg_get_serial_sequence('users','id'), "
                "(SELECT COALESCE(MAX(id), 1) FROM users))"
            )
        )
        db.session.commit()

    # --- 5. Mark delivery migration complete --------------------------------
    from app.delivery_store import MIGRATION_NAME

    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO delivery_migrations(name) VALUES (:name) ON CONFLICT DO NOTHING"),
            {"name": MIGRATION_NAME},
        )

    print("✅ Migration SQLite → PostgreSQL hoàn tất:")
    print(f"   applications   : {len(apps)}")
    print(f"   pipeline_runs  : {len(runs)}")
    print(f"   deployments    : {len(deployments)}")
    print(f"   jobs           : {len(jobs)}")
    print(f"   audit_logs     : {len(audits)}")
    print(f"   users          : {len(users)}")


if __name__ == "__main__":
    main()
