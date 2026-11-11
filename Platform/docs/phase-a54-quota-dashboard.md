# Phase A.5.4 — Dashboard quota và cảnh báo sử dụng

Ngày hoàn thiện local: 2026-11-11

## 1. Mục tiêu và phạm vi

A.5.4 biến usage calculator của A.5.2 và Kubernetes enforcement của A.5.3
thành một dashboard có thể giải thích cho người dùng: đang dùng bao
nhiêu, còn bao nhiêu, tài nguyên nào sắp hết và có cần nâng cấp hay
không. Web và REST API dùng chung một quota summary service để không tự
tính ngưỡng theo hai cách khác nhau.

Không thêm payment, email alert, Prometheus hay usage snapshot theo thời gian.
Các phần này thuộc Phase C. A.5.4 không cần migration database mới.

## 2. Quota summary chuẩn hóa

`build_quota_dashboard()` trả mười resource theo thứ tự cố định:

- application, service và replica/pod;
- CPU request/limit;
- memory request/limit;
- Ingress, PVC và storage.

Mỗi resource có `used`, `limit`, `remaining`, `percentage`, `status`, `unit`
và ba giá trị đã format cho UI. CPU được hiển thị bằng millicore/core;
memory và storage bằng MiB/GiB. `remaining` không bao giờ âm, nhưng
`percentage` vẫn có thể lớn hơn 100% để không che giấu tình trạng over-quota.

Ngưỡng trạng thái:

| Mức sử dụng | Status | Ý nghĩa |
|---:|---|---|
| `< 70%` | `normal` | Bình thường |
| `70% .. < 85%` | `warning` | Cần chú ý |
| `85% .. < 100%` | `critical` | Sắp hết quota |
| `>= 100%` | `exhausted` | Đã hết/vượt quota |

Với limit bằng 0: usage bằng 0 là `normal`; usage dương là
`exhausted`. Quy tắc này tránh chia cho 0 và không cảnh báo sai cho
resource bị tắt nhưng chưa được sử dụng.

Summary cấp project có resource sử dụng cao nhất, số warning/critical/
exhausted và cờ `needs_upgrade`.

## 3. Dashboard project

Trang `/projects/<id>/subscription` hiển thị:

- gói hiện tại và resource có phần trăm cao nhất;
- namespace, trạng thái và thời điểm Kubernetes quota sync;
- card/progress bar cho mười resource, có màu, chữ trạng thái và
  thuộc tính ARIA;
- banner 70%/85%/100% và liên kết đến yêu cầu nâng cấp;
- yêu cầu Pending nổi bật; form mới bị ẩn để không tạo request trùng;
- lịch sử gói có thể mở snapshot quota trước/sau.

Màu không phải tín hiệu duy nhất: mỗi card luôn có nhãn `Bình thường`,
`Cần chú ý`, `Sắp hết` hoặc `Đã hết quota`.

## 4. Dashboard Platform Admin

Trang `/admin/subscriptions` có thêm bảng tổng quan theo project:

- gói và Kubernetes namespace;
- resource cao nhất;
- số warning/critical/exhausted;
- trạng thái Kubernetes quota;
- liên kết dashboard chi tiết.

Project exhausted và critical được xếp trước. Danh sách application
được tải một lần rồi tái sử dụng cho mọi project, tránh lặp thao tác
đọc delivery store cho từng tenant.

## 5. REST API và khả năng tương thích

`GET /api/v1/projects/<id>/subscription` giữ nguyên `limits` và `usage`
cho client cũ, đồng thời bổ sung:

- `quota_summary`;
- `resources` với các field chuẩn hóa;
- `upgrade_request` nếu project có request Pending;
- khối `kubernetes` đã có từ A.5.3.

Endpoint tiếp tục dùng `can_access_project`; member/token project A không thể
đọc dashboard project B.

## 6. Lỗi vượt quota

`QuotaExceeded` bổ sung `remaining` và `exceeded_by`. Message nêu rõ usage
hiện tại, delta yêu cầu, projected usage, gói và limit. REST API, Web và
audit vẫn dùng cùng `details`; scale-down tiếp tục được phép.

## 7. File thay đổi

| File | Nội dung |
|---|---|
| `app/modules/quotas/service.py` | Summary, threshold, format và chi tiết lỗi quota |
| `app/modules/projects/routes.py` | Dashboard và Pending request cho project |
| `app/modules/api/routes.py` | API resources/summary/request, giữ tương thích field cũ |
| `app/modules/auth/admin.py` | Tổng hợp dashboard mọi project bằng một lần load app |
| `app/templates/projects/subscription.html` | Dashboard project và progress bar |
| `app/templates/admin/subscriptions.html` | Bảng cảnh báo cho Platform Admin |
| `tests/test_phase_a54_quota_dashboard.py` | Acceptance test A.5.4 |
| `docs/phase-a54-quota-dashboard.md` | Tài liệu này |

## 8. Kiểm thử local

- Acceptance A.5.4: 7/7 test đạt.
- Regression A.5.1–A.5.4: 31/31 test đạt.
- Full regression: 217/217 test đạt; 6 test PostgreSQL integration được
  skip đúng thiết kế khi chưa cấu hình database `_test`.
- `compileall`, `pip check` và `git diff --check` đều đạt.

## 9. Chờ nghiệm thu runtime

Khi PostgreSQL/K3s sẵn sàng:

1. Tạo workload lần lượt đạt 70%, 85% và 100%.
2. Đối chiếu dashboard với `kubectl describe resourcequota`.
3. Thử deploy/scale-up để xem lỗi vượt quota và thử scale-down.
4. Gửi/duyệt nâng cấp, xác nhận dashboard và ResourceQuota cùng đổi.
5. Sửa ResourceQuota ngoài hệ thống, inspect drift và sync lại.
