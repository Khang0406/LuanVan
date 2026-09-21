# Phase A.4.2 — RBAC theo project

## 1. Mục tiêu và kết quả

A.4.2 thay các kiểm tra chuỗi role toàn cục trên application bằng quyền theo
membership của từng project:

```text
User
 └── ProjectMembership
      └── RBACRole
           └── RBACPermission
```

Một user có thể là Operator ở project A và Viewer ở project B. Quyền được kiểm
tra lại ở backend cho mỗi resource; ẩn nút trên UI chỉ phục vụ trải nghiệm.

Platform Admin vẫn được nhận diện bởi `User.role == Admin`, có quyền toàn hệ
thống đối với server, cluster và mọi project. Các role dưới đây chỉ có hiệu lực
trong project của membership.

## 2. Ma trận role và permission

| Permission | Project Admin | Developer | Operator | Viewer | Auditor |
|---|:---:|:---:|:---:|:---:|:---:|
| `project:manage` | ✓ | | | | |
| `member:manage` | ✓ | | | | |
| `application:create` | ✓ | ✓ | | | |
| `application:read` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `application:update` | ✓ | ✓ | | | |
| `application:delete` | ✓ | ✓ | | | |
| `deployment:execute` | ✓ | ✓ | ✓ | | |
| `deployment:rollback` | ✓ | ✓ | ✓ | | |
| `service:scale` | ✓ | ✓ | ✓ | | |
| `monitoring:read` | ✓ | ✓ | ✓ | ✓ | ✓ |
| `audit:read` | ✓ | | | | ✓ |
| `quota:manage` | | | | | |

`quota:manage` đã được khai báo để A.5 sử dụng nhưng chưa cấp cho role project.
Platform Admin bypass permission này; Project Admin sẽ gửi yêu cầu nâng quota
thay vì tự thay đổi giới hạn.

## 3. Schema và migration

Revision: `0006_project_rbac`, tiếp sau `0005_projects_memberships`.

### Bảng mới

- `rbac_roles`: key, tên hiển thị, mô tả và cờ system role;
- `rbac_permissions`: permission key duy nhất và mô tả;
- `rbac_role_permissions`: quan hệ nhiều-nhiều role–permission.

`project_memberships` có thêm `role_id NOT NULL`, foreign key `RESTRICT` tới
`rbac_roles` và index phục vụ authorization lookup.

### Quy tắc backfill

| Membership cũ | Role mới |
|---|---|
| Owner project | Project Admin |
| User global `Admin` | Project Admin |
| User global `Developer` | Developer |
| User global `Viewer` | Viewer |

Migration seed bằng natural key rồi truy vấn lại ID, không giả định role hoặc
permission bắt đầu từ ID 1. Sau khi toàn bộ membership có role, cột mới được đổi
sang `NOT NULL`.

## 4. Authorization service dùng chung

File `app/modules/authorization/service.py` là nguồn policy duy nhất:

- `has_permission(user, permission, project_id)`;
- `permission_keys(user, project_id)`;
- `get_membership(user, project_id)`;
- `get_role(role_key)`;
- `ensure_rbac_catalog()`.

Web route, REST API và Jinja UI đều gọi cùng service này. Platform Admin được
bypass trong service, thay vì mỗi route tự viết ngoại lệ riêng.

## 5. Enforcement ở backend

### Application và delivery

- create → `application:create`;
- xem detail/manifest/history → `application:read`;
- secret/registry → `application:update`;
- xóa application/workload → `application:delete`;
- deploy/pipeline/restart/retry → `deployment:execute`;
- rollback → `deployment:rollback`;
- scale → `service:scale`;
- pod log/monitoring → `monitoring:read`.

Helper Web resolve application trước để chống truy cập chéo project, sau đó
kiểm tra permission theo chính `application.project_id`. Thiếu permission trả
HTTP 403 và ghi `ACCESS_DENIED`; resource ngoài tenant tiếp tục trả 404.

REST API kiểm tra cùng permission cho application detail, trigger pipeline,
pipeline history/run/event và monitoring. Việc gọi endpoint trực tiếp không thể
vượt qua nút đã bị ẩn trên Web.

### Project member

Chỉ `member:manage` mới được thêm, đổi role, disable hoặc remove membership.
Form thêm member yêu cầu chọn role. Thay đổi role sinh audit
`PROJECT_MEMBER_ROLE` với old/new role. Owner luôn phải giữ Project Admin.

### Monitoring và audit

Metric/log application dùng `monitoring:read` và vẫn scope theo active project.
Node/server chart toàn cụm chỉ Platform Admin được xem.

Project Admin/Auditor có `audit:read`. Với user không phải Platform Admin, trang
audit chỉ lấy event có `metadata.project_id` trùng active project. Event
application, pipeline và registry tự bổ sung project ID khi ghi audit.

## 6. UI

Template context cung cấp:

- `project_permissions`;
- `has_project_permission(permission)`;
- `active_project_role`.

Nút create/deploy/delete/rollback/config secret được render theo permission.
Badge trên topbar hiển thị role của active project thay vì role tài khoản cũ.
Project detail hiển thị role từng member và form đổi role cho người có quyền.

## 7. API response project

`GET /api/v1/projects` bổ sung:

```json
{
  "id": 1,
  "name": "Default Project",
  "my_role": "Operator",
  "permissions": [
    "application:read",
    "deployment:execute",
    "deployment:rollback",
    "monitoring:read",
    "service:scale"
  ]
}
```

API token project-scoped chưa nằm trong revision này. Global
`PLATFORM_API_TOKEN` cũ vẫn được xem như Platform Admin để giữ tương thích;
A.4.3 sẽ thay bằng token hash, scope, expiry và revocation.

## 8. File thay đổi

| File | Nội dung |
|---|---|
| `app/models.py` | Model role/permission, association và membership role |
| `migrations/versions/0006_project_rbac.py` | Schema, seed, backfill và rollback |
| `app/modules/authorization/service.py` | Catalog và authorization service chung |
| `app/modules/projects/service.py` | Gán/đổi role và member management theo permission |
| `app/modules/projects/routes.py` | Endpoint thêm member/chuyển role và audit |
| `app/modules/api/routes.py` | Permission enforcement và project permission response |
| `app/ui/routes.py` | Permission enforcement tại application/monitoring/audit |
| `app/modules/audit/service.py` | Gắn project ID cho event application/pipeline |
| `app/__init__.py` | Bootstrap catalog và Jinja permission context |
| `app/templates/base.html` | Badge role và navigation theo quyền |
| `app/templates/projects/detail.html` | Chọn/đổi role membership |
| `app/templates/applications/*` | Action button theo permission |
| `scripts/manage_database.py` | Kiểm tra schema revision 0006 |
| `tests/test_phase_a42_rbac.py` | Acceptance matrix Web/API/audit |
| `tests/test_database_migrations.py` | Backfill role và migration round-trip |

## 9. Kiểm thử local

Acceptance kiểm tra:

- đầy đủ ma trận permission;
- một user có role khác nhau giữa hai project;
- Operator scale được nhưng không xóa app/quản lý member;
- Viewer đọc app nhưng gọi API pipeline bị 403;
- Project Admin đổi role member;
- owner không thể bị hạ khỏi Project Admin;
- Auditor chỉ đọc audit active project;
- migration backfill owner/Admin/Developer/Viewer;
- upgrade/downgrade toàn bộ migration;
- Alembic không phát hiện schema drift.

Kết quả chính xác được ghi ở đầu `docs/phase-a-implementation-log.md`.

## 10. Nghiệm thu khi máy ảo sẵn sàng

Chưa áp dụng 0006 lên PostgreSQL/K3s runtime trong đợt local này. Trình tự test:

```bash
pg_dump --format=custom --file=<backup-before-rbac.dump> <database>
python scripts/manage_database.py upgrade
python scripts/manage_database.py check
```

Sau đó xác nhận:

1. revision là `0006_project_rbac`;
2. không có membership thiếu `role_id`;
3. ba application cũ vẫn thuộc Default Project;
4. tạo user test cho từng role;
5. Operator deploy/scale được nhưng không quản lý member/xóa app;
6. Viewer gọi API mutation nhận 403;
7. Auditor không thấy audit project khác;
8. Platform Admin vẫn quản lý server/cluster và mọi project;
9. deploy, scale, rollback thật trên K3s không bị ảnh hưởng.

Nếu cần rollback schema:

```bash
python scripts/manage_database.py downgrade 0005_projects_memberships
```

Rollback xóa role/permission assignment A.4.2, vì vậy bắt buộc giữ backup.
