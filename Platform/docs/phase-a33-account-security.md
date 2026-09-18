# Phase A.3.3 — Bảo mật đăng nhập và khôi phục tài khoản

## 1. Kết quả

A.3.3 bổ sung quy trình khôi phục tài khoản hoàn chỉnh trên nền identity của
A.3.2. Người dùng có thể yêu cầu email đặt lại mật khẩu, sử dụng token một lần,
và toàn bộ phiên đăng nhập cũ bị thu hồi sau khi mật khẩu thay đổi.

Đăng nhập sai liên tiếp được theo dõi trong một cửa sổ thời gian và tài khoản bị
khóa tạm. Giới hạn theo IP của Flask-Limiter tiếp tục bảo vệ cả những yêu cầu
không ánh xạ được tới tài khoản.

Luồng chính:

```text
Quên mật khẩu
→ phản hồi trung tính
→ token ký + lưu hash
→ gửi email
→ kiểm tra chữ ký/hash/hạn dùng/user
→ đổi mật khẩu
→ xoay fs_uniquifier
→ thu hồi mọi session cũ
```

## 2. Schema và migration

Revision mới: `0004_account_recovery_security`, tiếp sau
`0003_auth_framework_foundation`.

Các cột mới trên `users`:

| Cột | Mục đích |
|---|---|
| `failed_login_count` | Số lần sai trong cửa sổ hiện tại |
| `failed_login_window_started_at` | Mốc bắt đầu cửa sổ đếm lỗi |
| `locked_until` | Thời điểm hết khóa tạm; không thay đổi trạng thái quản trị |

Bảng `password_reset_tokens` lưu:

- `user_id` với foreign key cascade;
- SHA-256 của token, không lưu token nguyên văn;
- thời điểm hết hạn, sử dụng và tạo;
- IP đã yêu cầu, tối đa 45 ký tự.

Migration có downgrade về revision `0003`. `scripts/manage_database.py check`
được cập nhật để phát hiện thiếu bảng/cột mới.

## 3. Token đặt lại mật khẩu

Token có hai lớp kiểm tra giống email verification:

1. token phải có hash tương ứng trong `password_reset_tokens`;
2. record chưa có `used_at`;
3. hạn dùng do Platform quản lý chưa hết;
4. chữ ký Flask-Security hợp lệ;
5. token vẫn thuộc đúng `fs_uniquifier`, user và password hash hiện tại.

Khi gửi lại, mọi token chưa dùng trước đó bị vô hiệu hóa. Khi đặt lại thành
công, token hiện tại và toàn bộ token còn lại đều được đánh dấu đã dùng.

Phản hồi của `/auth/forgot-password` giống nhau cho email tồn tại, không tồn tại,
chưa xác minh hoặc bị vô hiệu hóa. Điều này ngăn dùng form để dò tài khoản.

## 4. Rate limit và khóa tạm

Các lớp giới hạn:

| Chức năng | Mặc định |
|---|---:|
| Login theo IP | 30 POST/phút |
| Forgot password theo IP | 10 POST/giờ |
| Reset password theo IP | 20 POST/giờ |
| Reset cooldown theo user | 60 giây |
| Reset tối đa theo user | 5 lần/giờ |
| Reset tối đa theo IP trong database | 20 lần/giờ |
| Login sai tối đa | 5 lần/15 phút |
| Thời gian khóa tạm | 15 phút |

`Locked` vẫn là trạng thái khóa quản trị không thời hạn. `locked_until` là khóa
tạm do đăng nhập sai và không thay đổi `status`. Khi hết thời gian, bộ đếm được
xóa và người dùng có thể đăng nhập bình thường.

Các cập nhật bộ đếm và thao tác tạo token reset khóa hàng user bằng
`SELECT ... FOR UPDATE` trên PostgreSQL, tránh mất lượt đếm hoặc tạo hai token
còn hiệu lực khi có request đồng thời.

## 5. Thu hồi session

Flask-Security dùng `fs_uniquifier` làm định danh session. Sau các thao tác:

- người dùng đổi mật khẩu;
- người dùng đặt lại mật khẩu bằng email;
- Admin reset mật khẩu;

hệ thống tạo `fs_uniquifier` mới. Cookie cũ vẫn có thể còn trên trình duyệt,
nhưng không còn ánh xạ được tới user nên bị Flask-Login từ chối ở request kế
tiếp. Phiên đang đổi mật khẩu cũng được logout và yêu cầu đăng nhập lại.

Admin reset tạo mật khẩu tạm bằng `secrets.token_urlsafe(12)` thay cho chuỗi hex
8 ký tự trước đây. Mật khẩu chỉ xuất hiện trong flash của request Admin và không
được ghi vào audit.

## 6. Email và cấu hình

Email reset dùng Flask-Mailman, có cả text và HTML. SMTP credential không được
ghi vào application, audit hoặc log lỗi.

Biến cấu hình mới:

```env
PASSWORD_RESET_TOKEN_TTL_SECONDS=3600
PASSWORD_RESET_COOLDOWN_SECONDS=60
PASSWORD_RESET_MAX_SENDS_PER_HOUR=5
PASSWORD_RESET_MAX_SENDS_PER_IP_HOUR=20
LOGIN_MAX_FAILED_ATTEMPTS=5
LOGIN_FAILURE_WINDOW_SECONDS=900
LOGIN_LOCKOUT_SECONDS=900
```

SMTP thật vẫn dùng các biến chung của A.3.2. Nếu chưa có Gmail App Password,
test sử dụng backend `locmem` và không gửi dữ liệu ra Internet.

## 7. Audit event

| Event | Khi nào ghi |
|---|---|
| `AUTH_PASSWORD_RESET_REQUESTED` | Yêu cầu email reset được tiếp nhận/gửi |
| `AUTH_PASSWORD_RESET` | Token bị từ chối hoặc mật khẩu được reset |
| `AUTH_ACCOUNT_TEMP_LOCKED` | Đạt ngưỡng đăng nhập sai |
| `AUTH_CHANGE_PASSWORD` | Người dùng đổi mật khẩu |
| `USER_RESET_PASSWORD` | Admin reset mật khẩu |

Audit chỉ ghi outcome, username, thời hạn khóa và số lần sai cần thiết. Raw
token, mật khẩu mới và SMTP credential không được ghi.

## 8. File thay đổi

| File | Nội dung |
|---|---|
| `app/models.py` | Trạng thái lockout và model `PasswordResetToken` |
| `app/modules/auth/service.py` | Token reset, email, lockout và session revocation |
| `app/modules/auth/routes.py` | Forgot/reset routes và bảo vệ login/change password |
| `app/modules/auth/admin.py` | Mật khẩu tạm mạnh hơn và revoke session |
| `app/config.py` | Cấu hình reset/lockout và bật recoverable identity |
| `app/__init__.py` | Default `SECURITY_RECOVERABLE` cho test/app cũ |
| `app/templates/auth/forgot_password.html` | Form yêu cầu reset thật |
| `app/templates/auth/reset_password.html` | Form đặt mật khẩu mới |
| `migrations/versions/0004_account_recovery_security.py` | Migration schema A.3.3 |
| `scripts/manage_database.py` | Kiểm tra schema A.3.3 |
| `.env.example` | Danh sách biến cấu hình mới |
| `tests/test_phase_a33_account_security.py` | Acceptance/security test A.3.3 |
| `tests/test_database_migrations.py` | Kiểm tra bảng mới khi dựng database |

## 9. Nghiệm thu

Các ca bắt buộc:

- email không tồn tại nhận phản hồi trung tính và không tạo token;
- token chỉ lưu hash, dùng một lần và có hạn;
- sửa token vẫn thất bại ngay cả khi sửa hash database cho khớp;
- SMTP lỗi không làm mất token và không lộ chi tiết kết nối;
- resend trong cooldown không tạo token thứ hai;
- mật khẩu cũ hết hiệu lực sau reset;
- đăng nhập sai tới ngưỡng sẽ khóa tạm;
- hết thời gian khóa có thể đăng nhập và bộ đếm được xóa;
- đổi/reset mật khẩu làm toàn bộ session cũ hết hiệu lực;
- migration có thể upgrade/downgrade trên database tạm.

Kết quả ngày 2026-09-18:

```text
Migration + A.3.2 + A.3.3: 21 tests OK
Full regression:             159 tests OK, 6 PostgreSQL tests skipped
Python compileall:           OK
```

Sáu test PostgreSQL bị skip vì `POSTGRES_TEST_DATABASE_URL` không được cấu
hình; đây là guard cố ý để test không chạy nhầm vào database runtime.

## 10. Triển khai và rollback

Triển khai runtime khi database hoạt động:

```bash
pg_dump --format=custom --file=<backup.dump> <database>
python scripts/manage_database.py upgrade
python scripts/manage_database.py check
```

Rollback schema chỉ khi đã đánh giá dữ liệu reset/lockout phát sinh:

```bash
python scripts/manage_database.py downgrade 0003_auth_framework_foundation
```

Downgrade sẽ xóa bảng token reset và trạng thái lockout; backup trước migration
là bắt buộc trên runtime.
