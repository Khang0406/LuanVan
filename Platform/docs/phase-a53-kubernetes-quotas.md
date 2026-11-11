# Phase A.5.3 — Kubernetes ResourceQuota và LimitRange

Ngày hoàn thiện local: 2026-09-28

## 1. Mục tiêu và ranh giới

A.5.3 bổ sung lớp cưỡng chế quota độc lập tại Kubernetes. Nếu Control Plane bị
lỗi hoặc một manifest đi vòng qua validation A.5.2, namespace của project vẫn
chịu `ResourceQuota` và `LimitRange`.

Đợt này không cài cluster mới và không thực hiện nghiệm thu K3s thật vì máy ảo
chưa sẵn sàng. Logic Kubernetes được kiểm thử bằng manifest và kubectl mock;
checklist runtime nằm ở cuối tài liệu.

## 2. Namespace theo project

Mỗi project có namespace ổn định:

```text
project-<project_id>-<project_slug>
```

Tên luôn là DNS label, tối đa 63 ký tự và có project ID để tránh trùng. Mọi
application mới trong project dùng namespace này; field namespace do người dùng
nhập không thể phá tenant boundary.

Revision `0009_project_kubernetes_quotas`:

- thêm `kubernetes_namespace` và trạng thái sync vào `projects`;
- bỏ unique constraint của `applications.namespace` để nhiều app cùng project
  được dùng chung namespace;
- chuyển application hiện hữu sang namespace project;
- lưu namespace cũ trong payload bằng `legacy_namespace` để truy vết/rollback;
- downgrade khôi phục namespace cũ hoặc dùng application ID nếu app được tạo sau
  migration.

## 3. ResourceQuota được sinh

Mỗi namespace có đúng một `platform-project-quota` với:

| Subscription limit | Kubernetes hard resource |
|---|---|
| Tổng replica/pod | `pods` |
| Số service | `services` |
| Số Ingress | `count/ingresses.networking.k8s.io` |
| Số PVC | `persistentvolumeclaims` |
| Storage | `requests.storage` |
| CPU request/limit | `requests.cpu`, `limits.cpu` |
| Memory request/limit | `requests.memory`, `limits.memory` |

Số application được Control Plane kiểm soát vì Kubernetes không có resource
native tương ứng với khái niệm application của nền tảng.

## 4. LimitRange và resource mặc định

Mỗi namespace có `platform-project-limits`. Giá trị mặc định được giới hạn theo
quota plan, thông thường:

- request: `100m CPU`, `128Mi memory`;
- limit: `500m CPU`, `512Mi memory`.

Manifest application vẫn khai báo resource rõ ràng cho mọi container. A.5.3 đã
bổ sung request/limit cho:

- container service;
- managed database;
- database initialization Job.

Usage calculator A.5.2 cũng tính CPU/RAM và pod tạm thời của init Job để kết quả
Control Plane khớp với workload Kubernetes.

## 5. Đồng bộ và drift detection

Trạng thái project:

- `Pending`: plan/namespace vừa thay đổi, chờ apply;
- `Synced`: desired state và observed state có cùng semantic hash;
- `Drifted`: resource bị thiếu hoặc bị sửa ngoài Platform;
- `Error`: không kết nối/đọc/apply được Kubernetes.

So sánh quantity theo giá trị chuẩn hóa, nên `2 CPU` và `2000m` được xem là tương
đương. Hệ thống kiểm tra namespace label, ResourceQuota hard limits và
LimitRange defaults.

Plan được approve/gán trực tiếp sẽ chuyển status về `Pending` rồi tự sync khi
`KUBERNETES_QUOTA_SYNC_ENABLED=true`. Platform Admin có thể dùng trang gói để:

- **Kiểm tra drift**: chỉ đọc observed state;
- **Đồng bộ lại**: apply desired state rồi verify.

Trước stage DEPLOY, worker apply/verify quota một lần nữa. Nếu sync không đạt
`Synced`, pipeline dừng trước khi xóa workload cũ.

## 6. Xóa workload trong namespace dùng chung

Luồng cũ từng xóa namespace hoặc dùng `kubectl delete pod --all`. Hai thao tác
này không an toàn khi nhiều application dùng chung namespace.

Sau A.5.3:

- xóa application không xóa namespace project;
- pod, StatefulSet, Job, ConfigMap, Secret, PVC và Ingress chỉ bị xóa theo label
  `app.kubernetes.io/part-of=<application_id>`;
- ResourceQuota, LimitRange và workload của application khác được giữ nguyên.

## 7. Web và REST API

Trang subscription hiển thị namespace, status, thông báo và thời điểm sync gần
nhất. Nút inspect/sync chỉ dành cho Platform Admin có `quota:manage`.

`GET /api/v1/projects/<id>/subscription` trả thêm:

```json
{
  "kubernetes": {
    "namespace": "project-2-team-a",
    "quota_sync_status": "Synced",
    "quota_sync_message": "ResourceQuota và LimitRange đã đồng bộ.",
    "quota_synced_at": "2026-09-28T10:00:00+00:00"
  }
}
```

## 8. File thay đổi

| File | Nội dung |
|---|---|
| `migrations/versions/0009_project_kubernetes_quotas.py` | Namespace, sync state, application backfill |
| `app/models.py` | Trạng thái Kubernetes quota trên Project |
| `app/modules/quotas/kubernetes.py` | Sinh manifest, apply, verify và drift detection |
| `app/modules/projects/service.py` | Namespace ổn định khi tạo/bootstrap project |
| `app/modules/applications/service.py` | Application dùng namespace project; xóa an toàn |
| `app/modules/deployments/manifest.py` | Resource cho mọi container, bỏ app-scoped quota |
| `app/modules/deployments/kubectl.py` | Cleanup theo application label |
| `app/modules/pipeline/engine.py` | Bắt buộc sync quota trước deploy |
| `app/modules/subscriptions/service.py` | Plan change đánh dấu quota Pending |
| `app/modules/auth/admin.py` | Tự sync sau approve/direct assignment |
| `app/modules/projects/routes.py` | Inspect/sync thủ công và audit |
| `app/modules/api/routes.py` | Trạng thái Kubernetes trong subscription API |
| `tests/test_phase_a53_kubernetes_quotas.py` | Acceptance test A.5.3 |

## 9. Chờ nghiệm thu K3s thật

1. Backup PostgreSQL và chạy migration `0009_project_kubernetes_quotas`.
2. Kiểm tra ba application cũ có `legacy_namespace` và namespace project mới.
3. Chạy sync, xác nhận Namespace/ResourceQuota/LimitRange được tạo.
4. Deploy hai application cùng project và xác nhận cùng namespace.
5. Thử manifest vượt CPU/RAM/pod/PVC và xác nhận Kubernetes từ chối.
6. Sửa ResourceQuota trực tiếp bằng kubectl, chạy inspect và xác nhận `Drifted`.
7. Sync lại và xác nhận `Synced`.
8. Xóa một application, xác nhận application còn lại và namespace không bị xóa.
9. Kiểm tra/xóa workload trong `legacy_namespace` sau khi application đã redeploy
   thành công ở namespace project.
