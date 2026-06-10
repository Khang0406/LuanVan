import os
import subprocess
from pathlib import Path

from flask import current_app


def kubectl(args, input_text=None):
    env = os.environ.copy()
    env["KUBECONFIG"] = current_app.config["KUBECONFIG"]
    command = ["kubectl", *args]
    result = subprocess.run(
        command,
        input=input_text,
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    return result.returncode, result.stdout, result.stderr


def kubectl_apply_file(path: Path):
    return kubectl(["apply", "-f", str(path)])
