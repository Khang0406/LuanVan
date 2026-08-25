# Phase A — Nhật ký triển khai (Implementation Log)

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
