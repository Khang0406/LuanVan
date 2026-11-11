from __future__ import annotations

import math
import re
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping

from app.db import db
from app.models import Project
from app.modules.subscriptions.service import (
    LIMIT_FIELDS,
    assign_initial_subscription,
    subscription_limits,
)


USAGE_FIELDS = tuple(LIMIT_FIELDS)


class QuotaConfigurationError(ValueError):
    """Raised when an application contains an invalid resource quantity."""


class QuotaExceeded(ValueError):
    code = "QUOTA_EXCEEDED"

    def __init__(
        self,
        *,
        project_id: int,
        plan: str,
        resource: str,
        used: int,
        requested: int,
        projected: int,
        limit: int,
        service: str | None = None,
    ) -> None:
        self.details = {
            "project_id": project_id,
            "plan": plan,
            "resource": resource,
            "label": LIMIT_FIELDS.get(resource, resource),
            "used": used,
            "requested": requested,
            "projected": projected,
            "limit": limit,
        }
        if service:
            self.details["service"] = service
        label = self.details["label"]
        super().__init__(
            f"Yêu cầu vượt quota {label}: đang dùng {used}, "
            f"sau thay đổi {projected}, giới hạn {limit}."
        )


def empty_usage() -> dict[str, int]:
    return {key: 0 for key in USAGE_FIELDS}


def _non_negative_int(value: Any, field: str, default: int = 0) -> int:
    raw = default if value in (None, "") else value
    try:
        parsed = int(raw)
    except (TypeError, ValueError) as exc:
        raise QuotaConfigurationError(f"{field} phải là số nguyên.") from exc
    if parsed < 0:
        raise QuotaConfigurationError(f"{field} không được âm.")
    return parsed


def cpu_to_millicores(value: Any, field: str = "CPU") -> int:
    raw = str(value).strip()
    match = re.fullmatch(r"(\d+(?:\.\d+)?)(m|u|n)?", raw)
    if not match:
        raise QuotaConfigurationError(
            f"{field} không đúng định dạng Kubernetes, ví dụ 100m hoặc 0.5."
        )
    try:
        amount = Decimal(match.group(1))
    except InvalidOperation as exc:
        raise QuotaConfigurationError(f"{field} không hợp lệ.") from exc
    suffix = match.group(2) or ""
    multiplier = {
        "": Decimal(1000),
        "m": Decimal(1),
        "u": Decimal("0.001"),
        "n": Decimal("0.000001"),
    }[suffix]
    return int(math.ceil(amount * multiplier))


def memory_to_mib(value: Any, field: str = "Memory") -> int:
    raw = str(value).strip()
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([KMGTPE]i|[kKMGTPE])?", raw)
    if not match:
        raise QuotaConfigurationError(
            f"{field} không đúng định dạng Kubernetes, ví dụ 128Mi hoặc 1Gi."
        )
    try:
        amount = Decimal(match.group(1))
    except InvalidOperation as exc:
        raise QuotaConfigurationError(f"{field} không hợp lệ.") from exc
    suffix = match.group(2) or ""
    binary = {
        "Ki": Decimal(1024),
        "Mi": Decimal(1024) ** 2,
        "Gi": Decimal(1024) ** 3,
        "Ti": Decimal(1024) ** 4,
        "Pi": Decimal(1024) ** 5,
        "Ei": Decimal(1024) ** 6,
    }
    decimal = {
        "k": Decimal(1000),
        "K": Decimal(1000),
        "M": Decimal(1000) ** 2,
        "G": Decimal(1000) ** 3,
        "T": Decimal(1000) ** 4,
        "P": Decimal(1000) ** 5,
        "E": Decimal(1000) ** 6,
    }
    bytes_value = amount * (binary.get(suffix) or decimal.get(suffix) or Decimal(1))
    return int(math.ceil(bytes_value / (Decimal(1024) ** 2)))


def _service_usage(service: Mapping[str, Any], replicas: int) -> dict[str, int]:
    usage = empty_usage()
    usage["max_services"] = 1
    usage["max_replicas"] = replicas
    cpu_request = cpu_to_millicores(
        service.get("cpu_request", "100m"), "CPU request"
    )
    cpu_limit = cpu_to_millicores(service.get("cpu_limit", "500m"), "CPU limit")
    memory_request = memory_to_mib(
        service.get("memory_request", "128Mi"), "Memory request"
    )
    memory_limit = memory_to_mib(
        service.get("memory_limit", "512Mi"), "Memory limit"
    )
    if cpu_request > cpu_limit:
        raise QuotaConfigurationError("CPU request không được lớn hơn CPU limit.")
    if memory_request > memory_limit:
        raise QuotaConfigurationError("Memory request không được lớn hơn Memory limit.")
    usage["cpu_request_millicores"] = cpu_request * replicas
    usage["cpu_limit_millicores"] = cpu_limit * replicas
    usage["memory_request_mib"] = memory_request * replicas
    usage["memory_limit_mib"] = memory_limit * replicas
    if (
        service.get("public", True)
        and service.get("ingress")
        and isinstance(service.get("ingress"), dict)
    ):
        usage["max_ingresses"] = 1
    return usage


def _add_usage(target: dict[str, int], addition: Mapping[str, int]) -> None:
    for key in USAGE_FIELDS:
        target[key] += int(addition.get(key, 0))


def calculate_application_usage(
    application: Mapping[str, Any], *, replicas_override: int | None = None
) -> dict[str, int]:
    usage = empty_usage()
    usage["max_applications"] = 1
    pvc_names: set[str] = set()

    services = application.get("services", [])
    if not isinstance(services, list):
        raise QuotaConfigurationError("Danh sách service của application không hợp lệ.")
    for index, service in enumerate(services):
        if not isinstance(service, Mapping):
            raise QuotaConfigurationError("Cấu hình service không hợp lệ.")
        replicas = (
            replicas_override
            if replicas_override is not None
            else _non_negative_int(service.get("replicas"), f"Replicas service {index + 1}", 1)
        )
        _add_usage(usage, _service_usage(service, replicas))
        storage = service.get("storage", [])
        if not isinstance(storage, list):
            raise QuotaConfigurationError("Cấu hình storage của service không hợp lệ.")
        for volume_index, volume in enumerate(storage):
            if not isinstance(volume, Mapping) or volume.get("type") != "pvc":
                continue
            name = str(
                volume.get("pvc_name")
                or f"{service.get('name', index)}-{volume.get('name', volume_index)}-pvc"
            )
            if name in pvc_names:
                continue
            pvc_names.add(name)
            usage["max_pvcs"] += 1
            usage["storage_mib"] += memory_to_mib(
                volume.get("size", "1Gi"), f"Storage PVC {name}"
            )

    databases = application.get("databases", [])
    if not isinstance(databases, list):
        raise QuotaConfigurationError("Danh sách database của application không hợp lệ.")
    for index, database in enumerate(databases):
        if not isinstance(database, Mapping):
            raise QuotaConfigurationError("Cấu hình database không hợp lệ.")
        replicas = _non_negative_int(
            database.get("replicas"), f"Replicas database {index + 1}", 1
        )
        _add_usage(usage, _service_usage(database, replicas))
        init_job = database.get("init_job")
        if isinstance(init_job, Mapping):
            init_usage = _service_usage({
                "cpu_request": init_job.get("cpu_request", "50m"),
                "cpu_limit": init_job.get("cpu_limit", "100m"),
                "memory_request": init_job.get("memory_request", "64Mi"),
                "memory_limit": init_job.get("memory_limit", "128Mi"),
                "public": False,
            }, 1)
            # An init Job consumes a pod/resources temporarily but does not
            # create a Kubernetes Service.
            init_usage["max_services"] = 0
            _add_usage(usage, init_usage)
        pvc_count = replicas if database.get("kind") == "StatefulSet" else 1
        usage["max_pvcs"] += pvc_count
        usage["storage_mib"] += memory_to_mib(
            database.get("storage_size", "1Gi"),
            f"Storage database {database.get('name', index + 1)}",
        ) * pvc_count

    return usage


def calculate_project_usage(
    project_id: int, applications: Iterable[Mapping[str, Any]] | None = None
) -> dict[str, int]:
    if applications is None:
        from app.modules.applications.service import load_applications

        applications = load_applications()
    usage = empty_usage()
    for application in applications:
        try:
            application_project_id = int(application.get("project_id"))
        except (TypeError, ValueError):
            continue
        if application_project_id == int(project_id):
            _add_usage(usage, calculate_application_usage(application))
    return usage


def _locked_project(project_id: int) -> Project:
    project = (
        Project.query.filter_by(id=project_id, status=Project.STATUS_ACTIVE)
        .with_for_update()
        .first()
    )
    if project is None:
        raise QuotaConfigurationError("Project không tồn tại hoặc đã bị vô hiệu hóa.")
    if project.subscription is None:
        subscription = assign_initial_subscription(
            project, project.owner, commit=False
        )
        project.subscription = subscription
        db.session.flush()
    return project


def _raise_first_exceeded(
    project: Project,
    used: Mapping[str, int],
    requested: Mapping[str, int],
    projected: Mapping[str, int],
) -> None:
    limits = subscription_limits(project.subscription)
    for resource in USAGE_FIELDS:
        limit = int(limits.get(resource, 0))
        value = int(projected.get(resource, 0))
        if value > limit:
            raise QuotaExceeded(
                project_id=project.id,
                plan=project.subscription.plan.key,
                resource=resource,
                used=int(used.get(resource, 0)),
                requested=int(requested.get(resource, 0)),
                projected=value,
                limit=limit,
            )


def validate_application_creation(
    project_id: int, application: Mapping[str, Any]
) -> dict[str, Any]:
    project = _locked_project(project_id)
    used = calculate_project_usage(project.id)
    requested = calculate_application_usage(application)
    projected = {
        key: used[key] + requested[key]
        for key in USAGE_FIELDS
    }
    _raise_first_exceeded(project, used, requested, projected)
    return {
        "used": used,
        "requested": requested,
        "projected": projected,
        "limits": subscription_limits(project.subscription),
    }


def validate_deployment(application: Mapping[str, Any]) -> dict[str, Any] | None:
    try:
        project_id = int(application.get("project_id"))
    except (TypeError, ValueError):
        return None
    project = _locked_project(project_id)
    used = calculate_project_usage(project.id)
    requested = empty_usage()
    _raise_first_exceeded(project, used, requested, used)
    return {
        "used": used,
        "requested": requested,
        "projected": dict(used),
        "limits": subscription_limits(project.subscription),
    }


def validate_scale(
    application: Mapping[str, Any], replicas: int
) -> dict[str, Any] | None:
    try:
        project_id = int(application.get("project_id"))
    except (TypeError, ValueError):
        return None
    project = _locked_project(project_id)
    used = calculate_project_usage(project.id)
    current_application = calculate_application_usage(application)
    proposed_application = calculate_application_usage(
        application, replicas_override=replicas
    )
    requested = {
        key: proposed_application[key] - current_application[key]
        for key in USAGE_FIELDS
    }
    projected = {
        key: used[key] + requested[key]
        for key in USAGE_FIELDS
    }

    # A scale-down must remain possible even when a plan downgrade or external
    # change has already left the project over quota.
    if proposed_application["max_replicas"] <= current_application["max_replicas"]:
        return {
            "used": used,
            "requested": requested,
            "projected": projected,
            "limits": subscription_limits(project.subscription),
            "scale_down": True,
        }

    for service in application.get("services", []):
        current_replicas = _non_negative_int(
            service.get("replicas"), f"Replicas service {service.get('name', '')}", 1
        )
        service_limit = _non_negative_int(
            service.get("max_replicas"),
            f"Max replicas service {service.get('name', '')}",
            100,
        )
        if replicas > current_replicas and replicas > service_limit:
            raise QuotaExceeded(
                project_id=project.id,
                plan=project.subscription.plan.key,
                resource="max_replicas",
                used=current_replicas,
                requested=replicas - current_replicas,
                projected=replicas,
                limit=service_limit,
                service=str(service.get("name", "")),
            )

    _raise_first_exceeded(project, used, requested, projected)
    return {
        "used": used,
        "requested": requested,
        "projected": projected,
        "limits": subscription_limits(project.subscription),
        "scale_down": False,
    }
