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

rbac_role_permissions = db.Table(
    "rbac_role_permissions",
    db.Column(
        "role_id",
        db.Integer,
        db.ForeignKey("rbac_roles.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    db.Column(
        "permission_id",
        db.Integer,
        db.ForeignKey("rbac_permissions.id", ondelete="CASCADE"),
        primary_key=True,
    ),
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
        db.String(64), unique=True, nullable=False, index=True,
        default=lambda: uuid.uuid4().hex,
    )
    active = db.Column(db.Boolean, nullable=False, default=True)
    confirmed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    role = db.Column(db.String(16), nullable=False, default="Developer")
    status = db.Column(db.String(32), nullable=False, default=STATUS_ACTIVE)
    email_verified_at = db.Column(db.DateTime(timezone=True), nullable=True)
    failed_login_count = db.Column(db.Integer, nullable=False, default=0)
    failed_login_window_started_at = db.Column(
        db.DateTime(timezone=True), nullable=True
    )
    locked_until = db.Column(db.DateTime(timezone=True), nullable=True)
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
    password_reset_tokens = db.relationship(
        "PasswordResetToken",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    project_memberships = db.relationship(
        "ProjectMembership",
        foreign_keys="ProjectMembership.user_id",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    api_tokens = db.relationship(
        "ApiToken",
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


class PasswordResetToken(db.Model):
    __tablename__ = "password_reset_tokens"

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

    user = db.relationship("User", back_populates="password_reset_tokens")

    @property
    def is_used(self) -> bool:
        return self.used_at is not None


class Project(db.Model):
    __tablename__ = "projects"

    STATUS_ACTIVE = "Active"
    STATUS_ARCHIVED = "Archived"
    VALID_STATUSES = {STATUS_ACTIVE, STATUS_ARCHIVED}

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    slug = db.Column(db.String(80), unique=True, nullable=False, index=True)
    description = db.Column(db.String(500), nullable=False, default="")
    status = db.Column(db.String(20), nullable=False, default=STATUS_ACTIVE)
    owner_user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    owner = db.relationship("User", foreign_keys=[owner_user_id])
    memberships = db.relationship(
        "ProjectMembership",
        back_populates="project",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    api_tokens = db.relationship(
        "ApiToken",
        back_populates="project",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    subscription = db.relationship(
        "ProjectSubscription",
        back_populates="project",
        cascade="all, delete-orphan",
        passive_deletes=True,
        uselist=False,
    )
    subscription_requests = db.relationship(
        "SubscriptionUpgradeRequest",
        back_populates="project",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    subscription_history = db.relationship(
        "SubscriptionHistory",
        back_populates="project",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    @validates("status")
    def validate_status(self, _key: str, status: str) -> str:
        if status not in self.VALID_STATUSES:
            raise ValueError(f"Trạng thái project không hợp lệ: {status}")
        return status


class RBACPermission(db.Model):
    __tablename__ = "rbac_permissions"

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(80), unique=True, nullable=False, index=True)
    description = db.Column(db.String(255), nullable=False, default="")


class RBACRole(db.Model):
    __tablename__ = "rbac_roles"

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(40), unique=True, nullable=False, index=True)
    name = db.Column(db.String(80), unique=True, nullable=False)
    description = db.Column(db.String(255), nullable=False, default="")
    is_system = db.Column(db.Boolean, nullable=False, default=True)

    permissions = db.relationship(
        "RBACPermission",
        secondary=rbac_role_permissions,
        lazy="selectin",
    )


class ProjectMembership(db.Model):
    __tablename__ = "project_memberships"
    __table_args__ = (
        db.UniqueConstraint("project_id", "user_id", name="uq_project_membership"),
    )

    STATUS_ACTIVE = "Active"
    STATUS_DISABLED = "Disabled"
    VALID_STATUSES = {STATUS_ACTIVE, STATUS_DISABLED}

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(
        db.Integer,
        db.ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    status = db.Column(db.String(20), nullable=False, default=STATUS_ACTIVE)
    invited_by_user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    role_id = db.Column(
        db.Integer,
        db.ForeignKey("rbac_roles.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    project = db.relationship("Project", back_populates="memberships")
    user = db.relationship(
        "User", foreign_keys=[user_id], back_populates="project_memberships"
    )
    invited_by = db.relationship("User", foreign_keys=[invited_by_user_id])
    role = db.relationship("RBACRole", lazy="joined")

    @validates("status")
    def validate_status(self, _key: str, status: str) -> str:
        if status not in self.VALID_STATUSES:
            raise ValueError(f"Trạng thái membership không hợp lệ: {status}")
        return status


class ApiToken(db.Model):
    """Hashed, revocable credential constrained to one user and project."""

    __tablename__ = "api_tokens"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    token_prefix = db.Column(db.String(20), unique=True, nullable=False, index=True)
    token_hash = db.Column(db.String(64), unique=True, nullable=False, index=True)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_id = db.Column(
        db.Integer,
        db.ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    scopes_json = db.Column(db.Text, nullable=False, default="[]")
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False, index=True)
    revoked_at = db.Column(db.DateTime(timezone=True), nullable=True, index=True)
    last_used_at = db.Column(db.DateTime(timezone=True), nullable=True)
    last_used_ip = db.Column(db.String(45), nullable=False, default="")
    rate_limit_per_minute = db.Column(db.Integer, nullable=False, default=60)
    rate_window_started_at = db.Column(db.DateTime(timezone=True), nullable=True)
    rate_window_count = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

    user = db.relationship("User", back_populates="api_tokens")
    project = db.relationship("Project", back_populates="api_tokens")

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None

    @property
    def is_expired(self) -> bool:
        expires_at = self.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        return expires_at <= datetime.now(timezone.utc)


class SubscriptionPlan(db.Model):
    __tablename__ = "subscription_plans"

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(40), unique=True, nullable=False, index=True)
    name = db.Column(db.String(80), unique=True, nullable=False)
    description = db.Column(db.String(255), nullable=False, default="")
    limits_json = db.Column(db.Text, nullable=False, default="{}")
    is_system = db.Column(db.Boolean, nullable=False, default=True)
    is_custom = db.Column(db.Boolean, nullable=False, default=False)
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )


class ProjectSubscription(db.Model):
    __tablename__ = "project_subscriptions"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(
        db.Integer, db.ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False, unique=True, index=True,
    )
    plan_id = db.Column(
        db.Integer, db.ForeignKey("subscription_plans.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )
    effective_limits_json = db.Column(db.Text, nullable=False, default="{}")
    assigned_by_user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at = db.Column(
        db.DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    project = db.relationship("Project", back_populates="subscription")
    plan = db.relationship("SubscriptionPlan", lazy="joined")
    assigned_by = db.relationship("User", foreign_keys=[assigned_by_user_id])


class SubscriptionUpgradeRequest(db.Model):
    __tablename__ = "subscription_upgrade_requests"

    STATUS_PENDING = "Pending"
    STATUS_APPROVED = "Approved"
    STATUS_REJECTED = "Rejected"
    STATUS_CANCELLED = "Cancelled"
    VALID_STATUSES = {
        STATUS_PENDING, STATUS_APPROVED, STATUS_REJECTED, STATUS_CANCELLED,
    }

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(
        db.Integer, db.ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    requested_plan_id = db.Column(
        db.Integer, db.ForeignKey("subscription_plans.id", ondelete="RESTRICT"),
        nullable=False, index=True,
    )
    requested_limits_json = db.Column(db.Text, nullable=False, default="{}")
    reason = db.Column(db.String(1000), nullable=False, default="")
    status = db.Column(db.String(20), nullable=False, default=STATUS_PENDING, index=True)
    requested_by_user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )
    reviewed_by_user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )
    admin_note = db.Column(db.String(1000), nullable=False, default="")
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc), index=True,
    )
    reviewed_at = db.Column(db.DateTime(timezone=True), nullable=True)

    project = db.relationship("Project", back_populates="subscription_requests")
    requested_plan = db.relationship("SubscriptionPlan", lazy="joined")
    requested_by = db.relationship("User", foreign_keys=[requested_by_user_id])
    reviewed_by = db.relationship("User", foreign_keys=[reviewed_by_user_id])

    @validates("status")
    def validate_status(self, _key: str, status: str) -> str:
        if status not in self.VALID_STATUSES:
            raise ValueError(f"Trạng thái yêu cầu gói không hợp lệ: {status}")
        return status


class SubscriptionHistory(db.Model):
    __tablename__ = "subscription_history"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(
        db.Integer, db.ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    from_plan_id = db.Column(
        db.Integer, db.ForeignKey("subscription_plans.id", ondelete="RESTRICT"),
        nullable=True,
    )
    to_plan_id = db.Column(
        db.Integer, db.ForeignKey("subscription_plans.id", ondelete="RESTRICT"),
        nullable=False,
    )
    from_limits_json = db.Column(db.Text, nullable=False, default="{}")
    to_limits_json = db.Column(db.Text, nullable=False, default="{}")
    action = db.Column(db.String(40), nullable=False)
    actor_user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True,
    )
    request_id = db.Column(
        db.Integer,
        db.ForeignKey("subscription_upgrade_requests.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False,
        default=lambda: datetime.now(timezone.utc), index=True,
    )

    project = db.relationship("Project", back_populates="subscription_history")
    from_plan = db.relationship("SubscriptionPlan", foreign_keys=[from_plan_id])
    to_plan = db.relationship("SubscriptionPlan", foreign_keys=[to_plan_id])
    actor = db.relationship("User", foreign_keys=[actor_user_id])
    request = db.relationship("SubscriptionUpgradeRequest", foreign_keys=[request_id])
