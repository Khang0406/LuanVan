# Biên bản nghiệm thu Giai đoạn 1 — Application Delivery và CI/CD

Ngày kiểm tra: 2026-07-28

Nhánh làm việc: `khang`

Project: `/home/vpkhang-b2204938/projects/LuanVan/Platform`

## 1. Kết luận

Luồng production của Web Map đã chạy đủ sáu stage:

`SOURCE → BUILD → TEST → PUSH → DEPLOY → VERIFY`

Pipeline nghiệm thu thành công, image đã được push lên Docker Hub, deployment hiện tại là `v2` ở trạng thái `Ready`, Web và MySQL đều đạt `1/1`, URL public trả HTTP 200. `demo-nginx` vẫn hoạt động và trả HTTP 200.

Các bài kiểm thử tự động, compile check, Git whitespace check, Kubernetes server-side dry-run, Ansible syntax-check và production-container smoke test đều đạt.

Không ghi registry token, webhook secret hoặc giá trị credential vào tài liệu này.

## 2. Pipeline Web Map

| Thuộc tính | Giá trị |
|---|---|
| Application | `map` |
| Pipeline run | `run-map-961eb02a-43a7-4206-a03c-4582e0ac0f4e` |
| Trạng thái | `Success` |
| Repository | `https://github.com/Khang0406/map-project.git` |
| Branch | `master` |
| Commit SHA | `54cedb6c1339d93dcb31fc1ae6100b0e6b1317e0` |
| Image | `khangvo04/map-web:54cedb6c1339d93dcb31fc1ae6100b0e6b1317e0` |
| Digest | `sha256:c5be516138ff69d21942e66d453e38a63136adceb8ef52c413bee090e8ac192c` |
| URL | `http://35.198.237.48:30082/frontend/index.php` |

Kết quả từng stage:

| Stage | Kết quả | Bằng chứng chính |
|---|---|---|
| SOURCE | Done | Clone repository hoàn tất; lưu đúng repository, branch và Git commit SHA |
| BUILD | Done | Build riêng image Web với tag theo commit SHA |
| TEST | Done | Container chạy ổn định; health check `/frontend/index.php` đạt |
| PUSH | Done | Push Docker Hub thành công; digest được lưu |
| DEPLOY | Done | Tạo deployment record `v2`; apply namespace, Web Deployment và Service |
| VERIFY | Done | Web `1/1`, MySQL `1/1`, URL public được xác định |

## 3. Deployment và lịch sử

Deployment hiện tại:

| Thuộc tính | Giá trị |
|---|---|
| Deployment ID | `deployment-8f08b61e-6b2c-4ed9-b0ac-d14914c10ea1` |
| Version | `v2` |
| Status / Verify | `Ready / Ready` |
| Pipeline run | `run-map-961eb02a-43a7-4206-a03c-4582e0ac0f4e` |
| Previous deployment | `deployment-9ec1ec3f-f64b-4a16-9ecb-479346a4d0d1` |
| Namespace | `map` |
| Actor | `system` |
| Started | `2026-07-28 20:04:38` |
| Finished | `2026-07-28 20:04:42` |

Application `map` đang trỏ `current_deployment_id` tới deployment `v2` nêu trên. Lịch sử còn giữ `v1`, trạng thái `Ready`, và quan hệ previous/current không bị ghi đè.

Các service được ghi trong deployment:

- `web`: image tag theo commit SHA và digest bất biến như mục 2.
- `mysql`: `mysql:8.0`.

## 4. Job Log và Audit Log

Database hiện có bốn Job thuộc Web Map, gồm các lần pipeline thành công và thất bại trước đó. Pipeline nghiệm thu có Job trạng thái `Success`.

Pipeline nghiệm thu tạo đủ sáu Audit Event thành công:

- `PIPELINE_SOURCE`
- `PIPELINE_BUILD`
- `PIPELINE_TEST`
- `PIPELINE_PUSH`
- `PIPELINE_DEPLOY`
- `PIPELINE_VERIFY`

Audit metadata không chứa registry token. Secret scan trên workspace, loại trừ các thư mục runtime đã được Git ignore, không tìm thấy mẫu Docker PAT, GitHub token hoặc Authorization header có giá trị.

## 5. Bằng chứng K3s và HTTP

Trạng thái runtime tại thời điểm nghiệm thu:

| Workload | Ready | Trạng thái |
|---|---:|---|
| `map-web` Deployment | `1/1` | Ready |
| `map-web` Pod | `1/1` | Running, không restart |
| `map-mysql` Deployment | `1/1` | Ready |
| `map-mysql` Pod | `1/1` | Running, không restart |
| `demo-nginx` Deployment | `1/1` | Ready |
| `demo-nginx` Pod | `1/1` | Running, không restart |

Service:

- `map-web`: NodePort `30082`.
- `map-mysql`: ClusterIP, port `3306`.
- `demo-nginx`: NodePort `30146`.

HTTP smoke:

```text
Web Map HTTP 200
demo-nginx HTTP 200
```

## 6. Automated checkpoint

Các lệnh checkpoint:

```bash
./.venv/bin/python -m unittest discover -s tests -v
./.venv/bin/python -m compileall -q app tests
git diff --check
git status --short --branch
```

Kết quả:

```text
Ran 41 tests in 3.968s
OK
compileall: exit 0
git diff --check: exit 0
branch: khang
```

41 test bao phủ persistence/migration, validation, pipeline state transition, chống chạy trùng, dừng khi stage lỗi, Docker credential inheritance và masking, health probe, deployment record, verify, rollback, authorization, webhook và cloud runtime configuration. Các lệnh `git`, `docker` và `kubectl` trong unit test được mock; unit test không deploy thật.

## 7. Cloud/K3s artifact checks

### Kubernetes server-side dry-run

Manifest được render bằng `kubectl kustomize k8s/platform`, sau đó kiểm tra bằng Kubernetes API:

```text
namespace/platform-system created (server dry run)
serviceaccount/platform created (server dry run)
clusterrole.rbac.authorization.k8s.io/platform-application-delivery created (server dry run)
clusterrolebinding.rbac.authorization.k8s.io/platform-application-delivery created (server dry run)
configmap/platform-config created (server dry run)
service/platform created (server dry run)
persistentvolumeclaim/platform-data created (server dry run)
deployment.apps/platform created (server dry run)
```

### Ansible syntax

```text
playbook: ansible/playbooks/install_platform_build_worker.yml
exit: 0
```

### Production-container smoke

Image kiểm tra: `delivery-platform:phase1-cloud`. Test dùng database tạm và credential giả, không dùng secret thật.

```text
healthz 200
readyz 200
production container smoke OK
```

## 8. Bảo vệ credential và phạm vi Git

Đã xác nhận các đường dẫn sau được Git ignore:

- `.env`
- `instance/`, gồm runtime database và `registry_credentials.json`
- `app/data/builds/`

Không thực hiện `git add`, commit hoặc push. Worktree vẫn có các thay đổi Giai đoạn 0–1 và dữ liệu runtime; cần review và stage từng file cụ thể khi tạo mốc Git, tuyệt đối không dùng `git add ..`.

Không đưa vào commit:

- Registry token và webhook secret.
- Runtime database hoặc credential trong `instance/`.
- Docker config tạm, build directories và kubeconfig.
- Inventory sinh tự động hoặc file chứa mật khẩu Ansible.
- Monitoring cache, `.env` và backup nhạy cảm.

## 9. Giới hạn cần ghi nhận trung thực

- Rollback đã có automated test cho cả success/failure và trước đó đã được chứng minh trên deployment demo; chưa chạy live thao tác `v2 → v1` của Web Map sau lần cấp PAT mới để tránh thay đổi deployment đang nghiệm thu.
- Deployment lưu cả image tag và digest, nhưng manifest Web hiện deploy bằng tag theo commit. Cần pin trực tiếp `image@sha256:digest` và cấm ghi đè cùng một commit tag để rollback có tính bất biến tuyệt đối.
- Image nền `mysql:8.0` chưa lưu digest trong deployment record.
- Bộ manifest cloud đã qua server-side dry-run; chưa triển khai Platform lên một cloud environment mới trong checkpoint này.

Các mục trên là hardening tiếp theo, không làm thay đổi kết quả pipeline sáu stage và runtime Ready/HTTP 200 đã ghi nhận.

## 10. Mốc chuyển sang Giai đoạn 2

Trước khi tạo commit:

1. Review danh sách file bằng `git status --short` và `git diff -- <file>`.
2. Stage từng file/thư mục đã xác nhận bằng đường dẫn cụ thể.
3. Kiểm tra `git diff --cached --check` và `git diff --cached`.
4. Quét secret lại trên staged content.
5. Chỉ commit/push khi có yêu cầu chủ ý.

Có thể chia thành các commit logic:

1. `stabilize platform persistence and validation`
2. `implement production application delivery pipeline`
3. `add deployment versioning and rollback`
4. `add registry inheritance and secure credentials`
5. `add phase 1 tests and cloud deployment artifacts`
