"""Cross-cutting HTTP observability helpers for the control plane."""

from __future__ import annotations

import logging
import time
import uuid

from flask import Flask, g, request


REQUEST_ID_HEADER = "X-Request-ID"
_MAX_REQUEST_ID_LENGTH = 128


def _request_id() -> str:
    supplied = request.headers.get(REQUEST_ID_HEADER, "").strip()
    if supplied and len(supplied) <= _MAX_REQUEST_ID_LENGTH:
        return supplied
    return uuid.uuid4().hex


def register_http_observability(app: Flask) -> None:
    """Add correlation IDs, latency headers and one structured access log."""

    @app.before_request
    def begin_request() -> None:
        g.request_id = _request_id()
        g.request_started_at = time.monotonic()

    @app.after_request
    def finish_request(response):
        request_id = getattr(g, "request_id", uuid.uuid4().hex)
        started_at = getattr(g, "request_started_at", time.monotonic())
        duration_ms = round((time.monotonic() - started_at) * 1000, 2)
        response.headers[REQUEST_ID_HEADER] = request_id
        response.headers["Server-Timing"] = f"app;dur={duration_ms}"
        app.logger.info(
            "http_request method=%s path=%s status=%s duration_ms=%s request_id=%s",
            request.method,
            request.path,
            response.status_code,
            duration_ms,
            request_id,
        )
        return response

    if not app.logger.handlers:
        logging.basicConfig(level=logging.INFO)
