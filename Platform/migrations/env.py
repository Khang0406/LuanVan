from __future__ import annotations

import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from dotenv import load_dotenv
from sqlalchemy import engine_from_config, pool

from app.db import db
from app import models  # noqa: F401 - registers ORM models in db.metadata


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

load_dotenv(Path(__file__).resolve().parents[1] / ".env")

database_url = os.getenv("DATABASE_URL", "").strip()
if database_url:
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))

target_metadata = db.metadata

# These tables are deliberately maintained through SQLAlchemy Core rather than
# ORM models. The baseline owns their creation, but autogenerate must never
# interpret their absence from db.metadata as an instruction to drop them.
CORE_TABLES = {
    "delivery_migrations",
    "applications",
    "application_services",
    "pipeline_runs",
    "pipeline_stages",
    "deployments",
    "deployment_services",
    "jobs",
    "audit_logs",
    "webhook_deliveries",
}


def include_object(obj, name, type_, reflected, compare_to):
    if type_ == "table" and reflected and compare_to is None and name in CORE_TABLES:
        return False
    return True


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
