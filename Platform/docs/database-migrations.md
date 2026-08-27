# Quản lý migration database

Schema PostgreSQL của Platform được quản lý bằng Alembic. Web và worker không
tự tạo bảng khi khởi động. Luôn chạy migration như một bước riêng trước khi
deploy phiên bản ứng dụng mới.

Web từ chối khởi động và `/readyz` báo lỗi nếu PostgreSQL chưa ở revision
`head`. SQLite chỉ được tự tạo schema trong test/development fallback.

Revision hiện tại: `0002_account_email_verification`.

## Database mới

```bash
source .venv/bin/activate
export DATABASE_URL='postgresql+psycopg2://...'
python scripts/manage_database.py upgrade
python scripts/manage_database.py check
```

## Nhận quản lý database hiện hữu

Baseline `0001_platform_baseline` mô tả schema đã tồn tại trước khi tích hợp
Alembic. Không chạy `upgrade` trực tiếp lần đầu trên database này vì Alembic sẽ
cố tạo lại bảng. Quy trình an toàn:

1. Dừng web và worker hoặc mở maintenance window.
2. Tạo backup PostgreSQL bằng `pg_dump --format=custom` và kiểm tra file khác rỗng.
3. Chạy `python scripts/manage_database.py adopt-existing`.
4. Chạy `python scripts/manage_database.py upgrade` để áp dụng các revision sau baseline.
5. Chạy `python scripts/manage_database.py check`.
6. Khởi động web/worker và kiểm tra `/readyz`, applications, pipeline và lịch sử deploy.

`adopt-existing` chỉ stamp revision sau khi xác nhận đủ bảng và các cột bắt
buộc; nó không sửa record nghiệp vụ.

## Tạo migration tiếp theo

Sau khi cập nhật model SQLAlchemy:

```bash
alembic revision --autogenerate -m "add account identity fields"
alembic upgrade head
```

Phải đọc lại file revision sinh ra trước khi chạy. Các bảng delivery dùng
SQLAlchemy Core được bảo vệ khỏi lệnh drop trong autogenerate.

## Downgrade và khôi phục

```bash
python scripts/manage_database.py downgrade -1
```

Chỉ downgrade revision đã được xác nhận có thể đảo ngược. Không downgrade thấp
hơn baseline trên database có dữ liệu: baseline downgrade xóa toàn bộ bảng.
Nếu migration thay đổi dữ liệu không thể đảo ngược, restore từ backup thay vì
cố chạy downgrade.
