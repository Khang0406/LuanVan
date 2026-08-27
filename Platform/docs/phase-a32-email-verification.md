# Phase A.3.2 — Tài khoản và xác minh email

## Mục tiêu

Tài khoản đăng ký mới chỉ được sử dụng Platform sau khi chứng minh quyền sở hữu
email. Hai tài khoản legacy được giữ `Active` để migration không làm gián đoạn
hệ thống hiện hữu.

## Mô hình dữ liệu

`users` được bổ sung:

- `email`: chuẩn hóa lowercase, unique, nullable cho user legacy;
- `status`: `PendingVerification`, `Active`, `Locked`, `Disabled`;
- `email_verified_at`;
- `status_changed_at`.

Bảng `email_verification_tokens` chứa:

- `user_id` và foreign key cascade;
- `token_hash` SHA-256, không lưu token nguyên văn;
- `expires_at`, `used_at`, `created_at`;
- `request_ip` để giới hạn phát hành token theo IP.

Token nguyên văn có 256 bit entropy, chỉ tồn tại tạm thời để tạo liên kết email
và không được persist. Khi gửi lại, token chưa dùng trước đó được đánh dấu đã
dùng. Xác minh thành công cũng thu hồi mọi token còn lại của user.

## Luồng nghiệp vụ

```text
Đăng ký username + email + password
→ tạo user PendingVerification
→ tạo token hash trong database
→ thử gửi SMTP
→ người dùng mở liên kết một lần
→ kiểm tra hash, used_at và expires_at
→ user chuyển Active, ghi email_verified_at
→ đăng nhập bằng username hoặc email
```

SMTP lỗi không rollback user hoặc token. Trang kết quả nói rõ tài khoản đã được
lưu và có thể gửi lại sau. Resend luôn trả phản hồi trung tính cho email không
tồn tại/đã xác minh để giảm khả năng dò tài khoản.

## Rate limit

Các giá trị mặc định:

| Biến | Mặc định | Ý nghĩa |
|---|---:|---|
| `EMAIL_VERIFICATION_TOKEN_TTL_SECONDS` | 3600 | Tuổi thọ token |
| `EMAIL_VERIFICATION_RESEND_COOLDOWN_SECONDS` | 60 | Chờ giữa hai lần gửi |
| `EMAIL_VERIFICATION_MAX_SENDS_PER_HOUR` | 5 | Giới hạn theo user |
| `EMAIL_VERIFICATION_MAX_SENDS_PER_IP_HOUR` | 20 | Giới hạn theo IP |

## Cấu hình SMTP

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
```

Production yêu cầu `PLATFORM_PUBLIC_URL` dùng HTTPS và phải có `SMTP_HOST`,
`SMTP_FROM`. Credentials chỉ lấy từ environment/Secret, không ghi vào log,
database hoặc email.

## Audit event

- `AUTH_REGISTER`
- `AUTH_EMAIL_VERIFICATION_SENT`
- `AUTH_EMAIL_VERIFICATION_RESEND`
- `AUTH_EMAIL_VERIFIED`
- `AUTH_LOGIN` khi Pending/Locked/Disabled bị từ chối
- `AUTH_SESSION_REJECTED` khi trạng thái user không còn Active

Raw token, SMTP password và toàn bộ địa chỉ email không được đưa vào metadata
audit. Access log dùng `request.path`, nên token trong query string của
`/auth/verify-email?token=...` không xuất hiện trong access log.

## Migration và rollback

Revision: `0002_account_email_verification`.

```bash
python scripts/manage_database.py upgrade
python scripts/manage_database.py check
```

Downgrade `0002 → 0001` xóa bảng token và bốn cột identity mới. Chỉ chạy sau
backup; email và trạng thái mới sẽ mất khi downgrade. Vòng downgrade/upgrade đã
được thử trên `platform_test`, không ảnh hưởng các bảng application/delivery.

## Giới hạn còn lại

- SMTP thật cần administrator cung cấp host/credential/domain gửi hợp lệ; lần
  triển khai này kiểm chứng transport bằng mock, chưa gửi email ra Internet.
- Khóa tạm do đăng nhập sai, reset password và revoke toàn bộ session thuộc
  Phase A.3.3.
- User legacy chưa có email; Phase quản trị tài khoản sau cần quy trình thêm và
  xác minh email nếu muốn chuyển họ sang định danh email đầy đủ.
