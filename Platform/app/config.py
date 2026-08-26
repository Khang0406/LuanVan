from pathlib import Path
import os

BASE_DIR = Path(__file__).resolve().parents[1]


class Config:
    """Minimal configuration for the UI prototype.

    The previous backend scaffold expected Config.BASE_DIR. Keeping it here makes
    the UI app safe to run even when callers still pass a config class while the
    backend modules remain placeholders.
    """

    BASE_DIR = BASE_DIR
    PLATFORM_ENV = os.getenv("PLATFORM_ENV", "development").strip().lower()
    SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "ui-prototype-secret")
    PLATFORM_HOST = os.getenv("PLATFORM_HOST", "127.0.0.1")
    PLATFORM_PORT = int(os.getenv("PLATFORM_PORT", "8000"))
    TRUST_PROXY_COUNT = max(0, int(os.getenv("TRUST_PROXY_COUNT", "0")))
    PIPELINE_MAX_CONCURRENT = max(
        1, int(os.getenv("PIPELINE_MAX_CONCURRENT", "1"))
    )
    PIPELINE_TASK_SOFT_TIME_LIMIT = max(
        60, int(os.getenv("PIPELINE_TASK_SOFT_TIME_LIMIT", "3300"))
    )
    PIPELINE_TASK_TIME_LIMIT = max(
        PIPELINE_TASK_SOFT_TIME_LIMIT + 30,
        int(os.getenv("PIPELINE_TASK_TIME_LIMIT", "3600")),
    )

    SQLALCHEMY_DATABASE_URI = os.getenv(
        "DATABASE_URL",
        "sqlite:///" + str(BASE_DIR / "instance" / "app.db"),
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = os.getenv("COOKIE_SECURE", "false").lower() == "true"
    REMEMBER_COOKIE_SECURE = SESSION_COOKIE_SECURE

    # Prometheus & Grafana — monitoring stack URLs
    PROMETHEUS_URL = os.getenv("PROMETHEUS_URL", "http://localhost:30900")
    GRAFANA_URL = os.getenv("GRAFANA_URL", "http://localhost:30300")
    GRAFANA_API_KEY = os.getenv("GRAFANA_API_KEY", "")

    @classmethod
    def validate_production(cls) -> None:
        """Reject unsafe defaults when the process is explicitly production."""
        if str(cls.PLATFORM_ENV).lower() != "production":
            return
        if not cls.SECRET_KEY or cls.SECRET_KEY in {
            "ui-prototype-secret",
            "change-me",
        }:
            raise RuntimeError(
                "FLASK_SECRET_KEY must be set to a strong non-default value "
                "when PLATFORM_ENV=production."
            )
        if not cls.SESSION_COOKIE_SECURE or not cls.REMEMBER_COOKIE_SECURE:
            raise RuntimeError(
                "COOKIE_SECURE must be true when PLATFORM_ENV=production."
            )
        database_url = str(cls.SQLALCHEMY_DATABASE_URI or "").lower()
        if not database_url.startswith(("postgresql://", "postgresql+")):
            raise RuntimeError(
                "DATABASE_URL must use PostgreSQL when PLATFORM_ENV=production."
            )
        if not os.getenv("REDIS_URL", "").strip():
            raise RuntimeError(
                "REDIS_URL must be configured when PLATFORM_ENV=production."
            )
        api_token = os.getenv("PLATFORM_API_TOKEN", "")
        if api_token and len(api_token) < 32:
            raise RuntimeError(
                "PLATFORM_API_TOKEN must contain at least 32 characters in production."
            )
