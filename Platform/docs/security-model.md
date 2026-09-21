# Security model và RBAC

## Ma trận quyền

| Chức năng | Platform Admin | Project Admin | Developer | Operator | Viewer | Auditor |
|---|---:|---:|---:|---:|---:|---:|
| Quản lý server/cluster | Có | Không | Không | Không | Không | Không |
| Quản lý member/role | Có | Có | Không | Không | Không | Không |
| Tạo/sửa/xóa application | Có | Có | Có | Không | Không | Không |
| Deploy/rollback/scale | Có | Có | Có | Có | Không | Không |
| Xem application/monitoring | Có | Có | Có | Có | Có | Có |
| Xem audit project | Có | Có | Không | Không | Không | Có |

Role được gắn trên `ProjectMembership`, vì vậy cùng một user có thể là Operator
ở project A và Viewer ở project B. Web và REST API gọi chung authorization
service; kiểm tra backend vẫn bắt buộc dù UI đã ẩn action. Mọi application,
deployment, pipeline, monitoring và audit đều được lọc theo project.

## API token

- Token thuộc một user và một project; raw token chỉ hiển thị một lần.
- Database chỉ lưu prefix và SHA-256 hash, token có expiry và có thể revoke.
- Quyền hiệu lực là giao giữa scope token và RBAC hiện tại của user.
- Disable user/membership, archive project hoặc hạ role có hiệu lực ngay.
- Mỗi token có rate limit, last-used time/IP và audit create/use/revoke.
- Token project A không thể dùng cho project B; token thiếu scope trả 403.

`PLATFORM_API_TOKEN` là compatibility credential toàn cục cho client cũ. Nên
chuyển automation sang project token rồi xóa biến này khỏi runtime.

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

RBAC hiện được cưỡng chế trong application layer, chưa phải policy engine độc
lập. Secret store là file permission-restricted, chưa mã hóa at rest ở
application layer. Hướng phát triển: OIDC, OPA/Kyverno, Vault/KMS, token
rotation tự động và security scanning CI.
