from datetime import datetime, timezone
import uuid

from flask_security import RoleMixin, UserMixin
from werkzeug.security import check_password_hash, generate_password_hash
from sqlalchemy.orm import synonym, validates

from .db import db


security_user_roles = db.Table(
    "security_user_roles",
    db.Column("user_id", db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
    db.Column("role_id", db.Integer, db.ForeignKey("security_roles.id", ondelete="CASCADE"), primary_key=True),
)


class SecurityRole(db.Model, RoleMixin):
    """Framework compatibility role; project-scoped RBAC is introduced in A.4."""

    __tablename__ = "security_roles"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), unique=True, nullable=False)
    description = db.Column(db.String(255), nullable=True)


class User(db.Model, UserMixin):
    __tablename__ = "users"
    VALID_ROLES = {"Admin", "Developer", "Viewer"}
    STATUS_PENDING = "PendingVerification"
    STATUS_ACTIVE = "Active"
    STATUS_LOCKED = "Locked"
    STATUS_DISABLED = "Disabled"
    VALID_STATUSES = {
        STATUS_PENDING,
        STATUS_ACTIVE,
        STATUS_LOCKED,
        STATUS_DISABLED,
    }

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False, index=True)
    email = db.Column(db.String(254), unique=True, nullable=True, index=True)
    password_hash = db.Column(db.String(256), nullable=False)
    # Flask-Security's datastore contract names this attribute ``password``.
    # A synonym keeps every existing Werkzeug hash and database column intact.
    password = synonym("password_hash")
    fs_uniquifier = db.Column(
        db.String(64), unique=True, nullable=False, default=lambda: uuid.uuid4().hex
    )
    active = db.Column(db.Boolean, nullable=False, default=True)
    confirmed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    role = db.Column(db.String(16), nullable=False, default="Developer")
    status = db.Column(db.String(32), nullable=False, default=STATUS_ACTIVE)
    email_verified_at = db.Column(db.DateTime(timezone=True), nullable=True)
    status_changed_at = db.Column(
        db.DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc)
    )
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    verification_tokens = db.relationship(
        "EmailVerificationToken",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    roles = db.relationship(
        "SecurityRole",
        secondary=security_user_roles,
        lazy="selectin",
    )

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    @validates("role")
    def validate_role(self, _key: str, role: str) -> str:
        if role not in self.VALID_ROLES:
            raise ValueError(f"Role không hợp lệ: {role}")
        return role

    @validates("status")
    def validate_status(self, _key: str, status: str) -> str:
        if status not in self.VALID_STATUSES:
            raise ValueError(f"Trạng thái tài khoản không hợp lệ: {status}")
        self.active = status == self.STATUS_ACTIVE
        return status

    @validates("email")
    def normalize_email(self, _key: str, email: str | None) -> str | None:
        return email.strip().lower() if email else None

    @property
    def is_admin(self) -> bool:
        return self.role == "Admin"

    def __repr__(self) -> str:
        return f"<User {self.username} ({self.role})>"


class EmailVerificationToken(db.Model):
    __tablename__ = "email_verification_tokens"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token_hash = db.Column(db.String(64), unique=True, nullable=False, index=True)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False, index=True)
    used_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
    request_ip = db.Column(db.String(45), nullable=False, default="")

    user = db.relationship("User", back_populates="verification_tokens")

    @property
    def is_used(self) -> bool:
        return self.used_at is not None
