from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()


def assert_database_schema_current() -> None:
    """Fail fast when a PostgreSQL runtime has pending migrations."""
    if db.engine.dialect.name != "postgresql":
        return

    from pathlib import Path

    from alembic.config import Config as AlembicConfig
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    config = AlembicConfig(str(Path(__file__).resolve().parents[1] / "alembic.ini"))
    expected = ScriptDirectory.from_config(config).get_current_head()
    with db.engine.connect() as connection:
        current = MigrationContext.configure(connection).get_current_revision()
    if current != expected:
        raise RuntimeError(
            "Database migration is not current "
            f"(current={current or 'none'}, expected={expected}). Run "
            "'python scripts/manage_database.py upgrade' before starting Platform."
        )
