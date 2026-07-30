# Triển khai Platform lên cloud

## Kiến trúc được chọn

Giai đoạn 1 dùng ba vùng trách nhiệm tách biệt:

1. **Platform Web/API** chạy bằng một pod Gunicorn trên K3s. Pod không có
   Docker socket và chỉ dùng tối đa 1 CPU/1 GiB RAM.
2. **Build Worker** là một VM riêng có Docker Engine. Platform kết nối qua SSH
   private network để build, chạy test container và push image.
3. **K3s runtime** chạy application từ immutable image đã push lên registry.

Docker xác nhận `DOCKER_HOST=ssh://user@host` là cách được hỗ trợ để điều khiển
remote engine qua SSH. Khóa SSH này có quyền tương đương root trên riêng Build
Worker, vì vậy VM phải chuyên dụng, chỉ mở cổng 22 từ private IP/CIDR của K3s,
và không được dùng làm runtime node. Xem
[Protect the Docker daemon socket](https://docs.docker.com/engine/security/protect-access/).

Không dùng chung Docker daemon với pod Platform. Build có thể dùng nhiều tài
nguyên nhưng chỉ ảnh hưởng VM Build Worker; K3s và giao diện vẫn hoạt động.
`PIPELINE_MAX_CONCURRENT=1` giới hạn một build toàn hệ thống. Test container bị
giới hạn 512 MiB, 1 CPU và 256 PID.

SQLite hiện là source of truth cho Application/Pipeline/Deployment/Job/Audit.
Nó nằm trên PVC `ReadWriteOnce`, Deployment dùng `replicas: 1` và strategy
`Recreate`. Web và pipeline worker là hai process/container riêng trong cùng
Pod, dùng chung SQLite/PVC; không scale Pod trên một replica.
Không tăng replicas. Khi cần HA/scale ngang, phải chuyển sang PostgreSQL và
worker queue độc lập trước.

## Registry

Phương án ưu tiên là một **Docker Hub organization do Platform quản lý**:

```text
docker.io/<organization>/<application>-<service>:<commit-sha>
```

Developer không push bằng tài khoản cá nhân ở mỗi pipeline. Admin lưu một
credential automation tại **CI/CD → Platform Container Registry**; mọi
application mặc định tự động kế thừa và Platform đăng nhập bằng
`--password-stdin`. Với Docker Team/Business, nên dùng
[Organization Access Token](https://docs.docker.com/enterprise/security/access-tokens/)
giới hạn quyền push theo repository. Với tài khoản miễn phí, dùng PAT riêng cho
CI/CD, không dùng mật khẩu tài khoản.

Image private cần một Kubernetes `imagePullSecret` chỉ có quyền pull. Credential
push không được chép vào manifest, Pipeline Log, Job Log hay Audit Log.

## Yêu cầu cloud tối thiểu

- K3s: control plane 2 vCPU/4 GiB và worker 2 vCPU/4 GiB trở lên.
- Build Worker: 4 vCPU/8 GiB, disk SSD 50 GiB trở lên.
- Cả hai ở cùng VPC/private network.
- Inbound Internet chỉ vào load balancer/Traefik cổng 80/443.
- Build Worker chỉ nhận SSH cổng 22 từ K3s; không mở 2375/2376 công khai.
- DNS cho `platform.<domain>` và TLS bằng cert-manager hoặc load balancer.

## 1. Chuẩn bị Build Worker

Tạo cặp khóa riêng cho Platform. Không đặt private key trong repository:

```bash
ssh-keygen -t ed25519 -f /secure/path/platform-builder -C platform-builder
cp ansible/inventories/platform-builder.example.ini /tmp/platform-builder.ini
cp ansible/group_vars/platform_builders.example.yml \
  ansible/group_vars/platform_builders.yml
```

Điền private IP của VM vào inventory và public key vào
`ansible/group_vars/platform_builders.yml`, sau đó:

```bash
ANSIBLE_LOCAL_TEMP=/tmp/platform-ansible-local \
ansible-playbook \
  -i /tmp/platform-builder.ini \
  ansible/playbooks/install_platform_build_worker.yml
```

Đối chiếu fingerprint SSH qua console/provider trước khi tạo `known_hosts`.
Không tin mù quáng kết quả `ssh-keyscan`.

Kiểm tra từ một máy trong private network:

```bash
DOCKER_HOST=ssh://platform-builder@<private-ip> \
DOCKER_SSH_COMMAND="ssh -i /secure/path/platform-builder -o IdentitiesOnly=yes" \
docker version
```

## 2. Build và push image Platform

Dùng tag bất biến, ví dụ Git commit SHA:

```bash
docker build -t docker.io/<organization>/delivery-platform:<git-sha> .
docker login --username <automation-user>
docker push docker.io/<organization>/delivery-platform:<git-sha>
```

Image `delivery-platform:phase1-cloud` đã được build và smoke-test local. Khi
triển khai thật phải thay `<organization>` và `<git-sha>`; không dùng `latest`.

## 3. Backup dữ liệu hiện tại

Script dùng SQLite backup API nên tạo snapshot nhất quán, kể cả khi database ở
WAL mode. Thư mục backup có thể chứa registry/webhook secret; lưu nó trên ổ mã
hóa và không commit:

```bash
python scripts/backup_platform_data.py \
  --source instance \
  --output /secure/backup
```

Giữ cả `app.db`, `registry_credentials.json` và `webhook_secrets.json` nếu có.
Không cần copy `app.db-wal`/`app.db-shm`.

## 4. Điền cấu hình không nhạy cảm

Trước khi apply:

- Thay `BUILD_WORKER_PRIVATE_IP` trong `k8s/platform/configmap.yaml`.
- Thay `PLATFORM_ORG` và `PLATFORM_IMAGE_TAG` trong
  `k8s/platform/deployment.yaml`.
- Thay URL Prometheus/Grafana nếu stack dùng namespace/service khác.
- Giữ `replicas: 1`, `PIPELINE_MAX_CONCURRENT: "1"`.

Kiểm tra không còn placeholder:

```bash
grep -R "REPLACE_WITH\\|BUILD_WORKER_PRIVATE_IP\\|PLATFORM_ORG\\|PLATFORM_IMAGE_TAG" \
  k8s/platform
kubectl kustomize k8s/platform >/tmp/platform-rendered.yaml
```

Các placeholder trong file `*.example.yaml` là bình thường; rendered manifest
không được còn placeholder.

## 5. Tạo namespace, PVC và nạp dữ liệu

```bash
kubectl apply -f k8s/platform/namespace.yaml
kubectl apply -f k8s/platform/pvc.yaml
kubectl apply -f k8s/platform/data-loader.example.yaml
kubectl wait -n platform-system \
  --for=condition=Ready pod/platform-data-loader --timeout=120s
```

Copy từng file từ thư mục backup đã tạo:

```bash
kubectl cp /secure/backup/<snapshot>/app.db \
  platform-system/platform-data-loader:/var/lib/platform/app.db
kubectl cp /secure/backup/<snapshot>/registry_credentials.json \
  platform-system/platform-data-loader:/var/lib/platform/registry_credentials.json
```

Chỉ copy `webhook_secrets.json` nếu file tồn tại. Sau khi đối chiếu kích thước,
xóa pod loader:

```bash
kubectl delete pod -n platform-system platform-data-loader
```

Nếu đây là cài đặt mới, bỏ qua data loader; Platform sẽ tạo database và import
legacy JSON idempotently lần đầu.

## 6. Tạo Kubernetes Secret an toàn

Tạo file tạm bên ngoài repository với quyền `0600`, dựa trên
`k8s/platform/secret.example.yaml`. Ít nhất phải thay Flask secret, admin
password và developer password khi database cũ còn `dev/dev123`.

Tạo SSH Secret trực tiếp từ file; command line không chứa nội dung private key:

```bash
kubectl create secret generic platform-builder-ssh \
  -n platform-system \
  --from-file=id_ed25519=/secure/path/platform-builder \
  --from-file=known_hosts=/secure/path/platform-builder-known-hosts
kubectl apply -f /secure/path/platform-runtime-secret.yaml
```

Production startup tự xoay `admin/admin123` và `dev/dev123` bằng hai giá trị
Secret. Nếu credential thiếu hoặc dưới 12 ký tự, pod từ chối khởi động. Sau lần
xoay đầu, có thể bỏ `PLATFORM_DEV_PASSWORD`; admin password vẫn cần cho database
mới.

## 7. Deploy và verify

```bash
kubectl apply -k k8s/platform
kubectl rollout status -n platform-system deployment/platform --timeout=180s
kubectl get pod,svc,pvc -n platform-system
kubectl logs -n platform-system deployment/platform --tail=200
kubectl port-forward -n platform-system service/platform 8000:80
```

Từ máy local:

```bash
curl --fail http://127.0.0.1:8000/healthz
curl --fail http://127.0.0.1:8000/readyz
```

Copy và chỉnh `k8s/platform/ingress.example.yaml`, thay domain rồi apply. Chỉ
bật `COOKIE_SECURE=true` khi truy cập qua HTTPS.

Sau khi đăng nhập:

1. CI/CD → Platform Container Registry.
2. Admin lưu Docker Hub organization username và OAT/PAT hợp lệ một lần.
3. Trigger Map pipeline và kiểm tra đủ SOURCE→VERIFY.
4. Deploy version B rồi rollback về A.
5. Xác minh `demo-nginx`, Job/Audit/Deployment History.

## 8. Vận hành và nâng cấp

- Snapshot PVC/database định kỳ; kiểm thử restore.
- Rotate registry token, webhook secret và builder SSH key.
- Theo dõi disk của Build Worker; chỉ prune cache trong maintenance window.
- Không chạy `docker system prune` trong khi pipeline đang active.
- Không tăng Gunicorn worker process hoặc K3s replica ở Giai đoạn 1.
- Scale ngang chỉ sau khi có PostgreSQL, distributed lock và queue worker
  (Celery/RQ tương đương), rồi tách Web/API khỏi Pipeline Worker.
