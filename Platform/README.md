# CICT Delivery Platform

Nền tảng Kubernetes tự quản cho application/container đa service: GitHub
webhook, pipeline `SOURCE → BUILD → TEST → PUSH → DEPLOY → VERIFY`, deployment
history/rollback, PVC, quota, HPA, Prometheus/Grafana, logs, alert/SMTP và audit.

## Chạy local

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python run.py
```

Mở terminal thứ hai để pipeline queued được xử lý:

```bash
source .venv/bin/activate
python -m app.pipeline_worker
```

Web restart không xóa SQLite queue. Nếu worker bị dừng giữa một pipeline, worker
mới đánh dấu run `Interrupted`; operator review rồi Retry để tạo run mới.

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
- [Deployment runbook](docs/deployment-runbook.md)
- [Backup/restore](docs/backup-restore.md)
- [Security/RBAC](docs/security-model.md)
- [Nghiệm thu Giai đoạn 4](docs/phase4-production-acceptance.md)
- [So sánh Vercel](docs/vercel-comparison.md)

Không commit `.env`, `instance/`, kubeconfig, populated Kubernetes Secret,
registry credential, webhook secret hoặc backup chứa dữ liệu nhạy cảm.
