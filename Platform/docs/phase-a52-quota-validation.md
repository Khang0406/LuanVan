# Phase A.5.2 — Usage Calculator và Quota Validation

Ngày hoàn thiện local: 2026-09-25

## 1. Mục tiêu và ranh giới

A.5.2 biến hạn mức danh nghĩa của subscription thành kiểm soát thật tại Control
Plane. Hệ thống tính desired usage của toàn project và từ chối create, deploy
hoặc scale-up nếu trạng thái sau thao tác vượt giới hạn.

Kubernetes `ResourceQuota` và `LimitRange` chưa thuộc đợt này; đó là A.5.3. Vì
vậy A.5.2 bảo vệ mọi luồng đi qua Control Plane, còn A.5.3 sẽ bổ sung lớp cưỡng
chế độc lập trong cluster.

## 2. Usage được tính như thế nào

Usage được tổng hợp từ mọi application thuộc project:

- số application;
- số service, bao gồm managed database;
- tổng replica/pod mong muốn;
- tổng CPU request và CPU limit theo `resource × replicas`;
- tổng memory request và memory limit theo `resource × replicas`;
- số Ingress của service public;
- số PVC và tổng dung lượng storage;
- StatefulSet có một PVC cho mỗi replica.

Các quantity Kubernetes được chuẩn hóa:

- CPU về millicore: `1 = 1000m`, `0.5 = 500m`;
- memory/storage về MiB: `1Gi = 1024Mi`;
- số lẻ được làm tròn lên để không đánh giá thấp tài nguyên.

CPU/memory request không được lớn hơn limit. Quantity sai định dạng bị từ chối
trước khi application được lưu hoặc trước khi thao tác Kubernetes bắt đầu.

## 3. Các điểm kiểm tra

### Tạo application

```text
Parse và validate cấu hình
→ khóa hàng project
→ tính usage hiện tại + application mới
→ kiểm tra mọi limit
→ lưu application
→ commit và nhả khóa
```

Quota được kiểm tra trước khi lưu registry credential, secret và webhook secret,
do đó một yêu cầu bị từ chối không để lại credential rác.

### Deploy/pipeline

Quota được kiểm tra hai lần:

1. lúc Web/API/webhook yêu cầu đưa pipeline vào queue;
2. trong worker, ngay trước khi xóa workload cũ và chạy `kubectl apply`.

Lần thứ hai xử lý trường hợp plan hoặc usage thay đổi trong lúc pipeline đang
chờ/build. Nếu vượt quota, stage `DEPLOY` chuyển `Failed` trước khi workload cũ
bị xóa.

### Scale

Scale-up tính trạng thái dự kiến của toàn project và kiểm tra cả:

- tổng replica của project;
- CPU/RAM phát sinh theo replica;
- `max_replicas` cấu hình riêng của từng service.

Scale-down luôn được phép nếu tổng replica của application giảm, kể cả project
đã vượt quota sau khi bị hạ gói. Đây là đường thoát để project giải phóng tài
nguyên thay vì bị khóa cứng.

## 4. Chống request đồng thời

Create, deploy, scale và thay đổi subscription dùng chung khóa
`SELECT ... FOR UPDATE` trên hàng `projects`. Trên PostgreSQL, hai mutation cùng
project được xử lý tuần tự:

```text
Request A khóa project → tính → lưu → commit
Request B chờ → tính lại trên dữ liệu mới → cho phép hoặc QUOTA_EXCEEDED
```

Khóa chỉ theo project nên nhóm A không chặn thao tác của nhóm B. SQLite test xác
nhận logic; kiểm thử cạnh tranh thật cần chạy lại với database PostgreSQL `_test`.

Việc lưu một application cũng được đổi từ `replace_applications()` sang atomic
upsert một record và các service của record đó. Vì vậy update/scale project A
không còn ghi lại toàn bộ danh sách và không thể làm mất thay đổi vừa commit của
project B.

## 5. Hợp đồng lỗi

REST API trả HTTP `409` với cấu trúc:

```json
{
  "data": null,
  "error": {
    "code": "QUOTA_EXCEEDED",
    "message": "Yêu cầu vượt quota Tổng replica/pod...",
    "details": {
      "project_id": 2,
      "plan": "basic",
      "resource": "max_replicas",
      "label": "Tổng replica/pod",
      "used": 4,
      "requested": 8,
      "projected": 12,
      "limit": 10
    }
  },
  "meta": {"request_id": "..."}
}
```

Web hiển thị cùng thông báo và audit lưu details để điều tra. GitHub webhook cũng
trả `QUOTA_EXCEEDED` có cấu trúc nếu push không thể tạo pipeline.

## 6. Hiển thị usage

Trang gói của project hiển thị `đang dùng / giới hạn` cho mười tài nguyên. API
subscription bổ sung trường `usage`. Dashboard phần trăm và cảnh báo ngưỡng vẫn
được dành cho A.5.4.

## 7. File thay đổi

| File | Nội dung |
|---|---|
| `app/modules/quotas/service.py` | Quantity parser, usage calculator, project lock và validation |
| `app/delivery_store.py` | Atomic upsert một application trên SQLite |
| `app/delivery_store_pg.py` | Atomic upsert một application trên PostgreSQL |
| `app/modules/applications/service.py` | Chặn create trước persistence/secret side effect |
| `app/modules/pipeline/engine.py` | Kiểm tra lúc enqueue và ngay trước deploy |
| `app/modules/pipeline/webhook.py` | Lỗi quota có cấu trúc cho GitHub webhook |
| `app/ui/routes.py` | Web create/deploy/scale/retry và audit quota |
| `app/modules/api/routes.py` | API `QUOTA_EXCEEDED` và usage theo project |
| `app/modules/subscriptions/service.py` | Đồng bộ khóa plan change với quota mutation |
| `app/modules/projects/routes.py` | Cấp usage cho trang subscription |
| `app/templates/projects/subscription.html` | Hiển thị đang dùng / giới hạn |
| `tests/test_phase_a52_quota_validation.py` | Acceptance test calculator và enforcement |

## 8. Kết quả kiểm thử local

- 10/10 acceptance test A.5.2 đạt.
- Nhóm regression quota/storage/pipeline/webhook/API/RBAC đạt 56/56.
- Full regression chạy 201 test và đạt `OK`; 6 PostgreSQL integration test được
  skip khi chưa cấu hình database test an toàn có hậu tố `_test`.
- `compileall`, `pip check` và `git diff --check` đều đạt.

## 9. Chờ nghiệm thu runtime

1. Chạy full regression trên PostgreSQL test database có hậu tố `_test`.
2. Gửi hai create/scale đồng thời cho cùng project và xác nhận chỉ request hợp lệ
   được commit.
3. Thử project Basic đủ quota rồi deploy/scale trên K3s thật.
4. Hạ quota thấp hơn usage, xác nhận deploy/scale-up bị chặn nhưng scale-down được
   phép.
5. Sau khi có cluster, tiếp tục A.5.3 để Kubernetes cưỡng chế quota độc lập.
