# CICT Platform UI Prototype

Source mới trong repo hiện tại, ưu tiên **giao diện UI trước** để kịp demo với thầy. Phần backend/Ansible/Kubernetes thật được giữ dưới dạng thư mục module trống để bổ sung sau.

## Mục tiêu bản hiện tại

- Trình bày được sản phẩm sẽ làm gì: quản lý server, cài Kubernetes bằng Ansible, deploy web/microservice, CI/CD, monitoring và audit log.
- Có cấu trúc source rõ ràng để sau này bổ sung business/backend theo từng module.
- Tận dụng hướng giao diện dashboard/server/website từ niên luận cũ nhưng tổ chức lại thành platform mới.

## Chạy UI

```bash
cd Platform
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python run.py
```

Mở: `http://127.0.0.1:8000`.

## Nếu gặp lỗi `Config has no attribute BASE_DIR`

Lỗi này xuất hiện khi máy đang chạy lại source scaffold backend cũ hoặc file bị lệch phiên bản. Bản UI prototype hiện tại đã có `Config.BASE_DIR` tối thiểu để tránh lỗi này. Hãy chạy lại các bước sau:

```bash
git status
git pull
cd Platform
source .venv/bin/activate
pip install -r requirements.txt
python run.py
```

Nếu vẫn lỗi, xóa cache Python rồi chạy lại:

```bash
find . -type d -name __pycache__ -prune -exec rm -rf {} +
python run.py
```

## Cấu trúc quan trọng

```text
app/ui/                 # routes + mock data cho UI prototype
app/templates/          # giao diện dashboard, server, cluster, app, deploy, CI/CD
app/modules/            # module backend để trống, bổ sung sau
ansible/playbooks/      # playbook để trống, bổ sung sau
k8s/templates/          # manifest template để trống, bổ sung sau
docs/                   # tài liệu mô tả mô hình và demo
```

## Giai đoạn bổ sung sau

1. Bổ sung database models và repository.
2. Bổ sung Ansible inventory/playbook runner.
3. Bổ sung Kubernetes client/kubectl wrapper.
4. Bổ sung deploy/scale/logs thật.
5. Bổ sung CI/CD webhook GitHub.
