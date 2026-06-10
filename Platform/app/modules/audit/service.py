from flask import session

from ...db import db
from ...models import AuditLog


def record_audit(action, resource_type=None, resource_id=None, result="SUCCESS", message=None):
    log = AuditLog(
        username=session.get("user", "system"),
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        result=result,
        message=message,
    )
    db.session.add(log)
    db.session.commit()
    return log
