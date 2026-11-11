from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Mapping

import yaml
from flask import current_app, has_app_context

from app.db import db
from app.models import Project
from app.modules.deployments.kubectl import run_kubectl
from app.modules.projects.service import project_namespace
from app.modules.quotas.service import cpu_to_millicores, memory_to_mib
from app.modules.subscriptions.service import subscription_limits


RESOURCE_QUOTA_NAME = "platform-project-quota"
LIMIT_RANGE_NAME = "platform-project-limits"


class KubernetesQuotaSyncError(RuntimeError):
    pass


def quota_sync_enabled() -> bool:
    if not has_app_context():
        return True
    return bool(
        current_app.config.get(
            "KUBERNETES_QUOTA_SYNC_ENABLED", not current_app.testing
        )
    )


def _quantity(value: int, suffix: str) -> str:
    return f"{max(0, int(value))}{suffix}"


def _container_defaults(limits: Mapping[str, int]) -> dict[str, dict[str, str]]:
    cpu_request = min(100, int(limits.get("cpu_request_millicores", 0)))
    cpu_limit = min(500, int(limits.get("cpu_limit_millicores", 0)))
    memory_request = min(128, int(limits.get("memory_request_mib", 0)))
    memory_limit = min(512, int(limits.get("memory_limit_mib", 0)))
    cpu_request = min(cpu_request, cpu_limit)
    memory_request = min(memory_request, memory_limit)

    default_request: dict[str, str] = {}
    default_limit: dict[str, str] = {}
    if cpu_request > 0:
        default_request["cpu"] = _quantity(cpu_request, "m")
    if memory_request > 0:
        default_request["memory"] = _quantity(memory_request, "Mi")
    if cpu_limit > 0:
        default_limit["cpu"] = _quantity(cpu_limit, "m")
    if memory_limit > 0:
        default_limit["memory"] = _quantity(memory_limit, "Mi")
    return {"defaultRequest": default_request, "default": default_limit}


def desired_quota_documents(project: Project) -> list[dict[str, Any]]:
    if project.subscription is None:
        raise KubernetesQuotaSyncError("Project chưa có subscription để sinh quota.")
    namespace = project_namespace(project)
    limits = subscription_limits(project.subscription)
    labels = {
        "managed-by": "luanvan-platform",
        "platform/project-id": str(project.id),
    }
    hard = {
        "pods": str(limits["max_replicas"]),
        "services": str(limits["max_services"]),
        "count/ingresses.networking.k8s.io": str(limits["max_ingresses"]),
        "persistentvolumeclaims": str(limits["max_pvcs"]),
        "requests.storage": _quantity(limits["storage_mib"], "Mi"),
        "requests.cpu": _quantity(limits["cpu_request_millicores"], "m"),
        "limits.cpu": _quantity(limits["cpu_limit_millicores"], "m"),
        "requests.memory": _quantity(limits["memory_request_mib"], "Mi"),
        "limits.memory": _quantity(limits["memory_limit_mib"], "Mi"),
    }
    defaults = _container_defaults(limits)
    return [
        {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {"name": namespace, "labels": labels},
        },
        {
            "apiVersion": "v1",
            "kind": "ResourceQuota",
            "metadata": {
                "name": RESOURCE_QUOTA_NAME,
                "namespace": namespace,
                "labels": labels,
            },
            "spec": {"hard": hard},
        },
        {
            "apiVersion": "v1",
            "kind": "LimitRange",
            "metadata": {
                "name": LIMIT_RANGE_NAME,
                "namespace": namespace,
                "labels": labels,
            },
            "spec": {
                "limits": [{"type": "Container", **defaults}],
            },
        },
    ]


def desired_quota_manifest(project: Project) -> str:
    return yaml.safe_dump_all(
        desired_quota_documents(project),
        sort_keys=False,
        explicit_start=True,
    )


def _canonical_quantity(resource: str, value: Any) -> int | str:
    if resource in {"requests.cpu", "limits.cpu", "cpu"}:
        return cpu_to_millicores(value, resource)
    if resource in {
        "requests.memory", "limits.memory", "requests.storage", "memory"
    }:
        return memory_to_mib(value, resource)
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return str(value)


def _canonical_hard(hard: Mapping[str, Any]) -> dict[str, int | str]:
    return {
        key: _canonical_quantity(key, value)
        for key, value in sorted(hard.items())
    }


def _canonical_defaults(values: Mapping[str, Any]) -> dict[str, int | str]:
    return {
        key: _canonical_quantity(key, value)
        for key, value in sorted(values.items())
    }


def _desired_semantic_state(project: Project) -> dict[str, Any]:
    namespace, quota, limit_range = desired_quota_documents(project)
    container = limit_range["spec"]["limits"][0]
    return {
        "namespace": namespace["metadata"]["name"],
        "labels": namespace["metadata"]["labels"],
        "hard": _canonical_hard(quota["spec"]["hard"]),
        "defaultRequest": _canonical_defaults(container.get("defaultRequest", {})),
        "default": _canonical_defaults(container.get("default", {})),
    }


def _state_hash(state: Mapping[str, Any]) -> str:
    payload = json.dumps(state, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def mark_project_quota_pending(
    project: Project, message: str = "Chờ đồng bộ ResourceQuota và LimitRange."
) -> None:
    desired = _desired_semantic_state(project)
    project.quota_sync_status = Project.QUOTA_SYNC_PENDING
    project.quota_sync_message = message[:500]
    project.quota_desired_hash = _state_hash(desired)
    project.quota_observed_hash = ""


def _load_json_resource(args: list[str]) -> tuple[dict[str, Any] | None, str]:
    success, output = run_kubectl([*args, "-o", "json"], timeout=20)
    if not success:
        return None, output
    try:
        return json.loads(output), ""
    except (json.JSONDecodeError, TypeError):
        return None, "Kubernetes trả về JSON không hợp lệ."


def _observed_semantic_state(project: Project) -> tuple[dict[str, Any] | None, str]:
    namespace = project_namespace(project)
    namespace_data, error = _load_json_resource(["get", "namespace", namespace])
    if not namespace_data:
        return None, error
    quota_data, error = _load_json_resource([
        "get", "resourcequota", RESOURCE_QUOTA_NAME, "-n", namespace,
    ])
    if not quota_data:
        return None, error
    limit_data, error = _load_json_resource([
        "get", "limitrange", LIMIT_RANGE_NAME, "-n", namespace,
    ])
    if not limit_data:
        return None, error
    container_limits = next(
        (
            item for item in limit_data.get("spec", {}).get("limits", [])
            if item.get("type") == "Container"
        ),
        {},
    )
    labels = namespace_data.get("metadata", {}).get("labels", {})
    managed_labels = {
        key: labels.get(key, "")
        for key in ("managed-by", "platform/project-id")
    }
    return {
        "namespace": namespace_data.get("metadata", {}).get("name", ""),
        "labels": managed_labels,
        "hard": _canonical_hard(quota_data.get("spec", {}).get("hard", {})),
        "defaultRequest": _canonical_defaults(
            container_limits.get("defaultRequest", {})
        ),
        "default": _canonical_defaults(container_limits.get("default", {})),
    }, ""


def inspect_project_quota(project: Project, *, commit: bool = True) -> dict[str, Any]:
    desired = _desired_semantic_state(project)
    desired_hash = _state_hash(desired)
    observed, error = _observed_semantic_state(project)
    project.quota_desired_hash = desired_hash
    if observed is None:
        project.quota_observed_hash = ""
        lowered = error.lower()
        if "notfound" in lowered or "not found" in lowered:
            project.quota_sync_status = Project.QUOTA_SYNC_DRIFTED
            project.quota_sync_message = "Thiếu namespace hoặc quota resource trên Kubernetes."
        else:
            project.quota_sync_status = Project.QUOTA_SYNC_ERROR
            project.quota_sync_message = (error or "Không đọc được quota Kubernetes.")[:500]
    else:
        observed_hash = _state_hash(observed)
        project.quota_observed_hash = observed_hash
        if observed_hash == desired_hash:
            project.quota_sync_status = Project.QUOTA_SYNC_SYNCED
            project.quota_sync_message = "ResourceQuota và LimitRange đã đồng bộ."
            project.quota_synced_at = datetime.now(timezone.utc)
        else:
            project.quota_sync_status = Project.QUOTA_SYNC_DRIFTED
            project.quota_sync_message = "Phát hiện quota Kubernetes khác desired state."
    if commit:
        db.session.commit()
    return {
        "status": project.quota_sync_status,
        "message": project.quota_sync_message,
        "desired_hash": project.quota_desired_hash,
        "observed_hash": project.quota_observed_hash,
        "desired": desired,
        "observed": observed,
    }


def sync_project_quota(project: Project, *, commit: bool = True) -> dict[str, Any]:
    project = Project.query.filter_by(id=project.id).with_for_update().one()
    mark_project_quota_pending(project, "Đang đồng bộ quota Kubernetes.")
    manifest = desired_quota_manifest(project)
    success, output = run_kubectl(
        ["apply", "--validate=false", "-f", "-"],
        timeout=45,
        stdin=manifest,
    )
    if not success:
        project.quota_sync_status = Project.QUOTA_SYNC_ERROR
        project.quota_sync_message = (output or "kubectl apply quota thất bại.")[:500]
        if commit:
            db.session.commit()
        return {
            "status": project.quota_sync_status,
            "message": project.quota_sync_message,
            "output": output,
        }
    result = inspect_project_quota(project, commit=commit)
    result["output"] = output
    return result


def ensure_project_quota_synced(project: Project) -> dict[str, Any]:
    if not quota_sync_enabled():
        return {"status": "Disabled", "message": "Quota sync bị tắt trong môi trường này."}
    result = sync_project_quota(project)
    if result["status"] != Project.QUOTA_SYNC_SYNCED:
        raise KubernetesQuotaSyncError(result["message"])
    return result
