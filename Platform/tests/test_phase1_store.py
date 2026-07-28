import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.delivery_store import (
    list_applications,
    list_pipeline_runs,
    migrate_json_state,
    replace_applications,
)


class DeliveryMigrationTests(unittest.TestCase):
    def _write(self, path: Path, payload):
        path.write_text(json.dumps(payload), encoding="utf-8")

    def test_json_migration_is_idempotent_and_normalized(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "platform.db"
            applications_file = root / "applications.json"
            pipeline_file = root / "pipeline_runs.json"
            jobs_file = root / "jobs.json"
            audit_file = root / "audit_logs.json"
            backup_dir = root / "backup"

            self._write(applications_file, [{
                "id": "map", "name": "map", "owner": "khang",
                "namespace": "map", "source_type": "github",
                "github_url": "https://github.com/example/map.git",
                "services": [{"name": "web", "image": "example/map:abc", "container_port": 80}],
                "status": "Running", "created_at": "2026-01-01", "updated_at": "2026-01-01",
            }])
            self._write(pipeline_file, [{
                "id": "run-map-1", "application_id": "map", "status": "Success",
                "created_at": "2026-01-01", "updated_at": "2026-01-01",
                "stages": [{"name": "SOURCE", "status": "Done", "message": "cloned"}],
            }])
            self._write(jobs_file, [])
            self._write(audit_file, [])

            first = migrate_json_state(
                applications_file, pipeline_file, jobs_file, audit_file,
                path=database, backup_dir=backup_dir,
            )
            second = migrate_json_state(
                applications_file, pipeline_file, jobs_file, audit_file,
                path=database, backup_dir=backup_dir,
            )

            self.assertEqual(first["applications"], 1)
            self.assertEqual(first["services"], 1)
            self.assertEqual(first["pipeline_runs"], 1)
            self.assertEqual(first["pipeline_stages"], 1)
            self.assertEqual(second["applications"], 0)
            self.assertTrue((backup_dir / "applications.json").exists())

            with sqlite3.connect(database) as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM applications").fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM application_services").fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM pipeline_runs").fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM pipeline_stages").fetchone()[0], 1)

    def test_database_is_authoritative_after_restart(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "platform.db"
            applications_file = root / "applications.json"
            pipeline_file = root / "pipeline_runs.json"
            jobs_file = root / "jobs.json"
            audit_file = root / "audit_logs.json"
            app = {
                "id": "demo-nginx", "name": "demo-nginx", "owner": "demo",
                "namespace": "demo-nginx", "source_type": "docker",
                "services": [{"name": "web", "image": "nginx:stable-alpine"}],
                "status": "Running", "created_at": "2026-01-01", "updated_at": "2026-01-01",
            }
            self._write(applications_file, [app])
            self._write(pipeline_file, [])
            self._write(jobs_file, [])
            self._write(audit_file, [])
            migrate_json_state(
                applications_file, pipeline_file, jobs_file, audit_file, path=database
            )

            app["status"] = "Ready"
            replace_applications([app], path=database)
            self._write(applications_file, [])

            self.assertEqual(list_applications(path=database)[0]["status"], "Ready")
            self.assertEqual(list_pipeline_runs(path=database), [])


if __name__ == "__main__":
    unittest.main()
