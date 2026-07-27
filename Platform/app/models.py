from datetime import datetime, timezone

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash
from sqlalchemy.orm import validates

from .db import db


class User(UserMixin, db.Model):
    __tablename__ = "users"
    VALID_ROLES = {"Admin", "Developer"}

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(64), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(16), nullable=False, default="Developer")
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    @validates("role")
    def validate_role(self, _key: str, role: str) -> str:
        if role not in self.VALID_ROLES:
            raise ValueError(f"Role không hợp lệ: {role}")
        return role

    @property
    def is_admin(self) -> bool:
        return self.role == "Admin"

    def __repr__(self) -> str:
        return f"<User {self.username} ({self.role})>"
