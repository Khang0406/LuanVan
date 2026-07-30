# Biên bản nghiệm thu Giai đoạn 4 — Production hardening

Ngày thực hiện: 2026-07-29<br>
Repository: `/home/vpkhang-b2204938/projects/LuanVan/Platform`<br>
Nhánh: `khang`<br>
Nguyên tắc: giữ nguyên thay đổi Giai đoạn 0–3, không commit/push.

## Baseline trước thay đổi

- Working tree có thay đổi và file mới của Giai đoạn 0–3; không reset,
  checkout hoặc xóa.
- Đọc `phase1-acceptance-evidence.md` và
  `phase3-acceptance-evidence.md`; Giai đoạn 2 được đối chiếu qua
  `tests/test_phase2.py` và implementation hiện hành vì repo không có biên bản
  phase2 riêng.
- Baseline: 92/92 unit test đạt; compileall đạt; `git diff --check` đạt.
- Tracked secret check chỉ thấy `.env.example`; `instance/`, `.env`, secret
  manifest thật và generated sensitive files được ignore.

## Hạng mục triển khai

- [x] Web chỉ enqueue, worker riêng claim SQLite atomically.
- [x] Web restart không làm mất/đổi queued pipeline (automated restart test).
- [x] Worker interruption thành `Interrupted`; retry tạo run mới có kiểm soát.
- [x] Giữ một pipeline/application và giới hạn toàn hệ thống.
- [x] Gunicorn, probes, graceful shutdown, PVC, resources và non-root.
- [x] Backup/restore vòng kín cô lập; secret value không vào backup.
- [x] Admin/Developer/Viewer và application ownership enforcement.
- [x] CSRF static form scan; webhook chỉ HMAC; cross-app tests.
- [x] Cookie production, secret masking và Git ignore.
- [x] SMTP config bằng Secret; SMTP thật phụ thuộc credential môi trường.
- [ ] Regression cluster thật Map/demo-nginx và end-to-end GitHub → rollback.

## Automated acceptance

Kết quả cuối cùng được cập nhật sau checkpoint:

```text
unit tests: 104/104 pass
compileall app/tests/scripts: pass
git diff --check: pass
kustomize render + client create dry-run: pass, đủ 8 resources
server-side dry-run: pass, đủ 8 resources
container build: delivery-platform:phase4-local pass
web container /healthz + /readyz: HTTP 200
worker container --once: exit 0
tracked secret-pattern scan: 0 matching files
```

Post-acceptance regression ngày 2026-07-30:

- `/cicd` đã tương thích với pipeline legacy thiếu `application_name`; route
  thực tế trả HTTP 200.
- Record `phase2-acceptance-20260729-133044` được archive và xóa URL NodePort
  mồ côi sau khi xác nhận namespace đã cleanup. Vẫn giữ 8 deployment, 6
  pipeline, 9 job và audit history.
- Trang Applications không còn hiển thị URL `:32766`; Map và demo-nginx tiếp
  tục HTTP 200.

Namespace `platform-system` chưa tồn tại. Kubernetes API không lưu Namespace
giữa các document trong một dry-run stream; vì vậy lần kiểm tra cuối thay
`metadata.namespace` thành namespace hiện hữu chỉ trong stream server dry-run.
File source không đổi và không resource nào được tạo.

## Live read-only regression

Cluster được định danh: context/cluster `default`, API
`https://35.198.237.48:6443`.

- `map-web` và `map-mysql`: `1/1`; NodePort Map `30082`; MySQL PVC `Bound`.
- Map public path `/frontend/index.php`: HTTP `200` (root `/` trả `403` theo
  routing hiện tại và không phải endpoint nghiệm thu).
- `demo-nginx-web`: `1/1`; NodePort `30146`; HTTP `200`.
- Namespace `platform-system` chưa có workload. Không apply/restart/rollback
  live resource nào.

## Nghiệm thu không được tự động tác động

Không apply/delete/rollback/load-test workload thật nếu chưa xác nhận kube
context, namespace, application và baseline Map/demo-nginx. Các lệnh cluster
được phép trong phiên này trước hết chỉ là read-only identification và dry-run.

SMTP production cần credential/hộp thư thật. Nếu thiếu, bằng chứng Giai đoạn 3
vẫn chứng minh logic Firing → Resolved và chống gửi trùng; dependency phải được
ghi rõ, không dùng credential giả để tuyên bố production đạt.

## Dependency còn chặn nghiệm thu live

- Shell hiện tại thiếu toàn bộ `SMTP_HOST`, `SMTP_PORT`, `SMTP_FROM`, `SMTP_TO`,
  `SMTP_USERNAME`, `SMTP_PASSWORD`; chưa thể xác nhận hộp thư thật nhận đúng hai
  email.
- Manifest production còn placeholder organization/image tag/build-worker IP;
  namespace `platform-system`, runtime Secret và builder SSH Secret chưa được
  provision. Vì vậy không apply Platform thật.
- Chưa có application/namespace acceptance Giai đoạn 4 và GitHub delivery mới
  được phê duyệt. Không chạy GitHub → build/push/deploy/rollback live để tránh
  nhầm vào `map` hoặc `demo-nginx`.
- HPA/load/rollback live cuối chỉ được chạy sau khi tạo acceptance application
  cô lập, cấp registry/build credential và chụp baseline NodePort/PVC. Logic
  tương ứng đã được kiểm thử tự động hoặc có bằng chứng Giai đoạn 1–3.

## Demo bảo vệ luận văn

1. Giới thiệu sơ đồ trong `architecture.md` và ba role.
2. Đăng nhập Developer, tạo application cô lập và cấu hình GitHub HMAC.
3. Push commit; UI hiện `Queued`; restart web; task vẫn còn.
4. Worker claim và trình bày sáu stage, image pin digest, deployment history.
5. Mở Prometheus/Grafana, logs đã mask và HPA scale event.
6. Rollback đúng application, xem audit.
7. Tạo backup, thay dữ liệu mẫu, restore, restart web/worker và xác nhận record.
8. Chứng minh Map/demo-nginx HTTP 200, NodePort/PVC không đổi.
9. Trình bày so sánh Vercel, giới hạn SQLite/file secret và roadmap.

## Chức năng, giới hạn, hướng phát triển

| Đã làm | Giới hạn hiện tại | Hướng phát triển |
|---|---|---|
| SQLite durable queue, worker riêng | Một worker, một web replica | PostgreSQL + distributed queue |
| Container đa service/PVC/HPA | Cluster Kubernetes tự quản | Multi-cluster/tenant policy |
| RBAC + ownership | Ba role cố định | OIDC + policy engine |
| Secret masking/metadata backup | File secret chưa mã hóa app-layer | Vault/KMS và rotation |
| Prometheus/Grafana/SMTP | SMTP thật cần credential | Alertmanager/provider managed |
