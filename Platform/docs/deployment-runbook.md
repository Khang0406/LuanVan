# Production deployment runbook

## Điều kiện trước khi chạy

1. Xác nhận đúng kube-context: `kubectl config current-context`.
2. Xác nhận namespace Platform là `platform-system`; xác nhận application mục
   tiêu bằng ID và namespace trong UI/SQLite.
3. Ghi nhận Map và demo-nginx: Deployment ready, NodePort, PVC và HTTP status.
4. Không apply nếu manifest còn placeholder `PLATFORM_ORG`, `PLATFORM_IMAGE_TAG`,
   `BUILD_WORKER_PRIVATE_IP` hoặc `REPLACE_`.
5. Tạo `platform-runtime`, `platform-builder-ssh` ngoài Git. Không in nội dung
   Secret trong terminal/log nghiệm thu.

## Render và kiểm tra

```bash
kubectl kustomize k8s/platform > /tmp/platform-phase4.yaml
kubectl apply --dry-run=server -f /tmp/platform-phase4.yaml
```

Kiểm tra output render có `replicas: 1`, strategy `Recreate`, PVC
`platform-data`, hai container `platform` và `pipeline-worker`, security context,
requests/limits và probes. Xóa file `/tmp` sau khi review nếu nó được ghép cùng
Secret ở quy trình khác.

## Triển khai

```bash
kubectl apply -k k8s/platform
kubectl -n platform-system rollout status deployment/platform --timeout=180s
kubectl -n platform-system get pod,svc,pvc
```

Không scale Deployment trên một replica. Web và worker dùng chung SQLite/PVC.
Không thay NodePort/PVC của Map hoặc demo-nginx.

## Smoke

```bash
curl --fail https://PLATFORM_HOST/healthz
curl --fail https://PLATFORM_HOST/readyz
kubectl -n platform-system logs deployment/platform -c platform --tail=100
kubectl -n platform-system logs deployment/platform -c pipeline-worker --tail=100
```

Log chỉ được xem sau khi tránh các lệnh dump environment/Secret. Xác nhận queue:
tạo pipeline test cô lập, restart riêng web container/Pod theo cửa sổ nghiệm
thu, task `Queued` còn tồn tại và worker claim đúng một lần.

## Graceful shutdown và sự cố worker

- SIGTERM làm worker ngừng nhận task mới và hoàn thành task hiện tại trong
  termination grace nếu đủ thời gian.
- Nếu worker bị kill giữa stage, lần worker startup kế tiếp chuyển run đó thành
  `Interrupted`. Operator kiểm tra tác động stage rồi nhấn Retry; hệ thống không
  tự deploy lại một cách mù quáng.
- Run `Queued` không bị web restart tác động.

## Rollback Platform

Rollback image Platform không được rollback PVC. Dùng image tag/digest đã biết,
render/dry-run lại rồi apply. Nếu schema/data hỏng, dùng quy trình
[backup-restore.md](backup-restore.md). Rollback application phải theo application
scope và deployment record pin digest.

## SMTP production

Điền `SMTP_HOST`, `SMTP_PORT`, `SMTP_FROM`, `SMTP_TO` và tùy chọn
`SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_STARTTLS`/`SMTP_SSL` trong Kubernetes
Secret. Chạy một alert `Firing`, sau đó `Resolved`; xác nhận đúng hai email và
không có email lặp. Nếu không có credential, ghi blocker môi trường và dùng bằng
chứng SMTP receiver cô lập Giai đoạn 3.
