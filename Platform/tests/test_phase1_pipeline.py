import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.delivery_store import replace_applications, reserve_pipeline_run
from app.modules.pipeline import build, engine
from app import registry_credentials


class SourceAndBuildValidationTests(unittest.TestCase):
    def test_github_url_branch_ref_validation(self):
        self.assertEqual(
            build.validate_repository_url("https://github.com/openai/example.git"),
            "https://github.com/openai/example.git",
        )
        self.assertEqual(build.validate_git_ref("feature/delivery-v1"), "feature/delivery-v1")
        for invalid in (
            "https://gitlab.com/a/b.git",
            "https://github.com/a/b.git --upload-pack=bad",
            "https://github.com/a/../b",
        ):
            with self.assertRaises(ValueError):
                build.validate_repository_url(invalid)
        for invalid in ("../main", "-branch", "main..evil", ""):
            with self.assertRaises(ValueError):
                build.validate_git_ref(invalid)

    def test_build_context_and_dockerfile_are_repository_relative(self):
        self.assertEqual(build.validate_repository_path("services/web", "context"), "services/web")
        self.assertEqual(build.validate_repository_path("docker/Dockerfile", "Dockerfile"), "docker/Dockerfile")
        for invalid in ("../Dockerfile", "/tmp/Dockerfile", "a/../../b"):
            with self.assertRaises(ValueError):
                build.validate_repository_path(invalid, "path")

    def test_clone_extracts_real_commit_sha_after_clone(self):
        sha = "a" * 40
        with TemporaryDirectory() as directory, patch.object(
            build, "_run_command", side_effect=[(True, "cloned"), (True, sha)]
        ) as run:
            success, output, commit = build.clone_repository(
                "https://github.com/example/project.git", Path(directory), "main"
            )
        self.assertTrue(success)
        self.assertEqual(output, "cloned")
        self.assertEqual(commit, sha)
        self.assertEqual(run.call_args_list[0].args[0][:5], ["git", "clone", "--depth", "1", "--branch"])
        self.assertEqual(run.call_args_list[1].args[0], ["git", "rev-parse", "HEAD"])

    def test_image_tag_is_immutable_and_per_service(self):
        application = {"id": "map", "owner": "team"}
        registry = {"registry": "docker.io", "username": "khang"}
        sha = "0123456789abcdef0123456789abcdef01234567"
        self.assertEqual(
            build._image_tag(application, "web", registry, sha),
            f"khang/map-web:{sha}",
        )
        self.assertEqual(
            build._image_tag(application, "mysql", registry, sha),
            f"khang/map-mysql:{sha}",
        )
        with self.assertRaises(ValueError):
            build._image_tag(application, "web", registry, "latest")


class RegistrySafetyTests(unittest.TestCase):
    def test_platform_credential_is_inherited_before_legacy_app_credential(self):
        platform_secret = "platform-token-not-for-logs"
        legacy_secret = "old-invalid-token"
        with TemporaryDirectory() as directory, patch.object(
            registry_credentials,
            "CREDENTIALS_FILE",
            Path(directory) / "registry_credentials.json",
        ):
            registry_credentials.save_registry_credential(
                registry_credentials.PLATFORM_DEFAULT_REFERENCE,
                "platform-org",
                platform_secret,
                "docker.io",
            )
            registry_credentials.save_registry_credential(
                "user:1",
                "legacy-user",
                legacy_secret,
                "docker.io",
            )
            application = {
                "id": "map",
                "user_id": 1,
                # Missing inherit_platform is treated as inherit=true so
                # existing applications migrate without a destructive rewrite.
                "registry": {
                    "credential_ref": "user:1",
                    "username": "legacy-user",
                },
            }
            effective = build._get_registry_config(application)
            status = registry_credentials.registry_credential_status(application)

        self.assertEqual(effective["username"], "platform-org")
        self.assertEqual(effective["password"], platform_secret)
        self.assertEqual(effective["credential_source"], "platform")
        self.assertNotEqual(effective["password"], legacy_secret)
        self.assertNotIn("credential", status)
        self.assertNotIn(platform_secret, str(status))

    def test_explicit_application_override_remains_supported(self):
        with TemporaryDirectory() as directory, patch.object(
            registry_credentials,
            "CREDENTIALS_FILE",
            Path(directory) / "registry_credentials.json",
        ):
            registry_credentials.save_registry_credential(
                registry_credentials.PLATFORM_DEFAULT_REFERENCE,
                "platform-org",
                "platform-token",
            )
            registry_credentials.save_registry_credential(
                "application:special",
                "special-org",
                "special-token",
            )
            effective = build._get_registry_config(
                {
                    "id": "special",
                    "registry": {
                        "inherit_platform": False,
                        "credential_ref": "application:special",
                    },
                }
            )
        self.assertEqual(effective["username"], "special-org")
        self.assertEqual(effective["password"], "special-token")
        self.assertEqual(effective["credential_source"], "application")

    def test_docker_login_uses_stdin_and_never_password_argument(self):
        secret = "super-secret-token"
        config = {
            "registry": "docker.io",
            "username": "khang",
            "password": secret,
            "token": "",
        }
        calls = []

        def fake_run(args, **kwargs):
            calls.append((args, kwargs))
            return True, "digest: sha256:" + "a" * 64

        with patch.object(build, "_docker_available", return_value=True), patch.object(
            build, "_run_command", side_effect=fake_run
        ):
            success, _ = build.push_docker_image("khang/map-web:abc1234", config)

        self.assertTrue(success)
        login_args, login_kwargs = calls[0]
        self.assertIn("--password-stdin", login_args)
        self.assertNotIn("--password", login_args)
        self.assertNotIn(secret, login_args)
        self.assertEqual(login_kwargs["stdin"], secret)
        self.assertFalse(any(secret in " ".join(call[0]) for call in calls))

    def test_container_test_runs_and_always_cleans_up(self):
        application = {"id": "demo", "test_startup_delay_seconds": 0}
        service = {"name": "web", "image": "nginx:stable-alpine", "container_port": 80}
        with patch.object(build, "_docker_available", return_value=True), patch.object(
            build, "_run_command",
            side_effect=[(True, "container-id"), (True, "running"), (True, "removed")],
        ) as run:
            success, message = build.test_docker_image(
                application, service, service["image"]
            )
        self.assertTrue(success)
        self.assertEqual(message, "running")
        self.assertEqual(run.call_args_list[-1].args[0][:3], ["docker", "rm", "-f"])

    def test_health_check_uses_isolated_probe_not_app_binaries(self):
        application = {"id": "map", "test_startup_delay_seconds": 0}
        service = {
            "name": "web",
            "container_port": 80,
            "health_path": "/frontend/index.php",
            "health_timeout_seconds": 5,
        }
        with patch.object(build, "_docker_available", return_value=True), patch.object(
            build,
            "_run_command",
            side_effect=[
                (True, "container-id"),
                (True, "running"),
                (True, "healthy response"),
                (True, "removed"),
            ],
        ) as run:
            success, message = build.test_docker_image(
                application,
                service,
                "platform/map-web:abcdef0",
            )
        self.assertTrue(success)
        self.assertIn("health /frontend/index.php passed", message)
        health_args = run.call_args_list[2].args[0]
        self.assertEqual(health_args[:3], ["docker", "run", "--rm"])
        self.assertIn("--network", health_args)
        self.assertIn("curlimages/curl:8.10.1", health_args)
        self.assertFalse(any("wget" in item for item in health_args))


class PipelineStateTests(unittest.TestCase):
    def _run(self):
        return {
            "id": "run-map-test",
            "application_id": "map",
            "application_name": "map",
            "status": "Running",
            "created_at": "2026-01-01",
            "updated_at": "2026-01-01",
            "stages": [
                {"name": name, "status": "Waiting", "message": "", "started_at": "", "finished_at": ""}
                for name in engine.PIPELINE_STAGES
            ],
        }

    def test_pipeline_stops_when_test_fails(self):
        application = {
            "id": "map", "name": "map", "source_type": "github",
            "deployment_mode": "production", "development_fallback_enabled": False,
            "services": [{"name": "web", "image": ""}],
        }
        run = self._run()

        def failed_build(_application, pipeline_run):
            for stage in pipeline_run["stages"]:
                if stage["name"] == "SOURCE":
                    stage["status"] = "Done"
                elif stage["name"] == "BUILD":
                    stage["status"] = "Done"
                elif stage["name"] == "TEST":
                    stage["status"] = "Failed"
                    stage["message"] = "container exited"
            return pipeline_run

        with patch.object(engine, "find_application", return_value=application), \
                patch.object(engine, "build_from_github", side_effect=failed_build), \
                patch.object(engine, "_save_pipeline_run"), \
                patch.object(engine, "save_application"), \
                patch.object(engine, "add_activity"), \
                patch.object(engine, "deploy_application") as deploy:
            engine._run_pipeline(run)

        self.assertEqual(run["status"], "Failed")
        self.assertEqual(
            next(stage for stage in run["stages"] if stage["name"] == "DEPLOY")["status"],
            "Skipped",
        )
        deploy.assert_not_called()

    def test_atomic_duplicate_pipeline_reservation(self):
        with TemporaryDirectory() as directory:
            database = Path(directory) / "platform.db"
            application = {
                "id": "map", "name": "map", "owner": "khang", "namespace": "map",
                "source_type": "github", "services": [], "status": "Draft",
            }
            replace_applications([application], path=database)
            run = self._run()
            run["status"] = "Queued"
            reserve_pipeline_run(run, path=database)
            duplicate = self._run()
            duplicate["id"] = "run-map-test-2"
            duplicate["status"] = "Queued"
            with self.assertRaisesRegex(ValueError, "đang chạy hoặc chờ"):
                reserve_pipeline_run(duplicate, path=database)


if __name__ == "__main__":
    unittest.main()
