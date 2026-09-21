# Phase A.4.3 — API token theo project

Ngày hoàn thiện local: 2026-09-21

## 1. Mục tiêu

Thay credential máy toàn cục bằng token cá nhân có phạm vi rõ ràng. Mỗi token
thuộc đúng một user và một project, chỉ gọi được các permission đã chọn và vẫn
chịu RBAC hiện tại của user. Token không thay thế mật khẩu đăng nhập Web.

## 2. Mô hình bảo mật

Token gốc có dạng `cict_<prefix>_<random-secret>` và chỉ xuất hiện trong HTTP
response tạo token. Database lưu `token_prefix` để tìm bản ghi và SHA-256 của
toàn bộ token để so sánh constant-time; không lưu token gốc.

Một request chỉ được phép khi đồng thời thỏa mãn:

1. hash khớp, token chưa hết hạn và chưa bị thu hồi;
2. user còn Active, project còn Active và membership còn Active;
3. project của resource trùng project của token;
4. scope chứa permission cần thiết;
5. role hiện tại của user vẫn có permission đó.

Vì quyền được giao tại thời điểm request, hạ role hoặc disable membership có
hiệu lực ngay với token đã phát hành. Platform Admin cũng bị giới hạn theo
project và scope khi dùng token mới; token không biến thành Admin toàn cục.

## 3. Dữ liệu và migration

Revision `0007_project_api_tokens` tạo bảng `api_tokens` gồm:

- tên, prefix và hash token;
- khóa ngoại `user_id`, `project_id` với `ON DELETE CASCADE`;
- danh sách scope JSON;
- expiry, revoked time, created time và last-used time/IP;
- rate limit và cửa sổ đếm request riêng từng token.

Các index phục vụ lookup prefix/hash, user, project, expiry và revocation. Lệnh
downgrade xóa bảng token mà không tác động user, project hoặc RBAC.

## 4. Web và API

Trang `/projects/<project_id>/tokens` cho user:

- xem token của chính mình trong project;
- chọn scope không vượt quyền hiện tại;
- chọn hạn 7, 30, 90, 180 hoặc 365 ngày;
- sao chép token gốc đúng một lần;
- xem prefix, scope, expiry, lần dùng cuối và trạng thái;
- thu hồi token ngay lập tức.

REST API nhận `Authorization: Bearer <token>`. Token sai/hết hạn/thu hồi trả
`401`; đúng danh tính nhưng thiếu scope/quyền trả `403`; vượt rate limit trả
`429 RATE_LIMIT_EXCEEDED` cùng `Retry-After`.

`PLATFORM_API_TOKEN` cũ vẫn hoạt động như Platform Admin để các client hiện hữu
không bị ngắt. Đây là compatibility credential; sau khi chuyển client sang token
mới nên xóa biến môi trường này.

## 5. Audit và dữ liệu nhạy cảm

Các action `API_TOKEN_CREATE`, `API_TOKEN_USE`, `API_TOKEN_REVOKE` lưu token ID,
project ID, scope/endpoint cần thiết nhưng không lưu raw token hay hash. IP sử
dụng gần nhất được giới hạn 45 ký tự. Token raw không đi qua redirect, flash,
URL hoặc log.

## 6. Cấu hình

```dotenv
API_TOKEN_RATE_LIMIT_PER_MINUTE=60
```

Giới hạn được chụp vào từng token khi tạo. Có thể thu hồi rồi tạo token mới nếu
cần áp dụng một mức khác. PostgreSQL dùng row lock khi cập nhật cửa sổ request
để giảm race giữa nhiều web worker.

## 7. Nghiệm thu local

- Raw token không tồn tại trong database.
- Token `application:read` đọc được application nhưng không trigger pipeline.
- Token Alpha không đọc được resource Beta.
- Token hết hạn hoặc đã revoke trả 401.
- Hạ Developer xuống Viewer làm mất scope vận hành ngay.
- Request vượt giới hạn trả 429 và `Retry-After`.
- Tạo/dùng/thu hồi token có audit, không lộ secret.
- Migration chạy được database trắng và vòng `head → base → head`.

Kết quả kiểm thử cuối được ghi trong
`docs/phase-a-implementation-log.md`. PostgreSQL runtime và K3s cần smoke test
lại sau khi máy ảo sẵn sàng.

## 8. File thay đổi

| File | Nội dung |
|---|---|
| `app/models.py` | Model `ApiToken` và relationship user/project |
| `app/modules/api_tokens/service.py` | Sinh/hash/auth/revoke/rate limit và principal |
| `app/modules/authorization/service.py` | Giao scope token với RBAC hiện tại |
| `app/modules/projects/service.py` | Cô lập project cho token principal |
| `app/modules/projects/routes.py` | Web lifecycle tạo/list/revoke và audit |
| `app/modules/api/routes.py` | Bearer authentication, 401/403/429 và audit use |
| `app/templates/projects/tokens.html` | UI quản lý token, hiển thị raw đúng một lần |
| `app/templates/projects/detail.html` | Liên kết quản lý token |
| `app/config.py`, `.env.example` | Rate limit mặc định |
| `migrations/versions/0007_project_api_tokens.py` | Migration upgrade/downgrade |
| `scripts/manage_database.py` | Schema validation cho bảng token |
| `tests/test_phase_a43_api_tokens.py` | Acceptance và security regression |
| `tests/test_database_migrations.py` | Xác nhận bảng ở migration head |

## 9. Checklist triển khai runtime

1. Backup PostgreSQL.
2. Chạy `python scripts/manage_database.py upgrade`.
3. Chạy `python scripts/manage_database.py check` và xác nhận revision
   `0007_project_api_tokens`.
4. Tạo token thử nghiệm scope `application:read`, gọi API đúng/sai project.
5. Thu hồi token và xác nhận request tiếp theo trả 401.
6. Chuyển automation sang token mới rồi xóa `PLATFORM_API_TOKEN` legacy.
