# Phase A.4.1 — Project và thành viên

## 1. Kết quả

A.4.1 thay phạm vi application theo owner cá nhân bằng tenant `Project`.
Một user có thể tham gia nhiều project, chọn project đang làm việc trên Web và
chỉ nhìn thấy application thuộc project đó.

Mô hình sau thay đổi:

```text
User
 └── ProjectMembership
      └── Project
           └── Application
```

Role/permission chi tiết chưa được đưa vào membership ở bước này. A.4.1 chỉ xác
lập ranh giới tenant, owner và trạng thái thành viên; A.4.2 sẽ bổ sung RBAC theo
project mà không phải đổi lại quan hệ dữ liệu.

## 2. Schema và migration

Revision mới: `0005_projects_memberships`, tiếp sau
`0004_account_recovery_security`.

### Bảng `projects`

| Cột | Mục đích |
|---|---|
| `id` | Khóa chính nội bộ |
| `name` | Tên hiển thị |
| `slug` | Định danh duy nhất |
| `description` | Mô tả project |
| `status` | `Active` hoặc `Archived` |
| `owner_user_id` | Người quản lý membership trước A.4.2 |
| `created_at`, `updated_at` | Lịch sử thời gian |

### Bảng `project_memberships`

| Cột | Mục đích |
|---|---|
| `project_id`, `user_id` | Một cặp duy nhất |
| `status` | `Active` hoặc `Disabled` |
| `invited_by_user_id` | Người thêm/kích hoạt thành viên |
| `created_at`, `updated_at` | Lịch sử thời gian |

`applications` có thêm `project_id` không nullable, foreign key tới `projects`
và index để lọc theo tenant.

## 3. Backfill dữ liệu cũ

Migration luôn tạo:

```text
name = Default Project
slug = default-project
```

Quy tắc chuyển đổi:

1. chọn Platform Admin đầu tiên làm owner; nếu chưa có user thì owner tạm `NULL`;
2. thêm tất cả user hiện hữu vào Default Project với trạng thái `Active`;
3. gán toàn bộ application cũ vào Default Project;
4. cập nhật cả cột `applications.project_id` và `project_id` trong JSON/JSONB
   payload;
5. mới chuyển cột thành `NOT NULL` và tạo foreign key.

Cách này giữ nguyên ba application, lịch sử pipeline/deployment và quyền truy
cập hiện có sau migration. Database trắng được Alembic migrate trước khi seed
user; `ensure_default_project()` hoàn thiện owner/membership sau bootstrap.

## 4. Chọn project hiện tại

Topbar có selector project. ID được lưu trong session dưới
`active_project_id`, nhưng server luôn kiểm tra lại membership ở mỗi request.

- membership bị disable/remove: project không còn hợp lệ và session tự chuyển
  sang project khả dụng đầu tiên;
- không có project: application list rỗng và người dùng được hướng dẫn tạo hoặc
  tham gia project;
- Platform Admin nhìn thấy mọi project nhưng Web vẫn chỉ hiển thị dữ liệu của
  project đang chọn.

Các màn hình được scope theo project hiện tại:

- Applications và toàn bộ thao tác detail/deploy/scale/log/rollback;
- Deployments;
- pipeline/job liên quan application;
- CI/CD history;
- application metrics và alert.

## 5. Quản lý project và thành viên

Người dùng global `Admin` hoặc `Developer` có thể tạo project. Người tạo trở
thành owner và membership `Active`.

Owner hoặc Platform Admin có thể:

- thêm tài khoản hiện hữu bằng username/email;
- kích hoạt lại membership đã disable;
- disable membership;
- xóa membership.

Không thể disable hoặc xóa owner. Xóa membership không xóa project/application.
A.4.1 chưa gửi invitation email cho địa chỉ chưa có tài khoản; đây là phạm vi
có chủ đích để tránh trộn identity workflow với RBAC A.4.2.

## 6. Cô lập Web và API

Application có `project_id` sẽ được authorize bằng membership `Active`. Kiểm
tra backend được thực hiện kể cả khi UI không hiển thị liên kết.

API mới:

```text
GET /api/v1/projects
GET /api/v1/applications?project_id=<id>
```

Nếu user yêu cầu project không thuộc phạm vi, API trả `403 FORBIDDEN`. API list
không truyền `project_id` trả các application thuộc toàn bộ project mà user là
thành viên; không trả project ngoài membership. API token project-scoped được
thực hiện ở A.4.3.

Các record test/SQLite legacy chưa có `project_id` tạm dùng authorization
`user_id` cũ để không phá compatibility. Runtime PostgreSQL sau migration 0005
không còn application thiếu project.

## 7. Audit

Các event mới:

- `PROJECT_CREATE`;
- `PROJECT_SELECT`;
- `PROJECT_MEMBER_ADD`;
- `PROJECT_MEMBER_STATUS`;
- `PROJECT_MEMBER_REMOVE`.

Audit metadata lưu `project_id` và `member_user_id`, không lưu email đầy đủ hoặc
credential.

## 8. File thay đổi

| File | Nội dung |
|---|---|
| `app/models.py` | Model Project/Membership và quan hệ User |
| `migrations/versions/0005_projects_memberships.py` | Schema và backfill dữ liệu cũ |
| `app/modules/projects/service.py` | Tenant selection và membership service |
| `app/modules/projects/routes.py` | Route Web quản lý project/member |
| `app/templates/projects/*` | Danh sách, tạo và chi tiết project |
| `app/templates/base.html` | Navigation và project selector |
| `app/modules/applications/service.py` | Authorization/application project scope |
| `app/delivery_store.py` | Persist `project_id` trên SQLite fallback |
| `app/delivery_store_pg.py` | Persist `project_id` trên PostgreSQL |
| `app/ui/routes.py` | Scope application/deployment/job/CI/CD/monitoring |
| `app/ui/mock_data.py` | Dashboard theo project hiện tại |
| `app/modules/api/routes.py` | Projects API và project filter |
| `app/__init__.py` | Register blueprint, bootstrap và template context |
| `scripts/manage_database.py` | Schema check A.4.1 |
| `tests/test_database_migrations.py` | Backfill/round-trip migration |
| `tests/test_phase_a41_projects.py` | Acceptance test tenant isolation |

## 9. Kiểm thử

Acceptance bao phủ:

- database trắng dựng tới revision 0005;
- application legacy được backfill vào Default Project;
- user legacy trở thành thành viên mà không mất dữ liệu;
- Web không hiển thị và không mở trực tiếp application project khác;
- API không trả project khác và từ chối filter trái phép;
- một user tham gia nhiều project;
- disable membership thu hồi quyền truy cập;
- non-owner không quản lý được membership;
- owner không thể bị xóa;
- application mới nhận đúng `project_id` đang chọn;
- project archived không còn cấp quyền đọc application cho member/Admin;
- job application của đồng đội cùng project được hiển thị, job project khác bị
  loại khỏi phạm vi;
- refresh monitoring cục bộ không resolve alert của tenant khác;
- project member không đọc được server/node chart toàn cụm;
- tên project và membership trùng trả lỗi nghiệp vụ ổn định;
- `alembic check` không phát hiện schema drift.

Kết quả cuối được ghi tại đầu `docs/phase-a-implementation-log.md`.

## 10. Tối ưu và đồng bộ sau nghiệm thu

### Truy vấn

Trước tối ưu, `load_accessible_applications()` gọi kiểm tra membership cho từng
application. Với `N` application, request có thể phát sinh `N` truy vấn quyền.
Sau tối ưu, service tải tập `project_id` hợp lệ một lần rồi lọc trong bộ nhớ.
Template context cũng tái sử dụng danh sách project để xác định active project,
không tải lại cùng dữ liệu.

### Phạm vi tenant liên module

- `Active Project` là nguồn scope chung cho application, dashboard, deployment,
  pipeline, job và monitoring.
- Job gắn `application_id` dùng project scope, không dùng riêng `actor_id`, nên
  lịch sử là tài sản của nhóm. Job hạ tầng không gắn application vẫn chỉ hiện
  cho người tạo hoặc Platform Admin.
- Alert lifecycle chỉ resolve alert application nằm trong tập application đang
  được refresh. Alert của project không được tải vẫn giữ nguyên trạng thái.
- Server, node metrics và chart toàn cụm chỉ dành cho Platform Admin; member
  tiếp tục xem application metrics đã lọc theo project.
- Project archived không còn được xem như project truy cập hợp lệ.

### Độ bền database

- Unique constraint vẫn là lớp quyết định cuối khi hai request tạo cùng slug
  hoặc membership đồng thời. Service bắt `IntegrityError`, rollback transaction
  và trả `ValueError` có thể hiển thị an toàn trên Web.
- Migration đọc toàn bộ payload application trước khi update để tương thích
  cách cursor hoạt động của PostgreSQL driver.

### Đồng bộ với A.3

Kiểm tra token xác minh email/reset mật khẩu đã dùng API hiện hành của
Flask-Security. Hai lớp bảo vệ vẫn giữ nguyên: chữ ký/identity/hash của framework
và bản ghi SHA-256 one-time có expiry của Platform. Thay đổi này loại bỏ cảnh
báo deprecated từ hai workflow mà không nới lỏng xác thực.

## 11. Triển khai và rollback

Runtime hiện không hoạt động, vì vậy chưa áp dụng revision 0005 lên PostgreSQL
thật. Khi database khả dụng:

```bash
pg_dump --format=custom --file=<backup.dump> <database>
python scripts/manage_database.py upgrade
python scripts/manage_database.py check
```

Sau upgrade phải kiểm tra số application/pipeline/deployment và xác nhận mọi
application có `project_id`.

Rollback về A.3.3:

```bash
python scripts/manage_database.py downgrade 0004_account_recovery_security
```

Downgrade xóa Project/Membership và `applications.project_id`; phải backup trước
vì membership tạo sau migration sẽ bị mất.
