import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


def _server(server_id: str, name: str) -> dict:
    return {
        "id": server_id,
        "name": name,
        "ip": "192.0.2.10",
        "role": "Worker",
        "status": "Ready",
        "ssh_user": "platform",
        "ssh_port": 22,
        "ssh_key_path": "~/.ssh/id_ed25519",
        "cluster_id": "cluster-test",
    }


class ServerDeleteServiceTests(unittest.TestCase):
    def test_delete_removes_only_target_and_generated_inventory(self):
        from app.modules.servers import service

        with TemporaryDirectory() as directory:
            data_dir = Path(directory)
            servers_file = data_dir / "servers.json"
            inventory_file = data_dir / "ansible-worker-one.ini"

            with patch.object(service, "DATA_DIR", data_dir), patch.object(
                service, "SERVERS_FILE", servers_file
            ):
                service.save_servers([
                    _server("worker-one", "Worker one"),
                    _server("worker-two", "Worker two"),
                ])
                inventory_file.write_text("[target]\nworker-one\n", encoding="utf-8")

                removed = service.delete_server("worker-one")

                self.assertEqual(removed["name"], "Worker one")
                self.assertEqual(
                    [server["id"] for server in service.load_servers()],
                    ["worker-two"],
                )
                self.assertFalse(inventory_file.exists())

    def test_delete_unknown_server_does_not_change_inventory(self):
        from app.modules.servers import service

        with TemporaryDirectory() as directory:
            data_dir = Path(directory)
            servers_file = data_dir / "servers.json"

            with patch.object(service, "DATA_DIR", data_dir), patch.object(
                service, "SERVERS_FILE", servers_file
            ):
                expected = [_server("worker-one", "Worker one")]
                service.save_servers(expected)

                self.assertIsNone(service.delete_server("missing"))
                self.assertEqual(service.load_servers(), expected)


class ServerDeleteRouteTests(unittest.TestCase):
    def _create_application(self, root: Path):
        from app import create_app

        database = root / "app.db"

        class TestConfig:
            TESTING = True
            SECRET_KEY = "server-delete-test"
            SQLALCHEMY_DATABASE_URI = f"sqlite:///{database}"
            SQLALCHEMY_TRACK_MODIFICATIONS = False

        environment = {
            "DELIVERY_DATABASE_PATH": str(database),
            "PLATFORM_DATA_DIR": str(root),
        }
        with patch.dict(os.environ, environment):
            return create_app(TestConfig)

    @staticmethod
    def _login(client, user_id: int, include_csrf: bool = True) -> None:
        with client.session_transaction() as session:
            session["_user_id"] = str(user_id)
            session["_fresh"] = True
            if include_csrf:
                session["_csrf_token"] = "server-delete-csrf"

    def test_admin_can_delete_server_and_action_is_audited(self):
        from app.models import User
        from app.modules.servers import service

        with TemporaryDirectory() as directory:
            root = Path(directory)
            application = self._create_application(root)
            with application.app_context():
                admin_id = User.query.filter_by(username="admin").one().id

            servers_file = root / "servers.json"
            with patch.object(service, "DATA_DIR", root), patch.object(
                service, "SERVERS_FILE", servers_file
            ), patch("app.ui.routes.record_audit") as record_audit:
                service.save_servers([_server("worker-one", "Worker one")])
                client = application.test_client()
                self._login(client, admin_id)

                response = client.post(
                    "/servers/worker-one/delete",
                    data={"csrf_token": "server-delete-csrf"},
                )

                self.assertEqual(response.status_code, 302)
                self.assertTrue(response.location.endswith("/servers"))
                self.assertEqual(service.load_servers(), [])
                record_audit.assert_called_once()
                self.assertEqual(record_audit.call_args.args[0], "SERVER_DELETE")
                self.assertNotIn(
                    "ssh_key_path",
                    record_audit.call_args.kwargs["metadata"],
                )

    def test_admin_pages_render_delete_control(self):
        from app.models import User
        from app.modules.servers import service

        with TemporaryDirectory() as directory:
            root = Path(directory)
            application = self._create_application(root)
            with application.app_context():
                admin_id = User.query.filter_by(username="admin").one().id

            servers_file = root / "servers.json"
            with patch.object(service, "DATA_DIR", root), patch.object(
                service, "SERVERS_FILE", servers_file
            ):
                service.save_servers([_server("worker-one", "Worker one")])
                client = application.test_client()
                self._login(client, admin_id)

                list_response = client.get("/servers")
                detail_response = client.get("/servers/worker-one")

                self.assertEqual(list_response.status_code, 200)
                self.assertEqual(detail_response.status_code, 200)
                delete_path = b"/servers/worker-one/delete"
                self.assertIn(delete_path, list_response.data)
                self.assertIn(delete_path, detail_response.data)

    def test_developer_cannot_delete_server(self):
        from app.models import User
        from app.modules.servers import service

        with TemporaryDirectory() as directory:
            root = Path(directory)
            application = self._create_application(root)
            with application.app_context():
                developer_id = User.query.filter_by(username="dev").one().id

            servers_file = root / "servers.json"
            with patch.object(service, "DATA_DIR", root), patch.object(
                service, "SERVERS_FILE", servers_file
            ), patch("app.modules.auth.routes.record_audit"):
                expected = [_server("worker-one", "Worker one")]
                service.save_servers(expected)
                client = application.test_client()
                self._login(client, developer_id)

                response = client.post(
                    "/servers/worker-one/delete",
                    data={"csrf_token": "server-delete-csrf"},
                )

                self.assertEqual(response.status_code, 302)
                self.assertTrue(response.location.endswith("/dashboard"))
                self.assertEqual(service.load_servers(), expected)

    def test_delete_requires_csrf_token(self):
        from app.models import User
        from app.modules.servers import service

        with TemporaryDirectory() as directory:
            root = Path(directory)
            application = self._create_application(root)
            with application.app_context():
                admin_id = User.query.filter_by(username="admin").one().id

            servers_file = root / "servers.json"
            with patch.object(service, "DATA_DIR", root), patch.object(
                service, "SERVERS_FILE", servers_file
            ):
                expected = [_server("worker-one", "Worker one")]
                service.save_servers(expected)
                client = application.test_client()
                self._login(client, admin_id, include_csrf=False)

                response = client.post("/servers/worker-one/delete")

                self.assertEqual(response.status_code, 400)
                self.assertEqual(service.load_servers(), expected)


if __name__ == "__main__":
    unittest.main()
