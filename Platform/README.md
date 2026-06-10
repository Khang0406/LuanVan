# CICT Platform

Source demo mới cho luận văn: nền tảng web tự động hóa cài đặt K3s/Kubernetes bằng Ansible và triển khai ứng dụng web/microservices.

## Chạy nhanh

```bash
cd Platform
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python run.py
```

Tài khoản demo mặc định: `admin` / `admin`.

## Kịch bản demo chính

1. Admin thêm server vào Server Inventory.
2. Admin tạo cluster và chọn node master/worker.
3. Admin chạy Ansible ping để kiểm tra SSH.
4. Admin chạy playbook cài K3s cluster.
5. Admin refresh node status bằng `kubectl get nodes`.
6. Developer tạo application/service từ Docker image.
7. Developer deploy service lên Kubernetes, xem pod status/logs và scale replicas.

## Lưu ý

- Đây là MVP demo trong thời gian ngắn, ưu tiên chạy được luồng end-to-end.
- Production cần chuyển SQLite sang PostgreSQL, mã hóa secret/SSH credential, bổ sung queue bất đồng bộ, RBAC đầy đủ và backup.
