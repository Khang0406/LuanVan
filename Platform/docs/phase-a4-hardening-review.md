# Hardening sau Identity, Project, RBAC và API token

Ngày thực hiện: 2026-09-23

## Mục tiêu

Rà soát toàn bộ A.3–A.4 trước khi quota A.5 bắt đầu. Đợt này không thêm mô
hình nghiệp vụ mới; thay đổi tập trung vào các boundary dùng chung mà quota sẽ
phụ thuộc: authentication, authorization, scale và audit.

## Các vấn đề đã xử lý

### 1. CSRF của REST API dùng session

Trước đây toàn bộ blueprint API được miễn CSRF, dù endpoint có thể fallback
sang Flask session. Hiện chỉ request có Bearer token mới được miễn. Request API
dùng session phải gửi `X-CSRF-Token`; thiếu token bị từ chối trước khi mutation
chạy. Bearer sai vẫn thất bại ở authentication.

### 2. Resend email verification đồng thời

Luồng email verification nay khóa hàng user bằng `SELECT ... FOR UPDATE` trước
khi kiểm tra cooldown, giới hạn giờ, revoke token cũ và tạo token mới. Cơ chế
này đồng bộ với password reset, tránh hai request PostgreSQL đồng thời cùng để
lại token còn hiệu lực.

### 3. Scale input và trạng thái khi lỗi một phần

- Replica phải là số nguyên trong khoảng `0..SCALE_MAX_REPLICAS`.
- Giá trị mặc định tối đa là 100 và có thể cấu hình bằng environment.
- Giá trị chữ, âm hoặc vượt trần bị audit `FAILED`, không gọi `kubectl`.
- Service layer tiếp tục kiểm tra replica không âm để chống gọi vòng qua route.
- Nếu một deployment scale thất bại, chỉ service scale thành công được cập nhật
  trong desired state; không còn ghi mọi service về cùng replica khi lỗi một
  phần.

Đây mới là safety ceiling. A.5.2 sẽ thay kiểm tra tĩnh bằng quota theo project,
plan, tổng pod/CPU/RAM và khóa transaction.

### 4. Token môi trường legacy

`PLATFORM_API_TOKEN` chỉ hoạt động khi `ALLOW_LEGACY_API_TOKEN=true`.
Development giữ mặc định tương thích; production mặc định `false`. Mỗi lần sử
dụng legacy token được audit bằng `API_TOKEN_LEGACY_USE`. Mục tiêu vận hành vẫn
là chuyển automation sang project-scoped token rồi xóa credential legacy.

### 5. Audit append

`record_audit()` trước đây đọc toàn bộ lịch sử rồi upsert lại toàn bộ danh sách
cho mỗi event. Nay delivery store có `append_audit_log()` để insert đúng một
event trong một transaction, cho cả SQLite và PostgreSQL. Hàm bulk
`replace_audit_logs()` vẫn được giữ cho migration/restore. ID event mới dùng
UUID thay vì timestamp microsecond để không va chạm giữa nhiều web worker.

### 6. Địa chỉ IP đáng tin cậy

API token và audit dùng `request.remote_addr`. Khi chạy sau reverse proxy,
`ProxyFix` chỉ biến đổi giá trị này nếu `TRUST_PROXY_COUNT` được cấu hình. Header
`X-Forwarded-For` do client tự gửi không còn được tin trực tiếp.

## Cấu hình mới

```dotenv
ALLOW_LEGACY_API_TOKEN=false
SCALE_MAX_REPLICAS=100
```

## File thay đổi

| File | Nội dung |
|---|---|
| `app/security.py` | CSRF phân biệt Bearer và session API |
| `app/modules/auth/service.py` | Row lock khi tạo verification token |
| `app/config.py`, `.env.example` | Legacy token switch và scale ceiling |
| `app/modules/api/routes.py` | Chặn/audit legacy token và IP chuẩn hóa |
| `app/ui/routes.py` | Validate replica trước khi scale |
| `app/modules/deployments/kubectl.py` | Defensive validation và partial-state safety |
| `app/delivery_store.py` | Append một audit event trên SQLite/dispatcher |
| `app/delivery_store_pg.py` | Append một audit event trên PostgreSQL |
| `app/modules/audit/service.py` | Không đọc/upsert toàn bộ audit mỗi event |
| `tests/test_phase5_api.py` | Session API CSRF và disable legacy token |
| `tests/test_phase_a42_rbac.py` | Invalid/excessive scale acceptance |
| `tests/test_phase_a_hardening.py` | Partial scale state regression |

## Phần chờ môi trường runtime

- Chạy test concurrent email resend trên PostgreSQL `*_test`.
- Xác nhận legacy token bị từ chối khi production không opt-in.
- Scale 0, hợp lệ và vượt trần trên K3s thật.
- Đo thời gian ghi audit trước/sau với lượng event thực tế.

Global role dùng để bảo vệ server/cluster vẫn được giữ. Policy ai được tạo
project sẽ được chốt cùng subscription/onboarding, tránh tự thay đổi hành vi
nghiệp vụ trong một đợt hardening bảo mật.

## Kết quả kiểm thử local

```text
Hardening/API/RBAC/identity target: 44 tests OK
Full regression:                    184 tests OK
PostgreSQL integration skipped:     6 tests
Python compileall:                  OK
Dependency check:                  OK
Alembic schema diff:                không có thay đổi schema ngoài migration
git diff --check:                   OK
```
