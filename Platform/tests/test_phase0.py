import json
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from app.json_store import is_list_of_dicts, mask_secrets, normalize_status, read_json, update_json, write_json
from app.modules.applications.service import _bounded_int
from app.modules.pipeline import engine


class JsonStoreTests(unittest.TestCase):
    def test_atomic_write_and_backup_recovery(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            write_json(path, [{"version": 1}])
            write_json(path, [{"version": 2}])
            self.assertEqual(json.loads(path.read_text())[0]["version"], 2)
            path.write_text("{broken", encoding="utf-8")
            self.assertEqual(read_json(path, [], is_list_of_dicts), [{"version": 1}])

    def test_validation_falls_back_to_default(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            path.write_text('{"not": "a list"}', encoding="utf-8")
            self.assertEqual(read_json(path, [], is_list_of_dicts), [])

    def test_concurrent_updates_do_not_lose_data(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "counter.json"
            write_json(path, {"count": 0})

            def increment():
                def update(payload):
                    payload["count"] += 1
                    return payload
                update_json(path, {"count": 0}, update)

            threads = [threading.Thread(target=increment) for _ in range(20)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(read_json(path, {})["count"], 20)

    def test_masking_and_status_normalization(self):
        masked = mask_secrets({"token": "abc", "log": "password=secret --password xyz"})
        self.assertEqual(masked["token"], "***")
        self.assertEqual(normalize_status("done"), "Success")
        self.assertEqual(normalize_status("in_progress"), "Running")

    def test_bounded_integer_validation(self):
        self.assertEqual(_bounded_int("30000", "NodePort", 30000, 30000, 32767), 30000)
        with self.assertRaises(ValueError):
            _bounded_int("70000", "Port", 80, 1, 65535)


class PipelineRecoveryTests(unittest.TestCase):
    def test_running_pipeline_is_closed_after_restart(self):
        with TemporaryDirectory() as directory:
            original = engine.PIPELINE_FILE
            engine.PIPELINE_FILE = Path(directory) / "pipeline_runs.json"
            try:
                write_json(engine.PIPELINE_FILE, [{
                    "id": "run-1", "status": "Running", "updated_at": "",
                    "stages": [
                        {"name": "BUILD", "status": "Running", "message": "", "finished_at": ""},
                        {"name": "DEPLOY", "status": "Waiting", "message": "", "finished_at": ""},
                    ],
                }])
                self.assertEqual(engine.recover_interrupted_pipeline_runs(), 1)
                run = read_json(engine.PIPELINE_FILE, [])[0]
                self.assertEqual(run["status"], "Interrupted")
                self.assertEqual(run["stages"][0]["status"], "Interrupted")
                self.assertEqual(run["stages"][1]["status"], "Skipped")
            finally:
                engine.PIPELINE_FILE = original
