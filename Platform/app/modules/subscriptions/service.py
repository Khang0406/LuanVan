from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Mapping

from app.db import db
from app.models import (
    Project,
    ProjectSubscription,
    SubscriptionHistory,
    SubscriptionPlan,
    SubscriptionUpgradeRequest,
    User,
)


PLAN_BASIC = "basic"
PLAN_PRO = "pro"
PLAN_CUSTOM = "custom"

LIMIT_FIELDS = {
    "max_applications": "Số application",
    "max_services": "Số service",
    "max_replicas": "Tổng replica/pod",
    "cpu_request_millicores": "CPU request (millicore)",
    "cpu_limit_millicores": "CPU limit (millicore)",
    "memory_request_mib": "Memory request (MiB)",
    "memory_limit_mib": "Memory limit (MiB)",
    "max_ingresses": "Số Ingress",
    "max_pvcs": "Số PVC",
    "storage_mib": "Storage (MiB)",
}

PLAN_DEFINITIONS = {
    PLAN_BASIC: {
        "name": "Basic",
        "description": "Gói cơ bản cho nhóm nhỏ và môi trường thử nghiệm.",
        "is_custom": False,
        "limits": {
            "max_applications": 3,
            "max_services": 10,
            "max_replicas": 10,
            "cpu_request_millicores": 2000,
            "cpu_limit_millicores": 4000,
            "memory_request_mib": 4096,
            "memory_limit_mib": 8192,
            "max_ingresses": 3,
            "max_pvcs": 3,
            "storage_mib": 10240,
        },
    },
    PLAN_PRO: {
        "name": "Pro/VIP",
        "description": "Gói mở rộng cho workload nhiều service.",
        "is_custom": False,
        "limits": {
            "max_applications": 10,
            "max_services": 40,
            "max_replicas": 40,
            "cpu_request_millicores": 8000,
            "cpu_limit_millicores": 16000,
            "memory_request_mib": 16384,
            "memory_limit_mib": 32768,
            "max_ingresses": 10,
            "max_pvcs": 10,
            "storage_mib": 102400,
        },
    },
    PLAN_CUSTOM: {
        "name": "Custom",
        "description": "Hạn mức riêng do Platform Admin phê duyệt.",
        "is_custom": True,
        "limits": {},
    },
}


class SubscriptionError(ValueError):
    pass


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _dump_limits(limits: Mapping[str, int]) -> str:
    return json.dumps(dict(limits), sort_keys=True, separators=(",", ":"))


def decode_limits(raw: str | None) -> dict[str, int]:
    try:
        decoded = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(decoded, dict):
        return {}
    return {
        key: int(value)
        for key, value in decoded.items()
        if key in LIMIT_FIELDS and isinstance(value, int) and not isinstance(value, bool)
    }


def validate_limits(values: Mapping[str, Any]) -> dict[str, int]:
    limits: dict[str, int] = {}
    for key, label in LIMIT_FIELDS.items():
        try:
            value = int(values.get(key, ""))
        except (TypeError, ValueError) as exc:
            raise SubscriptionError(f"{label} phải là số nguyên.") from exc
        if value < 0 or value > 1_000_000_000:
            raise SubscriptionError(f"{label} phải nằm trong khoảng 0..1.000.000.000.")
        limits[key] = value
    if limits["cpu_request_millicores"] > limits["cpu_limit_millicores"]:
        raise SubscriptionError("CPU request không được lớn hơn CPU limit.")
    if limits["memory_request_mib"] > limits["memory_limit_mib"]:
        raise SubscriptionError("Memory request không được lớn hơn Memory limit.")
    return limits


def plan_limits(plan: SubscriptionPlan) -> dict[str, int]:
    return decode_limits(plan.limits_json)


def subscription_limits(subscription: ProjectSubscription) -> dict[str, int]:
    return decode_limits(subscription.effective_limits_json)


def get_plan(plan_key: str) -> SubscriptionPlan:
    plan = SubscriptionPlan.query.filter_by(key=plan_key, active=True).first()
    if plan is None:
        raise SubscriptionError("Gói sử dụng không hợp lệ hoặc đã ngừng hoạt động.")
    return plan


def list_plans() -> list[SubscriptionPlan]:
    order = {PLAN_BASIC: 0, PLAN_PRO: 1, PLAN_CUSTOM: 2}
    plans = SubscriptionPlan.query.filter_by(active=True).all()
    return sorted(plans, key=lambda item: (order.get(item.key, 99), item.name))


def ensure_subscription_catalog() -> None:
    plans: dict[str, SubscriptionPlan] = {}
    for key, definition in PLAN_DEFINITIONS.items():
        plan = SubscriptionPlan.query.filter_by(key=key).first()
        if plan is None:
            plan = SubscriptionPlan(key=key)
            db.session.add(plan)
        plan.name = definition["name"]
        plan.description = definition["description"]
        plan.limits_json = _dump_limits(definition["limits"])
        plan.is_system = True
        plan.is_custom = definition["is_custom"]
        plan.active = True
        plans[key] = plan
    db.session.flush()

    basic = plans[PLAN_BASIC]
    subscribed_ids = {
        item[0] for item in db.session.query(ProjectSubscription.project_id).all()
    }
    for project in Project.query.order_by(Project.id):
        if project.id in subscribed_ids:
            continue
        subscription = ProjectSubscription(
            project_id=project.id,
            plan_id=basic.id,
            effective_limits_json=basic.limits_json,
            assigned_by_user_id=project.owner_user_id,
        )
        db.session.add(subscription)
        db.session.add(SubscriptionHistory(
            project_id=project.id,
            to_plan_id=basic.id,
            to_limits_json=basic.limits_json,
            action="INITIAL_ASSIGNMENT",
            actor_user_id=project.owner_user_id,
        ))
    db.session.commit()


def assign_initial_subscription(
    project: Project, actor: User | None, *, commit: bool = True
) -> ProjectSubscription:
    existing = ProjectSubscription.query.filter_by(project_id=project.id).first()
    if existing:
        return existing
    basic = get_plan(PLAN_BASIC)
    subscription = ProjectSubscription(
        project_id=project.id,
        plan_id=basic.id,
        effective_limits_json=basic.limits_json,
        assigned_by_user_id=getattr(actor, "id", None),
    )
    db.session.add(subscription)
    db.session.add(SubscriptionHistory(
        project_id=project.id,
        to_plan_id=basic.id,
        to_limits_json=basic.limits_json,
        action="INITIAL_ASSIGNMENT",
        actor_user_id=getattr(actor, "id", None),
    ))
    if commit:
        db.session.commit()
    return subscription


def get_project_subscription(project: Project) -> ProjectSubscription:
    subscription = ProjectSubscription.query.filter_by(project_id=project.id).first()
    if subscription is None:
        subscription = assign_initial_subscription(project, project.owner)
    return subscription


def submit_upgrade_request(
    project: Project,
    actor: User,
    plan_key: str,
    reason: str,
    custom_values: Mapping[str, Any] | None = None,
) -> SubscriptionUpgradeRequest:
    # Serialize per-project request creation so only one pending request exists.
    Project.query.filter_by(id=project.id).with_for_update().one()
    pending = SubscriptionUpgradeRequest.query.filter_by(
        project_id=project.id,
        status=SubscriptionUpgradeRequest.STATUS_PENDING,
    ).first()
    if pending:
        raise SubscriptionError("Project đã có một yêu cầu đang chờ xử lý.")
    plan = get_plan(plan_key)
    limits = validate_limits(custom_values or {}) if plan.is_custom else plan_limits(plan)
    clean_reason = reason.strip()
    if len(clean_reason) < 10 or len(clean_reason) > 1000:
        raise SubscriptionError("Lý do nâng cấp phải có từ 10 đến 1000 ký tự.")
    current = get_project_subscription(project)
    if plan.id == current.plan_id and limits == subscription_limits(current):
        raise SubscriptionError("Project đang sử dụng đúng gói và hạn mức này.")
    upgrade_request = SubscriptionUpgradeRequest(
        project_id=project.id,
        requested_plan_id=plan.id,
        requested_limits_json=_dump_limits(limits),
        reason=clean_reason,
        requested_by_user_id=actor.id,
    )
    db.session.add(upgrade_request)
    db.session.commit()
    return upgrade_request


def _apply_plan(
    project: Project,
    plan: SubscriptionPlan,
    limits: Mapping[str, int],
    actor: User,
    *,
    action: str,
    request_id: int | None = None,
) -> ProjectSubscription:
    subscription = (
        ProjectSubscription.query.filter_by(project_id=project.id)
        .with_for_update()
        .first()
    )
    if subscription is None:
        subscription = assign_initial_subscription(project, actor, commit=False)
        db.session.flush()
    old_plan_id = subscription.plan_id
    old_limits_json = subscription.effective_limits_json
    new_limits_json = _dump_limits(limits)
    subscription.plan_id = plan.id
    subscription.effective_limits_json = new_limits_json
    subscription.assigned_by_user_id = actor.id
    subscription.updated_at = _utcnow()
    db.session.add(SubscriptionHistory(
        project_id=project.id,
        from_plan_id=old_plan_id,
        to_plan_id=plan.id,
        from_limits_json=old_limits_json,
        to_limits_json=new_limits_json,
        action=action,
        actor_user_id=actor.id,
        request_id=request_id,
    ))
    return subscription


def review_upgrade_request(
    request_id: int,
    reviewer: User,
    decision: str,
    admin_note: str = "",
) -> SubscriptionUpgradeRequest:
    item = (
        SubscriptionUpgradeRequest.query.filter_by(id=request_id)
        .with_for_update()
        .first()
    )
    if item is None:
        raise SubscriptionError("Yêu cầu nâng cấp không tồn tại.")
    if item.status != SubscriptionUpgradeRequest.STATUS_PENDING:
        raise SubscriptionError("Yêu cầu này đã được xử lý.")
    clean_note = admin_note.strip()[:1000]
    now = _utcnow()
    if decision == "approve":
        limits = decode_limits(item.requested_limits_json)
        validate_limits(limits)
        _apply_plan(
            item.project,
            item.requested_plan,
            limits,
            reviewer,
            action="REQUEST_APPROVED",
            request_id=item.id,
        )
        item.status = SubscriptionUpgradeRequest.STATUS_APPROVED
    elif decision == "reject":
        if len(clean_note) < 3:
            raise SubscriptionError("Vui lòng nhập lý do từ chối.")
        item.status = SubscriptionUpgradeRequest.STATUS_REJECTED
    else:
        raise SubscriptionError("Quyết định xử lý không hợp lệ.")
    item.admin_note = clean_note
    item.reviewed_by_user_id = reviewer.id
    item.reviewed_at = now
    db.session.commit()
    return item


def assign_plan(
    project: Project,
    actor: User,
    plan_key: str,
    custom_values: Mapping[str, Any] | None = None,
) -> ProjectSubscription:
    plan = get_plan(plan_key)
    limits = validate_limits(custom_values or {}) if plan.is_custom else plan_limits(plan)
    now = _utcnow()
    for pending in SubscriptionUpgradeRequest.query.filter_by(
        project_id=project.id,
        status=SubscriptionUpgradeRequest.STATUS_PENDING,
    ).with_for_update().all():
        pending.status = SubscriptionUpgradeRequest.STATUS_CANCELLED
        pending.reviewed_by_user_id = actor.id
        pending.reviewed_at = now
        pending.admin_note = "Đã thay thế bởi thay đổi gói trực tiếp của Admin."
    subscription = _apply_plan(
        project, plan, limits, actor, action="ADMIN_ASSIGNMENT"
    )
    db.session.commit()
    return subscription


def list_project_requests(project_id: int) -> list[SubscriptionUpgradeRequest]:
    return (
        SubscriptionUpgradeRequest.query.filter_by(project_id=project_id)
        .order_by(SubscriptionUpgradeRequest.created_at.desc())
        .all()
    )


def list_upgrade_requests() -> list[SubscriptionUpgradeRequest]:
    return SubscriptionUpgradeRequest.query.order_by(
        SubscriptionUpgradeRequest.status.desc(),
        SubscriptionUpgradeRequest.created_at.desc(),
    ).all()


def list_subscription_history(project_id: int) -> list[SubscriptionHistory]:
    return (
        SubscriptionHistory.query.filter_by(project_id=project_id)
        .order_by(SubscriptionHistory.created_at.desc(), SubscriptionHistory.id.desc())
        .all()
    )
