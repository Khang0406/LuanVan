"""Authenticated GitHub push webhook endpoint."""

from __future__ import annotations

import hashlib
import hmac
import re

from flask import Blueprint, jsonify, request

from app.delivery_store import claim_webhook_delivery
from app.modules.applications.service import add_activity, find_application, save_application
from app.modules.audit.service import record_audit
from app.modules.pipeline.engine import trigger_pipeline
from app.webhook_secrets import get_webhook_secret


github_webhook_bp = Blueprint("github_webhook", __name__, url_prefix="/webhooks/github")


def verify_github_signature(body: bytes, signature_header: str, secret: str) -> bool:
    if not secret or not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)


@github_webhook_bp.post("/<application_id>")
def receive(application_id: str):
    application = find_application(application_id)
    if not application or application.get("source_type") != "github":
        return jsonify({"error": "Application not found"}), 404

    body = request.get_data(cache=True)
    secret = get_webhook_secret(
        application.get("webhook_secret_ref", ""), application_id
    )
    if not verify_github_signature(
        body, request.headers.get("X-Hub-Signature-256", ""), secret
    ):
        record_audit(
            "GITHUB_WEBHOOK", application_id, "FAILED", "Invalid webhook signature.",
            user="github", metadata={"event": request.headers.get("X-GitHub-Event", "")},
        )
        return jsonify({"error": "Invalid signature"}), 401

    if request.headers.get("X-GitHub-Event") != "push":
        return jsonify({"status": "ignored", "reason": "Only push events are supported"}), 202
    delivery_id = request.headers.get("X-GitHub-Delivery", "").strip()
    if not delivery_id:
        return jsonify({"error": "Missing X-GitHub-Delivery"}), 400
    payload = request.get_json(silent=True) or {}
    branch = str(payload.get("ref", "")).removeprefix("refs/heads/")
    configured_branch = application.get("default_branch", "main")
    if branch != configured_branch:
        return jsonify({
            "status": "ignored",
            "reason": f"Branch {branch!r} does not match {configured_branch!r}",
        }), 202
    commit_sha = str(payload.get("after", "")).lower()
    if not re.fullmatch(r"[0-9a-f]{40}", commit_sha):
        return jsonify({"error": "Invalid commit SHA"}), 400
    head_commit = payload.get("head_commit") or {}
    author = (head_commit.get("author") or {}).get("name", "")
    message = head_commit.get("message", "")
    accepted = claim_webhook_delivery(
        delivery_id, application_id, "push", commit_sha,
        {"branch": branch, "commit_author": author, "commit_message": message},
    )
    if not accepted:
        return jsonify({"status": "duplicate", "delivery_id": delivery_id}), 200

    try:
        run = trigger_pipeline(
            application_id,
            actor=None,
            trigger_type="GitPush",
            requested_ref=commit_sha,
            commit_author=author,
            commit_message=message,
        )
    except ValueError as exc:
        record_audit(
            "GITHUB_WEBHOOK", application_id, "FAILED", str(exc), user="github",
            metadata={"delivery_id": delivery_id, "commit_sha": commit_sha},
        )
        return jsonify({"error": str(exc), "delivery_id": delivery_id}), 409

    add_activity(
        application, "GIT_PUSH",
        f"GitHub push {commit_sha[:12]} by {author}: {message}", "Running",
    )
    save_application(application)
    record_audit(
        "GITHUB_WEBHOOK", application_id, "SUCCESS",
        f"Triggered pipeline {run['id']} from GitHub push.", user="github",
        metadata={"delivery_id": delivery_id, "commit_sha": commit_sha, "branch": branch},
    )
    return jsonify({"status": "accepted", "pipeline_id": run["id"]}), 202
