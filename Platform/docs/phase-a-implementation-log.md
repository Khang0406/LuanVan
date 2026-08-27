# Phase A — Nhật ký triển khai (Implementation Log)

## Phase A.3.2 — Tài khoản và xác minh email (2026-08-27)

### Thay đổi schema

- Thêm revision `0002_account_email_verification`.
- `users` có thêm `email`, `status`, `email_verified_at`, `status_changed_at`.
- Thêm bảng `email_verification_tokens` với foreign key cascade, hash unique,
  thời hạn, thời điểm sử dụng, thời điểm tạo và IP yêu cầu.
- Hai user legacy (`admin`, `dev`) được giữ `Active`, email `NULL`; không tạo
  email giả và không buộc xác minh lại sau migration.

### Backend và bảo mật

- Đăng ký mới tạo user `PendingVerification`; chỉ `Active` mới đăng nhập được.
- Email được chuẩn hóa lowercase và kiểm tra unique; đăng nhập hỗ trợ username
  hoặc email.
- Token ngẫu nhiên 256 bit, database chỉ giữ SHA-256; token một lần, có hạn và
  token cũ bị thu hồi khi resend.
- Resend có cooldown, giới hạn theo user/IP và phản hồi trung tính để chống dò
  email.
- SMTP hỗ trợ STARTTLS hoặc SSL, optional authentication và timeout; exception
  không lộ host, username hay password.
- SMTP lỗi không rollback tài khoản/token đã tạo.
- Session của tài khoản không còn `Active` bị từ chối và xóa.
- Production bắt buộc public URL HTTPS, `SMTP_HOST` và `SMTP_FROM`.

### Web/UI và audit

- Form đăng ký có email; trang chờ xác minh và trang gửi lại email.
- Login hiển thị trạng thái chưa xác minh và liên kết resend.
- Admin user list hiển thị email, trạng thái tài khoản.
- Audit các sự kiện đăng ký, gửi/resend, xác minh, từ chối login/session; không
  ghi raw token hoặc SMTP credential.

### File chính

- `migrations/versions/0002_account_email_verification.py`
- `app/models.py`
- `app/email_service.py`
- `app/modules/auth/service.py`
- `app/modules/auth/routes.py`
- `app/templates/auth/{register,verification_sent,resend_verification,login}.html`
- `tests/test_phase_a32_email_verification.py`
- `docs/phase-a32-email-verification.md`

### Migration production và backup

- Backup trước migration:
  `instance/platform-pre-a32-20260827-114952.dump`, mode `0600`, PostgreSQL
  custom format, SHA-256
  `12ed2ae9ba7dbc077c5d6166b4315ae330793909cf555eb3516202ca130a0e0d`.
- Nâng PostgreSQL `0001_platform_baseline → 0002_account_email_verification`
  bằng transactional DDL.
- `alembic check`: không có schema drift.
- Sau migration còn đủ 3 application, 44 pipeline run, 10 deployment, 108
  audit log, 2 user; bảng token ban đầu có 0 record.
- `/healthz`, `/readyz`, `/auth/register` đều trả 200; ba application vẫn là
  `ctu-cinema`, `demo-nginx`, `map`.

### Kiểm thử

- 7 test A.3.2: đăng ký/xác minh/login, token dùng lại, token hết hạn, SMTP lỗi,
  resend rate limit, chống dò email, Locked session/login, SMTP TLS/login và
  production configuration.
- Toàn bộ suite: 147 test đạt; 6 PostgreSQL test skip khi không cấu hình `_test`.
- PostgreSQL integration riêng: 6/6 đạt sau migration `0002`.
- PostgreSQL `platform_test`: downgrade `0002 → 0001 → 0002` thành công và số
  application không đổi.
- SMTP thật chưa được cấu hình; transport được kiểm chứng bằng SMTP mock, không
  có email ngoài hệ thống được gửi trong lần triển khai này.

## Phase A.3.1 — Alembic và database baseline (2026-08-27)

- Thêm Alembic và revision `0001_platform_baseline`, bao phủ `users` cùng 10
  bảng delivery hiện hữu.
- Database mới được dựng hoàn toàn bằng `python scripts/manage_database.py upgrade`.
- Database hiện hữu được kiểm tra bảng/cột rồi `adopt-existing`; thao tác chỉ
  ghi `alembic_version`, không tạo lại bảng hoặc sửa dữ liệu nghiệp vụ.
- Web và worker PostgreSQL không còn tự tạo schema. Cả hai từ chối chạy khi
  revision chưa ở `head`; `/readyz` cũng kiểm tra migration revision.
- Autogenerate bảo vệ các bảng SQLAlchemy Core khỏi bị đề xuất drop.
- Script migration SQLite → PostgreSQL chuyển sang dựng schema bằng Alembic.
- Thêm runbook backup, upgrade, check và downgrade tại
  `docs/database-migrations.md`.

Nghiệm thu:

- PostgreSQL hiện hữu ở revision `0001_platform_baseline` và `alembic check`
  không phát hiện schema drift.
- Sau khi adopt còn đủ 3 application (`ctu-cinema`, `demo-nginx`, `map`), 44
  pipeline run, 10 deployment, 108 audit log và 2 user.
- Backup trước baseline:
  `instance/platform-pre-alembic-20260827-104528.dump` (PostgreSQL custom dump,
  SHA-256 `f039b8e2d3bc79df90f996789ac05a0f536a3ca934cdebd1e1d69c7cd9c003c3`).
- Toàn bộ 140 test mặc định đạt (6 PostgreSQL test được skip khi không cấu hình
  database `_test`); chạy riêng PostgreSQL integration đạt 6/6. Migration còn
  được kiểm tra riêng trên SQLite trắng và bằng vòng downgrade → upgrade.

> Ngày: 2026-08-25
> Phạm vi: Control Plane — **PostgreSQL + Redis + Celery + Realtime (SSE) + REST API**
> Tài liệu thiết kế: `docs/phase-a-postgresql-redis-design.md`

Ghi chép chi tiết những gì đã nâng cấp trong lần code này (đã bao gồm cả
di trú PostgreSQL).

---

## 1. Tóm tắt nâng cấp

| Hạng mục | Trước | Sau |
|---|---|---|
| Delivery store | SQLite (`sqlite3` thô) | **PostgreSQL (JSONB)** + SQLite fallback cho test/dev |
| Task queue | SQLite claim loop (`pipeline_worker.py`) | **Celery + Redis** (có fallback về SQLite) |
| Realtime deploy | Không có — bấm Deploy phải refresh tay | **Redis Pub/Sub + SSE** (`/api/v1/pipeline/<id>/events`) |
| API điều khiển | Không có (chỉ UI routes) | **REST `/api/v1/*`** (token + session) |
| Dependency | Flask, SQLAlchemy, gunicorn | + `celery[redis]`, `redis`, `psycopg2-binary` |

**Quyết định công nghệ (theo thiết kế đã chốt):**
- **Celery** làm queue (D3), **Redis** làm broker + pub/sub (D3/D4), **SSE** làm realtime (D4).
- **PostgreSQL** (D1/D2/D6) qua facade pattern — giữ nguyên 100+ test SQLite không đổi.

---

## 2. File mới

### `app/redis_client.py`
Lazy Redis client. `get_redis()` resolve 1 lần, ping thử, trả `None` nếu
`REDIS_URL` không đặt hoặc Redis không tới được → mọi helper thành no-op.
Giúp dev/test (không có Redis) và SQLite fallback vẫn chạy bình thường.

- `redis_url()`, `get_redis()`, `is_available()`
- `publish(channel, payload)` — publish JSON, silent-fail.
- `subscribe(channel)` — generator đọc pub/sub (dùng cho SSE).

### `app/realtime.py`
Publisher sự kiện pipeline theo envelope ở design doc §8:
`pipeline.stage`, `pipeline.status`, `pipeline.log`. Channel `platform:pipeline:{run_id}`.
Tất cả đều no-op khi không có Redis.

### `app/worker.py`
Celery app `platform`:
- `run_pipeline(pipeline_run_id)` — task chạy `_run_pipeline_safely` (giữ nguyên engine).
- `@worker_ready` — gọi `recover_interrupted_pipeline_runs()` khi worker khởi động
  (tương đương `PipelineWorker.prepare()` của SQLite fallback).
- `task_acks_late=True`, `prefetch_multiplier=1`, concurrency đọc từ
  `PIPELINE_MAX_CONCURRENT` (mặc định 4).

Chạy worker: `celery -A app.worker.celery_app worker --concurrency=4 --loglevel=info`

### `app/modules/api/__init__.py` + `routes.py`
Blueprint `api` (`/api/v1`). Reuse service layer hiện có.

Auth:
- `Authorization: Bearer <PLATFORM_API_TOKEN>` → admin principal (chỉ khi biến env được đặt).
- Khác → Flask-Login session.
- Error handler JSON cho 401/403/404.

Endpoints:
- `GET /api/v1/health` (không cần auth)
- `GET /api/v1/servers` (Admin)
- `GET /api/v1/applications` (theo scope)
- `GET /api/v1/applications/<id>`
- `POST /api/v1/applications/<id>/pipeline` (trigger, 202)
- `GET /api/v1/applications/<id>/pipeline`
- `GET /api/v1/pipeline/<run_id>`
- `GET /api/v1/pipeline/<run_id>/events` (**SSE realtime**)
- `GET /api/v1/monitoring/metrics`

### `tests/test_phase5_api.py`
11 test mới phủ: auth API (token/session/scope), trigger+read pipeline, SSE
(stream fallback polling khi không có Redis), `get_pipeline_run`/
`mark_pipeline_running`, realtime no-op, và enqueue Celery khi Redis available.

---

## 3. File sửa đổi

### `app/delivery_store.py`
Thêm 2 hàm (không đổi chữ ký hiện tại, không ảnh hưởng test cũ):
- `get_pipeline_run(run_id, path=None)` — đọc 1 run theo id (Celery dùng).
- `mark_pipeline_running(run_id, worker_id, path=None)` — chuyển Queued→Running,
  ghi `worker_id`/`claimed_at`/`attempt` (mirror metadata của claim loop cũ).

### `app/modules/pipeline/engine.py`
- Import `pipeline_stage`, `pipeline_status` từ `app.realtime`.
- `_update_stage`: publish `pipeline.stage` sau mỗi lần đổi trạng thái stage.
- `_stop_failed_pipeline` & kết thúc pipeline: publish `pipeline.status`.
- `trigger_pipeline`: nếu Redis available → `run_pipeline.delay(run_id)` (Celery),
  ngược lại giữ nguyên SQLite queue; publish `pipeline.status=Queued`.

### `app/__init__.py`
- Import + đăng ký `api_bp`.

### `app/security.py`
- `validate_csrf` bỏ qua blueprint `api` (API không dùng form; tự auth/scope check).

### `requirements.txt`
- Thêm `celery[redis]>=5.3,<6.0`, `redis>=5.0,<7.0`.

---

## 4. PostgreSQL — di trú delivery store (đã hoàn thành)

### `app/delivery_store_pg.py` (mới)
Backend PostgreSQL dùng **SQLAlchemy Core + psycopg2**, mirror toàn bộ public API
của `delivery_store`:

- `payload TEXT` → **`JSONB`** (đọc lại bằng `payload::text` + `json.loads`).
- `BEGIN IMMEDIATE` → **`SELECT ... FOR UPDATE SKIP LOCKED`** (claim) và
  **`pg_advisory_xact_lock`** (reserve + cấp version deployment).
- `INSERT OR REPLACE`/`INSERT OR IGNORE` → **`ON CONFLICT ... DO UPDATE/DO NOTHING`**.
- Bỏ `PRAGMA` (PostgreSQL tự quản WAL + FK).

### `app/delivery_store.py` — facade/dispatch
- Thêm `_use_postgres(path)`: chọn PG khi `DATABASE_URL` là postgres **và** không
  có `path` tường minh. `path` tường minh luôn là SQLite → 100+ test không đổi.
- Mỗi hàm public thêm guard `if _use_postgres(path): return _pg_backend().<fn>(...)`.
- Private helper SQLite (`_connect`, `_upsert_*`) giữ nguyên.

### `docker-compose.yml` (mới)
`postgres:16-alpine` + `redis:7-alpine` cho dev/integration test.

### `tests/test_postgres_backend.py` (mới)
4 test integration (CRUD, claim/reserve limit, version monotonic đa luồng,
webhook idempotent). Tự **skip** khi `DATABASE_URL` không phải postgres.

### Kiểm chứng end-to-end
Boot app với `DATABASE_URL=postgresql+psycopg2://...`:
`/readyz` → `database: ok`, user seeded vào PG (admin/dev/viewer),
`_use_postgres(None) == True`.

---

## 5. Kiểm thử

- `python -m unittest discover -s tests` → **127/127 OK** (123 + 4 PG test; 4 PG test skip khi không có `DATABASE_URL`).
- PG integration: `DATABASE_URL=... python -m unittest tests.test_postgres_backend` → **4/4 OK** (chạy trên container PostgreSQL thật).
- Smoke Celery eager mode: task `run_pipeline` mark Running → gọi engine → trả status.
- Không regression trên Phase 0–4, cloud runtime, server delete.

---

## 6. Cách chạy (production)

```bash
# web (nhiều replica được phép)
gunicorn -w 4 -b 0.0.0.0:8000 run:app

# worker (thay python -m app.pipeline_worker)
celery -A app.worker.celery_app worker --concurrency=4 --loglevel=info
```

```env
DATABASE_URL=postgresql+psycopg2://platform:...@postgres:5432/platform
REDIS_URL=redis://redis:6379/0
PLATFORM_API_TOKEN=<token-cho-machine-clients>
PIPELINE_MAX_CONCURRENT=4
```

---

## 7. Chính thức chuyển hệ thống sang PostgreSQL (2026-08-25)

### 7.1 Thay đổi

| File | Thay đổi |
|---|---|
| `.env` | Thêm `DATABASE_URL=postgresql+psycopg2://platform:platform@localhost:5432/platform` và `REDIS_URL=redis://localhost:6379/0` |
| `scripts/migrate_sqlite_to_postgres.py` | **Mới** — script migration một lần SQLite → PostgreSQL |
| `.venv/` | Cài thêm `celery[redis]`, `redis`, `psycopg2-binary` (venv người dùng) |
| `docker-compose.yml` | (đã có) postgres:16-alpine + redis:7-alpine |

### 7.2 Dữ liệu đã di trú (SQLite `instance/app.db` → PostgreSQL)

| Bảng | Số bản ghi |
|---|---|
| applications | 3 (`demo-nginx`, `map`, `ctu-cinema`) |
| application_services | 4 |
| pipeline_runs / pipeline_stages | 42 / 252 |
| deployments / deployment_services | 8 / 11 |
| jobs | 29 |
| audit_logs | 91 |
| users | 2 (`admin` id=1, `dev` id=2 — giữ nguyên id + password hash scrypt) |
| delivery_migrations | 1 (marker, chặn JSON re-import) |

### 7.3 Quy trình migration

1. Đọc toàn bộ dữ liệu từ SQLite (dùng `list_*` với `path=instance/app.db` để ép SQLite).
2. `DROP SCHEMA public CASCADE` + tạo lại schema trên PostgreSQL (xóa dữ liệu test cũ).
3. Ghi delivery data qua backend PG (`replace_applications`, `upsert_pipeline_run`, `update_deployment_record`, `replace_jobs`, `replace_audit_logs`).
4. Tạo bảng `users` qua SQLAlchemy `db.create_all()`, insert 2 user giữ nguyên id + hash, rồi `setval` sequence.
5. Ghi marker `phase1-json-import-v1` để app không re-import JSON lúc boot.

### 7.4 Kiểm chứng sau chuyển đổi

- `/readyz` → `database: ok · data_directory: ok · status: ready`
- `/healthz` → `ok`
- `/applications` → **200**, hiển thị đủ 3 app
- `/api/v1/applications` → **200**, `['demo-nginx', 'map', 'ctu-cinema']`
- password hash `admin`/`dev` khớp 100% giữa SQLite và PostgreSQL (đăng nhập giữ nguyên)

### 7.5 Lưu ý vận hành

- SQLite `instance/app.db` **được giữ nguyên** làm backup (không xóa).
- Pipeline queue giờ qua Celery + Redis: cần chạy worker riêng
  `celery -A app.worker.celery_app worker --concurrency=4 --loglevel=info`
  (tương đương `python -m app.pipeline_worker` trước đây).
- Muốn rollback về SQLite: xóa 2 dòng `DATABASE_URL`/`REDIS_URL` trong `.env` rồi restart.

---

## 8. Tối ưu & kiểm tra lần 2 (2026-08-25)

### 8.1 Vấn đề phát hiện & xử lý

| # | Vấn đề | Mức độ | Xử lý |
|---|---|---|---|
| 1 | `delivery_store_pg._engine()` tạo engine mới mỗi lần gọi → rò rỉ connection pool (PostgreSQL lên tới **26 connections** chỉ với web + 1 worker) | Cao | Cache engine thành **singleton** (`pool_size=5`, `max_overflow=10`, `pool_recycle=3600`) → còn **9 connections** |
| 2 | API dùng bearer token gọi `find_accessible_application(..., current_user)` — `current_user` là anonymous → token hợp lệ vẫn bị trả **404** | Cao | Thêm `_ApiPrincipal` + `_principal()`: token → admin principal; thay `current_user` bằng `_principal()` ở mọi access check |
| 3 | `app/worker.py` đặt `load_dotenv()` ở module level → khi `engine.trigger_pipeline` import `app.worker`, nó load `.env` làm lệch test (test chạy SQLite bị dispatch sang PG) | Cao | Bỏ `load_dotenv()` khỏi `app/worker.py`; tạo **`run_worker.py`** (entry script load `.env` trước khi start Celery) |

### 8.2 Kết quả kiểm chứng

- Test suite: **128/128 OK** (thêm 1 test bearer-token access; 4 test PG skip khi không có `DATABASE_URL`).
- PostgreSQL connections: **26 → 9** (hết rò rỉ).
- Bearer token giờ truy cập application đúng: `GET /api/v1/applications/demo-nginx` → 200 (trước là 404).
- Web `/readyz` → `database: ok`; Celery worker chạy qua `python run_worker.py`.

### 8.3 Cách chạy worker (mới)

```bash
python run_worker.py   # load .env rồi start Celery (khuyến nghị)
```

---

## 9. Phase A.1 — Đợt 1: an toàn đa worker (2026-08-25)

### 9.1 Atomic execution gate

- `mark_pipeline_running()` giờ claim nguyên tử duy nhất khi run còn `Queued`.
- Celery redelivery hoặc hai worker nhận cùng `run_id`: chỉ một worker claim
  thành công; worker còn lại trả `not_claimed` và không chạy pipeline.
- Cả SQLite fallback và PostgreSQL đều giữ cùng contract để unit test không lệch
  production.

### 9.2 Worker lease và heartbeat

- Khi claim, run lưu `worker_id`, `claimed_at`, `heartbeat_at`,
  `lease_expires_at` và tăng `attempt`.
- Heartbeat chỉ cập nhật ba trường lease trực tiếp trong JSON/JSONB, không ghi
  lại payload cũ nên không thể làm mất stage/log cập nhật đồng thời.
- `worker_ready` không còn đóng toàn bộ run `Running`; chỉ run có lease hết hạn
  mới chuyển `Interrupted`. Run legacy không có lease được giữ nguyên để tránh
  nhận nhầm là worker chết.
- Cấu hình: `PIPELINE_LEASE_SECONDS=300`,
  `PIPELINE_HEARTBEAT_SECONDS=30`.

### 9.3 Bằng chứng kiểm thử

- Phase A API/worker tests: **15/15 OK**.
- Full SQLite regression: **131 tests OK**, 4 PostgreSQL integration tests skip
  đúng thiết kế khi không đặt `DATABASE_URL`.
- PostgreSQL thật: **6/6 OK**, gồm claim đồng thời hai thread chỉ một thành
  công, owner-only lease renewal, lease expiry recovery, version concurrency và
  webhook idempotency.
- PostgreSQL integration chỉ chạy khi có `POSTGRES_TEST_DATABASE_URL` và tên
  database kết thúc bằng `_test`; test từ chối database runtime để tránh
  `replace_applications()` xóa dữ liệu vận hành.

---

## 10. Phase A.2 — Hardening nền tảng dùng chung (2026-08-26)

### 10.1 Mục tiêu và phạm vi

Đợt này không thêm module nghiệp vụ mới. Thay đổi tập trung vào đường đi chung
của Web, REST API và Celery worker: kiểm tra cấu hình, readiness dependency,
correlation ID, hợp đồng lỗi API và giới hạn tài nguyên task.

### 10.2 Thay đổi theo vấn đề

| # | Trước tối ưu | Sau tối ưu | File |
|---|---|---|---|
| 1 | Production vẫn có thể khởi động với SQLite hoặc không có Redis | Khi `PLATFORM_ENV=production`, hệ thống từ chối khởi động nếu không dùng PostgreSQL, thiếu Redis, cookie không secure hoặc còn secret mặc định | `app/config.py` |
| 2 | Bearer token ngắn có thể được chấp nhận trong production | Token được cấu hình phải dài tối thiểu 32 ký tự; token rỗng vẫn cho phép tắt machine authentication | `app/config.py` |
| 3 | `/readyz` chỉ kiểm tra database và thư mục dữ liệu | Nếu `REDIS_URL` được đặt, readiness ping Redis thật; Redis hỏng trả HTTP 503 nên load balancer không chuyển traffic vào instance chưa sẵn sàng | `app/__init__.py`, `app/redis_client.py` |
| 4 | Khó nối một lỗi UI/API với access log | Mỗi request có `X-Request-ID`, `Server-Timing` và một access log gồm method/path/status/duration/request_id | `app/observability.py`, `app/__init__.py` |
| 5 | API success/error không mang mã truy vết | Envelope API giữ `data/error` để tương thích và thêm `meta.request_id`; error hỗ trợ `details` có cấu trúc | `app/modules/api/routes.py` |
| 6 | Pipeline task có thể giữ worker vô hạn; result backend tăng không giới hạn | Thêm soft/hard time limit, Redis visibility timeout, broker reconnect vô hạn và TTL cho Celery result | `app/worker.py` |
| 7 | Cấu hình mẫu để concurrency bằng 1 | Mẫu mới dùng 4 worker slot và khai báo đầy đủ lease, heartbeat, task timeout, visibility timeout, result expiry | `.env.example` |
| 8 | Chưa có regression test riêng cho hardening | Thêm 5 test cho production guard, request ID, Redis readiness và Celery safety | `tests/test_phase_a_hardening.py` |

### 10.3 Hợp đồng HTTP mới

Mọi response Web/API có hai header:

- `X-Request-ID`: dùng lại giá trị client gửi nếu hợp lệ, nếu không tự sinh UUID.
- `Server-Timing: app;dur=<milliseconds>`: thời gian xử lý trong Flask.

REST API giữ tương thích với client cũ:

```json
{
  "data": {},
  "error": null,
  "meta": {
    "request_id": "acceptance-request-1"
  }
}
```

Khi lỗi, `data=null`; `error` gồm `code`, `message` và có thể có
`details`. Người vận hành dùng `meta.request_id` để tìm đúng access log.

### 10.4 Cấu hình mới và giá trị mặc định

| Biến | Mặc định | Ý nghĩa |
|---|---:|---|
| `PIPELINE_MAX_CONCURRENT` | 4 trong file mẫu | Số task chạy đồng thời trên một worker |
| `PIPELINE_TASK_SOFT_TIME_LIMIT` | 3300 giây | Báo timeout mềm để task có cơ hội kết thúc |
| `PIPELINE_TASK_TIME_LIMIT` | 3600 giây | Worker cưỡng chế dừng task quá hạn |
| `CELERY_VISIBILITY_TIMEOUT` | 7200 giây | Thời gian Redis giữ task đã giao trước khi redelivery |
| `CELERY_RESULT_EXPIRES` | 86400 giây | Xóa result Celery sau 24 giờ |

Ràng buộc: hard time limit luôn lớn hơn soft time limit trong `Config`.
Lease/heartbeat vẫn là cơ chế phục hồi trạng thái pipeline bền vững; Celery
timeout là lớp bảo vệ tài nguyên bổ sung.

### 10.5 Kiểm thử và bằng chứng

- Targeted hardening + API regression: **20/20 OK**.
- Full suite: **138 tests**, **OK**, **6 skipped** (PostgreSQL integration chỉ
  chạy khi đặt `POSTGRES_TEST_DATABASE_URL` trỏ tới database `*_test`).
- Lần chạy đầu dùng biến `TEMP` của Windows/WSL làm 2 test chmod/path thất bại.
  Chạy lại với `TMPDIR=/tmp TEMP=/tmp TMP=/tmp` đạt toàn bộ; đây là khác biệt
  filesystem của môi trường test, không phải lỗi nghiệp vụ.
- `git diff --check`: không có lỗi whitespace.

### 10.6 Hạng mục chủ động chưa đưa vào đợt này

Các việc sau cần một đợt thiết kế/migration riêng, không ghép vào hardening để
tránh rủi ro dữ liệu đang phát triển:

- Alembic và migration version cho cả bảng SQLAlchemy lẫn delivery schema.
- API token đa người dùng lưu dạng hash, scope và expiry trong PostgreSQL.
- Pagination/retention job, audit log, pipeline và deployment.
- Cancel pipeline đang chạy và retry policy phân loại theo lỗi.
- Chuẩn hóa toàn bộ status legacy trong dữ liệu cũ.

Đây là backlog Phase A.3; ưu tiên tiếp theo nên là Alembic + pagination/retention,
sau đó mới token registry và pipeline cancellation.
