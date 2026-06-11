from pathlib import Path
import os

BASE_DIR = Path(__file__).resolve().parents[1]

class Config:
    BASE_DIR = BASE_DIR

    SECRET_KEY = os.getenv(
        "FLASK_SECRET_KEY",
        "dev-platform-secret"
    )

    SQLALCHEMY_DATABASE_URI = os.getenv(
        "DATABASE_URL",
        f"sqlite:///{BASE_DIR / 'instance' / 'platform.db'}",
    )

    SQLALCHEMY_TRACK_MODIFICATIONS = False

    PLATFORM_HOST = os.getenv(
        "PLATFORM_HOST",
        "127.0.0.1"
    )

    PLATFORM_PORT = int(
        os.getenv("PLATFORM_PORT", "8000")
    )

    ANSIBLE_DIR = BASE_DIR / "ansible"

    GENERATED_INVENTORY_DIR = (
        ANSIBLE_DIR / "inventories" / "generated"
    )

    K8S_TEMPLATE_DIR = (
        BASE_DIR / "k8s" / "templates"
    )

    K8S_GENERATED_DIR = (
        BASE_DIR / "k8s" / "generated"
    )

    KUBECONFIG = os.getenv(
        "KUBECONFIG",
        str(Path.home() / ".kube" / "config")
    )