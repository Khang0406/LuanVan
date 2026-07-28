"""Deployment versioning and rollback orchestration."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.delivery_store import (
    create_deployment_record,
    get_deployment,
    list_deployments,
    update_deployment_record,
)
from app.modules.applications.service import add_activity, save_application
from app.modules.audit.service import record_audit
from app.modules.deployments.kubectl import run_kubectl, verify_application
from app.modules.jobs.service import create_job, finish_job


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _actor(deployment_actor: Any | None) -> dict[str, Any]:
    if getattr(deployment_actor, "is_authenticated", False):
        return {
            "actor": getattr(deployment_actor, "username", "unknown"),
            "actor_id": getattr(deployment_actor, "id", None),
            "actor_role": getattr(deployment_actor, "role", ""),
        }
    if isinstance(deployment_actor, dict):
        return {
            "actor": deployment_actor.get("actor", "system"),
            "actor_id": deployment_actor.get("actor_id"),
            "actor_role": deployment_actor.get("actor_role", ""),
        }
    if isinstance(deployment_actor, str) and deployment_actor:
        return {"actor": deployment_actor, "actor_id": None, "actor_role": ""}
    return {"actor": "system", "actor_id": None, "actor_role": ""}


def begin_pipeline_deployment(
    application: dict[str, Any],
    pipeline_run: dict[str, Any],
    manifest: str,
) -> dict[str, Any]:
    actor = {
        "actor": pipeline_run.get("actor", "system"),
        "actor_id": pipeline_run.get("actor_id"),
        "actor_role": pipeline_run.get("actor_role", ""),
    }
    services = pipeline_run.get("service_images") or [
        {
            "service": service["name"],
            "image": service.get("image", ""),
            "digest": service.get("image_digest", ""),
            "required": service.get("required", True),
        }
        for service in application.get("services", [])
    ]
    managed_database = application.get("managed_database")
    if managed_database:
        services = [*services, {
            "service": managed_database.get("name", "mysql"),
            "image": managed_database.get("image", "mysql:8.0"),
            "digest": managed_database.get("image_digest", ""),
            "required": managed_database.get("required", True),
        }]
    deployment_mode = pipeline_run.get("deployment_mode") or (
        "Production" if application.get("deployment_mode", "production") == "production"
        else "Development fallback"
    )
    rollback_eligible = (
        deployment_mode == "Production"
        and all(service.get("image") for service in services)
    )
    record = create_deployment_record({
        "application_id": application["id"],
        "pipeline_run_id": pipeline_run["id"],
        "previous_deployment_id": application.get("current_deployment_id"),
        "repository": pipeline_run.get("repository", application.get("github_url", "")),
        "branch": pipeline_run.get("branch", application.get("default_branch", "")),
        "commit_sha": pipeline_run.get("commit_sha", ""),
        "services": services,
        "manifest": manifest,
        "deployment_mode": deployment_mode,
        "rollback_eligible": rollback_eligible,
        "namespace": application["namespace"],
        "url": application.get("url", ""),
        "verify_status": "Pending",
        "status": "Progressing",
        "started_at": _now(),
        "finished_at": "",
        **actor,
    })
    pipeline_run["deployment_id"] = record["id"]
    pipeline_run["deployment_version"] = record["version"]
    add_activity(
        application, "DEPLOYMENT",
        f"Created deployment v{record['version']} ({record['id']}) in {deployment_mode} mode.",
        "Running",
    )
    save_application(application)
    return record


def finish_pipeline_deployment(
    application: dict[str, Any],
    deployment: dict[str, Any],
    *,
    success: bool,
    verify_message: str,
    verify_details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    deployment["verify_status"] = "Ready" if success else "Failed"
    deployment["status"] = "Ready" if success else "Failed"
    deployment["finished_at"] = _now()
    deployment["verify_message"] = verify_message
    deployment["verify_details"] = verify_details or {}
    if verify_details and verify_details.get("url"):
        deployment["url"] = verify_details["url"]
    update_deployment_record(deployment)

    if success:
        application["current_deployment_id"] = deployment["id"]
        application["url"] = deployment.get("url") or application.get("url", "")
        application["current_commit_sha"] = deployment.get("commit_sha", "")
        application["status"] = "Running"
        deployed_images = {
            item.get("service"): item for item in deployment.get("services", [])
        }
        for service in application.get("services", []):
            deployed = deployed_images.get(service.get("name"))
            if deployed:
                service["image"] = deployed.get("image", service.get("image", ""))
                service["image_digest"] = deployed.get("digest", "")
        add_activity(
            application, "DEPLOYMENT",
            f"Deployment v{deployment['version']} is Ready and is now current.", "Done",
        )
    else:
        add_activity(
            application, "DEPLOYMENT",
            f"Deployment v{deployment['version']} verify failed: {verify_message}", "Failed",
        )
    save_application(application)
    return deployment


def rollback_application(
    application: dict[str, Any],
    target_deployment_id: str,
    actor: Any | None = None,
    *,
    timeout_seconds: int = 180,
) -> tuple[bool, str, dict[str, Any] | None]:
    """Apply a prior immutable manifest and create a new rollback deployment."""
    target = get_deployment(target_deployment_id)
    actor_payload = _actor(actor)
    if not target or target.get("application_id") != application.get("id"):
        message = "Deployment không tồn tại hoặc không thuộc application."
        record_audit(
            "DEPLOYMENT_ROLLBACK", application.get("id", ""), "FAILED", message,
            user=actor, metadata={"target_deployment_id": target_deployment_id},
        )
        return False, message, None
    if not target.get("rollback_eligible") or target.get("deployment_mode") != "Production":
        message = "Deployment này không đủ điều kiện rollback bằng immutable image."
        record_audit(
            "DEPLOYMENT_ROLLBACK", application["id"], "FAILED", message,
            user=actor, metadata={"target_deployment_id": target_deployment_id},
        )
        return False, message, None
    if not target.get("manifest"):
        message = "Deployment không có manifest đã lưu."
        return False, message, None

    rollback = create_deployment_record({
        "application_id": application["id"],
        "pipeline_run_id": None,
        "previous_deployment_id": application.get("current_deployment_id"),
        "rollback_of_deployment_id": target["id"],
        "repository": target.get("repository", ""),
        "branch": target.get("branch", ""),
        "commit_sha": target.get("commit_sha", ""),
        "services": target.get("services", []),
        "manifest": target["manifest"],
        "deployment_mode": "Production",
        "rollback_eligible": True,
        "namespace": application["namespace"],
        "url": target.get("url", ""),
        "verify_status": "Pending",
        "status": "Progressing",
        "started_at": _now(),
        "finished_at": "",
        **actor_payload,
    })
    job = create_job(
        "Rollback", f"Rollback {application['name']} to v{target['version']}",
        application["id"], command="kubectl apply --validate=false -f -", actor=actor,
        metadata={
            "application_id": application["id"],
            "target_deployment_id": target["id"],
            "rollback_deployment_id": rollback["id"],
        },
    )
    add_activity(
        application, "ROLLBACK",
        f"Rollback v{rollback['version']} started from deployment v{target['version']}.",
        "Running",
    )
    save_application(application)

    apply_ok, apply_output = run_kubectl(
        ["apply", "--validate=false", "-f", "-"],
        timeout=90,
        stdin=target["manifest"],
    )
    if not apply_ok:
        rollback.update({
            "status": "Failed", "verify_status": "Failed", "finished_at": _now(),
            "verify_message": apply_output,
        })
        update_deployment_record(rollback)
        finish_job(job["id"], False, apply_output, "Rollback apply failed")
        add_activity(application, "ROLLBACK", f"Rollback apply failed: {apply_output}", "Failed")
        save_application(application)
        record_audit(
            "DEPLOYMENT_ROLLBACK", application["id"], "FAILED", apply_output,
            user=actor, metadata={"rollback_deployment_id": rollback["id"]},
        )
        return False, apply_output, rollback

    verify_ok, verify_message, verify_details = verify_application(
        application, timeout_seconds=timeout_seconds
    )
    rollback = finish_pipeline_deployment(
        application, rollback, success=verify_ok,
        verify_message=verify_message, verify_details=verify_details,
    )
    output = f"{apply_output}\n{verify_message}".strip()
    finish_job(job["id"], verify_ok, output, verify_message)
    record_audit(
        "DEPLOYMENT_ROLLBACK", application["id"],
        "SUCCESS" if verify_ok else "FAILED", verify_message,
        user=actor,
        metadata={
            "target_deployment_id": target["id"],
            "rollback_deployment_id": rollback["id"],
        },
    )
    add_activity(
        application, "ROLLBACK",
        (
            f"Rollback to v{target['version']} succeeded as v{rollback['version']}."
            if verify_ok else f"Rollback verify failed: {verify_message}"
        ),
        "Done" if verify_ok else "Failed",
    )
    save_application(application)
    return verify_ok, verify_message, rollback


__all__ = [
    "begin_pipeline_deployment",
    "finish_pipeline_deployment",
    "get_deployment",
    "list_deployments",
    "rollback_application",
]
