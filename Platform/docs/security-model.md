# Security model và RBAC

## Ma trận quyền

| Chức năng | Admin | Developer | Viewer |
|---|---:|---:|---:|
| Xem application, monitoring, log | Có | Có (application sở hữu) | Có |
| Tạo/deploy application | Có | Có | Không |
| Quản lý application secret | Có | Có (application sở hữu) | Không |
| Registry/SMTP toàn hệ thống | Có | Không | Không |
| Quản lý server/cluster | Có | Không | Không |
| Rollback | Có | Có (application sở hữu) | Không |

Developer được kiểm tra `application.user_id`; URL của application/deployment
khác trả 404. Rollback còn kiểm tra `deployment.application_id`. Viewer đọc mọi
application để phục vụ quan sát nhưng endpoint POST mutate bị chặn server-side,
không chỉ ẩn button.

## CSRF và webhook

Mọi POST/PUT/PATCH/DELETE nội bộ yêu cầu session CSRF token. Automated test quét
toàn bộ static POST form. GitHub webhook được miễn CSRF vì không có session,
nhưng chỉ chấp nhận chữ ký `sha256=` HMAC SHA-256 hợp lệ, push đúng branch,
commit SHA hợp lệ và delivery ID chưa xử lý.

## Secret

- Runtime stores ở `instance/` hoặc `/var/lib/platform`, mode 0600 và Git ignore.
- `instance/`, `.env`, `app/data/` runtime và generated manifests bị loại khỏi
  Docker build context; image production không đóng gói state nghiệm thu.
- Payload SQLite/JSON, audit, job, stage output và exception message đi qua
  masking theo cả tên trường nhạy cảm và giá trị secret đã biết.
- Manifest public/preview loại bỏ hoặc mask Secret data.
- Docker login truyền credential qua stdin; Ansible secret qua file tạm 0600.
- Backup mặc định chỉ lưu metadata. Không log environment, Kubernetes Secret,
  Docker config hoặc credential file.

## Session và runtime

Cookie session/remember luôn `HttpOnly`, `SameSite=Lax`; production yêu cầu
`Secure`. Proxy trust count là cấu hình tường minh. Container chạy non-root,
drop capability, seccomp RuntimeDefault và không privilege escalation.

## Secret scan trước bàn giao

Quét tracked/staged content, không in file runtime:

```bash
git ls-files -z | xargs -0 rg -n \
  'gh[pousr]_[A-Za-z0-9_]{20,}|Authorization:[[:space:]]*(Bearer|Basic)[[:space:]]+[^*[:space:]]+|password[=:][^*[:space:]]+'
git diff --check
```

Review từng finding; không đưa giá trị thật vào ticket hoặc báo cáo.

## Giới hạn và hướng phát triển

RBAC hiện là role + ownership trong application layer, chưa phải policy engine
đa tenant. Secret store là file permission-restricted, chưa mã hóa at rest ở
application layer. Hướng phát triển: OIDC, policy engine, Vault/KMS, PostgreSQL,
key rotation và security scanning CI.
