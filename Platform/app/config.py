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
    SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "ui-prototype-secret")
    PLATFORM_HOST = os.getenv("PLATFORM_HOST", "127.0.0.1")
    PLATFORM_PORT = int(os.getenv("PLATFORM_PORT", "8000"))
