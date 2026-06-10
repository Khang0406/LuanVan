from flask import Blueprint, jsonify, render_template

from ...models import AnsibleJob, AnsibleJobLog
from ..auth.routes import login_required

jobs_bp = Blueprint("jobs", __name__, url_prefix="/jobs")


@jobs_bp.route("")
@login_required
def list_jobs():
    return render_template("jobs/detail.html", jobs=AnsibleJob.query.order_by(AnsibleJob.id.desc()).all(), job=None)


@jobs_bp.route("/<int:job_id>")
@login_required
def detail(job_id):
    job = AnsibleJob.query.get_or_404(job_id)
    return render_template("jobs/detail.html", job=job, jobs=None)


@jobs_bp.route("/<int:job_id>/logs")
@login_required
def logs(job_id):
    job = AnsibleJob.query.get_or_404(job_id)
    return jsonify({
        "status": job.status,
        "logs": [log.line for log in AnsibleJobLog.query.filter_by(job_id=job.id).order_by(AnsibleJobLog.id).all()],
    })
