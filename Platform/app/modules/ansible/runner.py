from datetime import datetime
from pathlib import Path
import os
import subprocess

from flask import current_app, session

from ...db import db
from ...models import AnsibleJob, AnsibleJobLog
from .inventory import render_inventory


def append_log(job, line, level="INFO"):
    db.session.add(AnsibleJobLog(job_id=job.id, line=line.rstrip(), level=level))
    db.session.commit()


def run_playbook(cluster, playbook_name):
    job = AnsibleJob(
        cluster_id=cluster.id if cluster else None,
        playbook=playbook_name,
        status="RUNNING",
        started_at=datetime.utcnow(),
        created_by=session.get("user", "system"),
    )
    db.session.add(job)
    db.session.commit()

    inventory_path = None
    if cluster:
        inventory_path = render_inventory(cluster, current_app.config["GENERATED_INVENTORY_DIR"])

    playbook_path = Path(current_app.config["ANSIBLE_DIR"]) / "playbooks" / playbook_name
    command = ["ansible-playbook"]
    if inventory_path:
        command.extend(["-i", str(inventory_path)])
    command.append(str(playbook_path))

    append_log(job, f"$ {' '.join(command)}")
    env = os.environ.copy()
    env.setdefault("ANSIBLE_HOST_KEY_CHECKING", "False")

    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            cwd=str(Path(current_app.config["ANSIBLE_DIR"]).parent),
        )
        for line in process.stdout or []:
            append_log(job, line)
        return_code = process.wait()
        job.status = "SUCCESS" if return_code == 0 else "FAILED"
        append_log(job, f"Playbook finished with exit code {return_code}")
    except FileNotFoundError:
        job.status = "FAILED"
        append_log(job, "Không tìm thấy ansible-playbook. Hãy cài Ansible trên management server.", "ERROR")
    except Exception as exc:
        job.status = "FAILED"
        append_log(job, f"Playbook error: {exc}", "ERROR")
    finally:
        job.finished_at = datetime.utcnow()
        db.session.commit()
    return job
