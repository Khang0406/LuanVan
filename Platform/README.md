# CICT Delivery Platform

Nền tảng Kubernetes tự quản cho application/container đa service: GitHub
webhook, pipeline `SOURCE → BUILD → TEST → PUSH → DEPLOY → VERIFY`, deployment
history/rollback, PVC, quota, HPA, Prometheus/Grafana, logs, alert/SMTP và audit.

## Chạy local

Cần Docker (chạy PostgreSQL + Redis):

```bash
docker compose up -d --wait
```

Tạo venv và chạy web app:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python run.py
```

Mở terminal thứ hai để pipeline queued được xử lý (Celery worker):

```bash
source .venv/bin/activate
python run_worker.py
```

Web restart không xóa queue. Nếu worker bị dừng giữa một pipeline, worker
mới đánh dấu run `Interrupted`; operator review rồi Retry để tạo run mới.

Nếu chưa có Docker, có thể chạy fallback SQLite bằng cách bỏ `DATABASE_URL` và
`REDIS_URL` trong `.env`, rồi dùng worker cũ `python -m app.pipeline_worker`.

Tài khoản development được seed cho ba role: Admin, Developer và Viewer. Không
dùng password development trong production. Production yêu cầu
`FLASK_SECRET_KEY`, password bootstrap mạnh và `COOKIE_SECURE=true`.

## Kiểm tra

```bash
python -m unittest discover -s tests -v
python -m compileall -q app tests scripts
git diff --check
kubectl kustomize k8s/platform | kubectl create --dry-run=client --validate=false -f -
```

## Tài liệu

- [Kiến trúc](docs/architecture.md)
- [Thiết kế Phase A — PostgreSQL + Redis + API](docs/phase-a-postgresql-redis-design.md)
- [Nhật ký triển khai Phase A](docs/phase-a-implementation-log.md)
- [Deployment runbook](docs/deployment-runbook.md)
- [Backup/restore](docs/backup-restore.md)
- [Security/RBAC](docs/security-model.md)
- [Nghiệm thu Giai đoạn 4](docs/phase4-production-acceptance.md)
- [So sánh Vercel](docs/vercel-comparison.md)

Không commit `.env`, `instance/`, kubeconfig, populated Kubernetes Secret,
registry credential, webhook secret hoặc backup chứa dữ liệu nhạy cảm.
