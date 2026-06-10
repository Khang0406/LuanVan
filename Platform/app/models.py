from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash

from .db import db


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(30), nullable=False, default="ADMIN")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)


class Server(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    ip = db.Column(db.String(120), nullable=False)
    ssh_port = db.Column(db.Integer, default=22)
    ssh_user = db.Column(db.String(120), nullable=False)
    ssh_password = db.Column(db.String(255))
    ssh_key_path = db.Column(db.String(255))
    os = db.Column(db.String(120), default="Ubuntu")
    status = db.Column(db.String(40), default="UNKNOWN")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Cluster(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text)
    status = db.Column(db.String(40), default="DRAFT")
    kubeconfig_path = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    nodes = db.relationship("ClusterNode", backref="cluster", cascade="all, delete-orphan")


class ClusterNode(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    cluster_id = db.Column(db.Integer, db.ForeignKey("cluster.id"), nullable=False)
    server_id = db.Column(db.Integer, db.ForeignKey("server.id"), nullable=False)
    role = db.Column(db.String(30), nullable=False)  # master / worker
    k8s_status = db.Column(db.String(40), default="UNKNOWN")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    server = db.relationship("Server")


class AnsibleJob(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    cluster_id = db.Column(db.Integer, db.ForeignKey("cluster.id"))
    playbook = db.Column(db.String(255), nullable=False)
    status = db.Column(db.String(40), default="PENDING")
    started_at = db.Column(db.DateTime)
    finished_at = db.Column(db.DateTime)
    created_by = db.Column(db.String(80))
    cluster = db.relationship("Cluster")
    logs = db.relationship("AnsibleJobLog", backref="job", cascade="all, delete-orphan")


class AnsibleJobLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.Integer, db.ForeignKey("ansible_job.id"), nullable=False)
    line = db.Column(db.Text, nullable=False)
    level = db.Column(db.String(20), default="INFO")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Application(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text)
    namespace = db.Column(db.String(120), nullable=False)
    owner = db.Column(db.String(80))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    services = db.relationship("AppService", backref="application", cascade="all, delete-orphan")


class AppService(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    application_id = db.Column(db.Integer, db.ForeignKey("application.id"), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    image = db.Column(db.String(255), nullable=False)
    port = db.Column(db.Integer, nullable=False, default=80)
    replicas = db.Column(db.Integer, default=1)
    service_type = db.Column(db.String(40), default="NodePort")
    node_port = db.Column(db.Integer)
    status = db.Column(db.String(40), default="DRAFT")
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    deployments = db.relationship("DeploymentRecord", backref="service", cascade="all, delete-orphan")


class DeploymentRecord(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    service_id = db.Column(db.Integer, db.ForeignKey("app_service.id"), nullable=False)
    image = db.Column(db.String(255), nullable=False)
    replicas = db.Column(db.Integer, default=1)
    status = db.Column(db.String(40), default="PENDING")
    endpoint_url = db.Column(db.String(255))
    message = db.Column(db.Text)
    started_at = db.Column(db.DateTime, default=datetime.utcnow)
    finished_at = db.Column(db.DateTime)


class AuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80))
    action = db.Column(db.String(120), nullable=False)
    resource_type = db.Column(db.String(80))
    resource_id = db.Column(db.Integer)
    result = db.Column(db.String(40), default="SUCCESS")
    message = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
