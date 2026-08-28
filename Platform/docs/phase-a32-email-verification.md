# Phase A.3.2 — Tài khoản và xác minh email bằng framework

## 1. Kết quả

A.3.2 đã được viết lại trên nhánh thử nghiệm `experiment/auth-framework` theo
hướng kết hợp ba framework chuyên trách:

| Thành phần | Trách nhiệm |
|---|---|
| Flask-Security-Too 5.8 | Định danh ổn định, chuẩn hóa email và token xác minh có chữ ký |
| Flask-Mailman 1.1 | SMTP, email text/HTML và backend `locmem` cho test |
| Flask-Limiter 4.1 | Rate limit theo IP; Redis là storage dùng chung ở production |

Không bật blueprint giao diện mặc định của Flask-Security. Các route và template
hiện có được giữ để không làm thay đổi UI, audit event và quy tắc trạng thái tài
khoản của Platform. Đây là tích hợp framework ở tầng identity/service, không
phải thay toàn bộ ứng dụng bằng trang mẫu của framework.

## 2. Vì sao không thay toàn bộ route bằng Flask-Security

Thử nghiệm kiến trúc phát hiện bốn điểm không thể thay máy móc:

- password hiện tại là hash Werkzeug `scrypt`; đổi password backend ngay có thể
  khiến `admin` và `dev` không đăng nhập được;
- Platform có trạng thái `PendingVerification`, `Active`, `Locked`, `Disabled`,
  trong khi framework dùng cờ `active` và `confirmed_at`;
- yêu cầu luận văn bắt buộc token resend thu hồi token cũ, database chỉ lưu hash
  và mọi bước có audit; token mặc định của framework không cung cấp đủ registry
  nghiệp vụ này;
- RBAC A.4 sẽ theo project, nên role compatibility của framework không được dùng
  thay cho authorization service trong tương lai.

Giải pháp cuối cùng giữ `password_hash`, `role`, `status`, route và audit hiện
tại, đồng thời dùng các primitive đã được framework kiểm chứng cho email/token,
mail transport và distributed rate limiting.

## 3. Mô hình dữ liệu sau khi viết lại

Revision mới: `0003_auth_framework_foundation`, tiếp sau
`0002_account_email_verification`.

`users` được bổ sung:

- `fs_uniquifier`: UUID ngẫu nhiên, unique, không nullable; là định danh session
  ổn định của Flask-Security;
- `active`: cờ tương thích framework, luôn đồng bộ với `status == Active`;
- `confirmed_at`: thời điểm xác nhận theo chuẩn Flask-Security.

Hai bảng compatibility được thêm theo contract SQLAlchemy datastore:

- `security_roles`;
- `security_user_roles`.

Hai bảng này chưa phải RBAC theo project. `users.role` vẫn là nguồn phân quyền
hiện tại; A.4 sẽ tạo Project Membership, Role và Permission riêng.

Migration backfill user cũ như sau:

```text
fs_uniquifier = UUID mới cho từng user
active         = true nếu status = Active
confirmed_at   = email_verified_at hoặc created_at hoặc thời điểm migration
password_hash  = giữ nguyên 100%
```

Loader session nhận cả `fs_uniquifier` mới và numeric user id cũ. Vì vậy phiên
đăng nhập cũ không bị cắt đột ngột chỉ do deploy A.3.2; phiên mới sẽ tự dùng định
danh framework.

## 4. Token xác minh hai lớp

Token mới được tạo bởi Flask-Security và ký bằng
`SECURITY_PASSWORD_SALT`. Payload ràng buộc cả `fs_uniquifier` và hash email.
Platform tiếp tục lưu `SHA-256(raw_token)` trong
`email_verification_tokens`, không lưu token nguyên văn.

Khi xác minh, hệ thống kiểm tra đồng thời:

1. hash token có tồn tại trong registry;
2. token chưa có `used_at`;
3. `expires_at` của Platform chưa hết hạn;
4. chữ ký Flask-Security hợp lệ;
5. token thuộc đúng user và đúng email hiện tại.

Chỉ khi cả năm điều kiện đạt, user mới chuyển `Active`, `active=true`, đồng thời
ghi `email_verified_at` và `confirmed_at`. Resend đánh dấu toàn bộ token cũ đã
dùng trước khi tạo token mới. Cách này giữ được khả năng thu hồi rõ ràng mà vẫn
có xác thực chữ ký của framework.

## 5. Email và rate limit

`app/email_service.py` tự viết đã được xóa. `EmailMultiAlternatives` của
Flask-Mailman gửi cùng lúc bản text và HTML, vẫn sử dụng các biến SMTP cũ nên
không phải đổi Kubernetes Secret.

Rate limit có hai lớp:

- Flask-Limiter: `POST /auth/register` tối đa 10 lần/giờ/IP và
  `POST /auth/resend-verification` tối đa 20 lần/giờ/IP;
- registry database: cooldown 60 giây, tối đa 5 lần/giờ/user và 20 lần/giờ/IP.

Production dùng `REDIS_URL` làm storage của Flask-Limiter, nên giới hạn được chia
sẻ giữa nhiều web replica. Test dùng `memory://` và mail backend `locmem`, không
gửi email ra Internet.

## 6. Cấu hình

Các biến SMTP cũ vẫn giữ nguyên:

```env
PLATFORM_PUBLIC_URL=https://platform.example.com
SMTP_HOST=smtp.example.com
SMTP_PORT=587
SMTP_FROM=platform@example.com
SMTP_USERNAME=...
SMTP_PASSWORD=...
SMTP_STARTTLS=true
SMTP_SSL=false
SMTP_TIMEOUT=10
REDIS_URL=redis://redis:6379/0
SECURITY_PASSWORD_SALT=<secret độc lập tối thiểu 32 ký tự>
```

Production từ chối khởi động nếu thiếu HTTPS public URL, SMTP host/sender hoặc
nếu `SECURITY_PASSWORD_SALT` ngắn hơn 32 ký tự/trùng `FLASK_SECRET_KEY`.
Credential, raw token và địa chỉ email đầy đủ không được ghi vào audit log.

## 7. File thay đổi

| File | Nội dung |
|---|---|
| `requirements.txt` | Thêm Flask-Security-Too, Flask-Mailman, Flask-Limiter Redis |
| `app/extensions.py` | Khởi tạo tập trung Security, Mail và Limiter |
| `app/__init__.py` | Gắn datastore, session loader tương thích và extension lifecycle |
| `app/config.py` | Ánh xạ SMTP → Mailman, token salt, Redis limiter và production guard |
| `app/models.py` | Thêm identity fields, framework mixin và compatibility roles |
| `app/modules/auth/service.py` | Token ký + token hash, email validation và Mailman delivery |
| `app/modules/auth/routes.py` | Gắn rate limit và đồng bộ trạng thái đăng ký |
| `migrations/versions/0003_auth_framework_foundation.py` | Migration có upgrade/downgrade và backfill |
| `scripts/manage_database.py` | Kiểm tra schema mới khi chạy `check` |
| `.env.example` | Mô tả salt và mail backend |
| `tests/test_phase_a32_email_verification.py` | Acceptance/security tests mới |
| `tests/test_database_migrations.py` | Test backfill và round-trip migration |
| `app/email_service.py` | Đã xóa; Flask-Mailman thay thế |

## 8. Kiểm thử và log nghiệm thu

Các trường hợp được kiểm tra:

- đăng ký → pending → xác minh → active → đăng nhập bằng email;
- token chỉ dùng một lần và token hết hạn bị từ chối;
- dù sửa hash trong database, token bị sửa chữ ký vẫn không kích hoạt user;
- lỗi gửi email không rollback tài khoản/token;
- phản hồi resend không tiết lộ email có tồn tại;
- cooldown database và HTTP 429 theo IP;
- Locked/Disabled không đăng nhập và session bị từ chối;
- Mailman `locmem` giữ email trong test, không đi ra Internet;
- production guard cho HTTPS, SMTP và signing salt;
- migration database trắng, backfill user legacy và vòng
  `upgrade → downgrade → upgrade`.

Kết quả gần nhất:

```text
python -m unittest discover -s tests
150 tests OK, 6 PostgreSQL integration tests skipped

python -m unittest tests.test_database_migrations \
  tests.test_phase_a32_email_verification
12 tests OK
```

Kiểm chứng PostgreSQL ngày 2026-08-28:

- `platform_test` nâng `0002 → 0003`, chạy 6/6 integration test và vòng
  `0003 → 0002 → 0003` thành công;
- database runtime `platform` ở revision `0003_auth_framework_foundation`;
- còn đủ 2 user, 3 application, 44 pipeline run và 10 deployment;
- `admin`, `dev` đều `Active`, có `fs_uniquifier` 32 ký tự và `confirmed_at`;
- backup trước smoke test:
  `instance/platform-pre-auth-framework-20260828-130120.dump`, mode `0600`,
  SHA-256 `384ccddff7e89ee2efacc94d4f50e9f26b5c23db8d8dc14222ebb679ee97b18f`.

Runtime smoke test đạt: `/healthz`, `/readyz`, `/auth/register`, `/auth/login`,
`/dashboard`, `/applications` đều trả đúng trạng thái; tài khoản legacy đăng
nhập được và UI còn hiển thị đủ `demo-nginx`, `map`, `ctu-cinema`. Celery kết
nối Redis/PostgreSQL và sẵn sàng với concurrency 4.

## 9. Triển khai và rollback

Triển khai sau khi PostgreSQL test đạt:

```bash
pg_dump --format=custom --file=<backup.dump> <database>
python scripts/manage_database.py upgrade
python scripts/manage_database.py check
```

Rollback schema khi chưa phát sinh dữ liệu phụ thuộc revision mới:

```bash
python scripts/manage_database.py downgrade 0002_account_email_verification
```

Rollback code an toàn nhất là quay về checkpoint commit
`17dba183337240eddd0815d5fda11562493b2c5c`. Không dùng reset trên database đã
ghi dữ liệu mới; phải downgrade migration trước hoặc phục hồi backup.

## 10. Phạm vi chưa làm

- khóa tạm theo số lần login sai, reset password và revoke toàn bộ session thuộc
  A.3.3;
- Project Membership và RBAC theo project thuộc A.4;
- SMTP thật chưa được cung cấp credential; runtime hiện kiểm tra workflow bằng
  acceptance test và không gửi email ra ngoài.
