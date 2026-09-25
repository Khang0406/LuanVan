# Phase A.5.1 — Subscription Plan và yêu cầu nâng cấp

Ngày hoàn thiện local: 2026-09-25

## 1. Mục tiêu và ranh giới

A.5.1 định nghĩa project được cấp gói nào và hạn mức danh nghĩa bao nhiêu. Đợt
này hoàn thiện lifecycle Basic/Pro/Custom, yêu cầu nâng cấp, phê duyệt và lịch
sử. Việc tính usage rồi chặn create/deploy/scale chưa nằm ở đây; đó là A.5.2.

Không tích hợp thanh toán. Platform Admin là bên quyết định cuối cùng.

## 2. Catalog gói mặc định

| Hạn mức | Basic | Pro/VIP |
|---|---:|---:|
| Application | 3 | 10 |
| Service | 10 | 40 |
| Tổng replica/pod | 10 | 40 |
| CPU request | 2.000m | 8.000m |
| CPU limit | 4.000m | 16.000m |
| Memory request | 4.096 MiB | 16.384 MiB |
| Memory limit | 8.192 MiB | 32.768 MiB |
| Ingress | 3 | 10 |
| PVC | 3 | 10 |
| Storage | 10.240 MiB | 102.400 MiB |

Custom bắt buộc có đủ mười giá trị do người dùng đề xuất hoặc Admin nhập. CPU
request không được lớn hơn CPU limit; Memory request không được lớn hơn Memory
limit. Tất cả giá trị là số nguyên không âm.

## 3. Vì sao lưu snapshot quota

`subscription_plans` là catalog. Khi gán gói, hệ thống sao chép limits vào
`project_subscriptions.effective_limits_json`. Vì vậy thay đổi catalog trong
tương lai không âm thầm đổi quota project đang chạy. Mỗi lần gán/duyệt tạo một
snapshot cũ/mới trong `subscription_history`, phù hợp audit và rollback nghiệp
vụ sau này.

## 4. Quy trình người dùng

Project member được xem gói và limits. Chỉ Project Admin hoặc Platform Admin có
`project:manage` mới gửi yêu cầu:

```text
Chọn Pro/Custom + nhập lý do
→ kiểm tra project chưa có yêu cầu Pending
→ lưu request và snapshot limits được đề xuất
→ ghi audit SUBSCRIPTION_REQUEST_CREATE
```

Một project chỉ có một yêu cầu Pending tại một thời điểm. Request project A
không hiển thị cho member project B.

## 5. Quy trình Platform Admin

Trang `/admin/subscriptions` hỗ trợ:

- xem yêu cầu của mọi project;
- approve: cập nhật subscription, tạo history và đóng request;
- reject: bắt buộc có lý do, không đổi gói hiện tại;
- gán Basic/Pro/Custom trực tiếp;
- khi gán trực tiếp, mọi request Pending của project được chuyển Cancelled để
  không còn quyết định treo mâu thuẫn.

Mọi thao tác có audit với project ID, request ID, plan và limits; không lưu dữ
liệu thanh toán vì luận văn không triển khai payment.

## 6. Schema và migration

Revision `0008_subscription_plans` tạo:

- `subscription_plans` — catalog Basic, Pro/VIP, Custom;
- `project_subscriptions` — đúng một subscription cho mỗi project;
- `subscription_upgrade_requests` — yêu cầu và kết quả review;
- `subscription_history` — snapshot thay đổi plan/quota.

Migration seed ba plan, backfill mọi project hiện hữu bằng Basic và tạo history
`INITIAL_ASSIGNMENT`. Project tạo mới cũng nhận Basic trong cùng transaction với
project/membership. Downgrade xóa đúng bốn bảng theo thứ tự foreign key.

## 7. Web và REST API

- `/projects/<id>/subscription`: xem gói, limits, request và history.
- `POST /projects/<id>/subscription`: gửi yêu cầu.
- `/admin/subscriptions`: review và gán trực tiếp.
- `GET /api/v1/projects/<id>/subscription`: trả plan/limits, vẫn kiểm tra tenant
  và project scope của session/API token.

## 8. File thay đổi

| File | Nội dung |
|---|---|
| `app/models.py` | Bốn model subscription và relationship project |
| `app/modules/subscriptions/service.py` | Catalog, validation, assignment, request/review/history |
| `migrations/versions/0008_subscription_plans.py` | Schema, seed, backfill, downgrade |
| `app/__init__.py` | Bootstrap catalog/subscription cho local test |
| `app/modules/projects/service.py` | Basic assignment cho project mới |
| `app/modules/projects/routes.py` | Trang và POST request theo permission |
| `app/modules/auth/admin.py` | Review và direct assignment cho Platform Admin |
| `app/modules/api/routes.py` | API đọc subscription theo tenant |
| `app/templates/projects/subscription.html` | UI project plan/request/history |
| `app/templates/admin/subscriptions.html` | UI Admin review/assign |
| `scripts/manage_database.py` | Schema validation revision 0008 |
| `tests/test_phase_a51_subscriptions.py` | Acceptance workflow và isolation |
| `tests/test_database_migrations.py` | Seed/backfill và migration round-trip |

## 9. Kết quả kiểm thử local

- 33/33 test trọng tâm đạt: subscription, migration, REST API và API token.
- Full regression chạy 191 test và đạt `OK`; trong đó 6 PostgreSQL integration
  test được skip khi chưa có database test an toàn với hậu tố `_test`.
- Migration chạy thành công theo cả hai hướng rồi nâng lại:
  `upgrade head → downgrade base → upgrade head`.
- Database SQLite trắng dựng được đến `0008_subscription_plans`.
- `manage_database.py check` xác nhận đủ schema; `alembic check` không phát hiện
  thao tác migration còn thiếu.
- `compileall`, `pip check` và `git diff --check` đều đạt.

## 10. Chờ nghiệm thu runtime

1. Backup PostgreSQL.
2. Upgrade/check revision `0008_subscription_plans`.
3. Xác nhận ba application hiện hữu nằm trong project có Basic subscription.
4. Thử request → approve → Custom trực tiếp bằng PostgreSQL thật.
5. Kiểm tra hai request đồng thời chỉ tạo được một Pending request.

ResourceQuota/LimitRange và kiểm tra `QUOTA_EXCEEDED` chưa được tạo ở A.5.1.
Đó là tiêu chí của A.5.2 và A.5.3.
