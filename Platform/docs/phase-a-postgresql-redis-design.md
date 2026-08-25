# Thiết kế Phase A — Control Plane: PostgreSQL + Redis + Realtime + API

> Trạng thái: **Thiết kế (chưa code)**
> Mục tiêu: nâng nền tảng từ "PaaS nội bộ chạy SQLite đơn worker" thành
> **Control Plane** scale ngang, có khả năng hiển thị trạng thái deploy theo
> thời gian thực và có REST API để các hệ thống khác gọi.

---

## 1. Mục tiêu & phạm vi

1. **Di trú delivery store từ SQLite → PostgreSQL** (bỏ giới hạn 1 web replica + 1 worker).
2. **Thay SQLite task queue bằng Redis + Celery** (nhiều worker đồng thời).
3. **Realtime pipeline progress** — UI cập nhật tiến trình deploy (6 stage + log) mà không cần refresh.
4. **REST API `/api/v1/*`** — lớp điều khiển lập trình được.
5. **Redis cache** cho metrics monitoring (chia sẻ giữa nhiều web replica).

**Ngoài phạm vi Phase A** (để Phase B/C/D/E): service mesh, tracing, log tập trung, GitOps, OIDC/Vault.

---

## 2. Bối cảnh hiện trạng (đã khảo sát)

| Thành phần | Hiện trạng | Vị trí |
|---|---|---|
| Delivery store | `sqlite3` thô, 9 bảng trong `SCHEMA`, claim bằng `BEGIN IMMEDIATE` | `app/delivery_store.py` |
| User/auth | Flask-SQLAlchemy, bảng `users` | `app/models.py`, `app/db.py` |
| Config DB | `DATABASE_URL` (SQLAlchemy) và `DELIVERY_DATABASE_PATH` (delivery store) tách rời | `app/config.py`, `app/delivery_store.py:32` |
| Pipeline engine | 6 stage `SOURCE→BUILD→TEST→PUSH→DEPLOY→VERIFY` | `app/modules/pipeline/engine.py:316` |
| Worker | Poll loop `claim_next_pipeline_run` mỗi 2s | `app/pipeline_worker.py` |
| Queue limit | `PIPELINE_MAX_CONCURRENT` mặc định `1`, 1 pipeline/application | `app/delivery_store.py:614` |
| Realtime hiện tại | Monitoring dùng AJAX polling; **pipeline không có realtime** (redirect + flash) | `app/ui/routes.py:605`, `:922` |
| Cache monitoring | In-memory `_cache` + ring buffer (không chia sẻ giữa replica) | `app/modules/monitoring/collector.py:37` |

**Hệ quả:** hiện tại bấm "Deploy" → `trigger_pipeline` enqueue → `redirect + flash`
(`ui/routes.py:605-625`), UI chỉ thấy thông báo tĩnh, phải refresh tay để xem
tiến trình. Worker chạy ngầm và không có kênh phản hồi về UI.

---

## 3. Kiến trúc mục tiêu

```mermaid
flowchart LR
    Browser[Browser] -->|SSE /api/v1/pipeline/events| Web[Flask Gunicorn N replica]
    Client[CLI / CI / service] -->|REST /api/v1| Web
    Web --> PG[(PostgreSQL)]
    Web --> Redis[(Redis)]
    Redis -->|broker| Worker[Celery Worker N]
    Worker -->|SELECT FOR UPDATE SKIP LOCKED| PG
    Worker -->|PUBLISH pipeline:*| Redis
    Web -->|SUBSCRIBE pipeline:*| Redis
    Worker --> K8s[K3s cluster]
    Web --> K8s
```

Vai trò từng tầng:

- **PostgreSQL**: nguồn dữ liệu chuẩn duy nhất cho application/pipeline/deployment/
  job/audit/webhook + `users`. Thay `sqlite3` thô bằng SQLAlchemy Core.
- **Redis**: (1) broker Celery, (2) pub/sub realtime, (3) cache metrics.
- **Celery Worker**: thay `pipeline_worker.py` claim loop; giữ nguyên logic
  `_run_pipeline_safely`.

---

## 4. Quyết định thiết kế (đã chốt, kèm lý do)

| # | Quyết định | Lựa chọn | Lý do |
|---|---|---|---|
| D1 | Lớp SQL | **SQLAlchemy Core** (không ORM cho delivery store) | SQL portable SQLite↔PostgreSQL, tái dùng `db.py` đang có; tránh viết lại 9 bảng |
| D2 | Giữ public API `delivery_store` | **Không đổi chữ ký** (`path: Path \| None`) | 112 test truyền `path=` (SQLite temp) không phải sửa |
| D3 | Queue | **Celery + Redis broker** | Chuẩn công nghiệp, sẵn retry/visibility timeout; RQ là phương án nhẹ nếu muốn |
| D4 | Realtime transport | **SSE** (Server-Sent Events) | Chỉ cần push 1 chiều; đơn giản hơn WebSocket, chạy qua gunicorn được |
| D5 | Di trú | **Big-bang có backup + feature flag** | Đã có backup/restore; flag `DATABASE_URL`/`REDIS_URL` để rollback nhanh về SQLite |
| D6 | User DB vs delivery store | **Gộp 1 PostgreSQL instance**, schema riêng | Đơn giản vận hành; `users` do SQLAlchemy ORM, delivery store do Core |

---

## 5. Schema PostgreSQL

### 5.1 Nguyên tắc

- Giữ 9 bảng hiện tại, đổi:
  - `payload TEXT` → **`JSONB`** (query/đánh index theo field khi cần).
  - `INTEGER` boolean flag → **`BOOLEAN`** (hoặc giữ `SMALLINT` để tương thích SQLite khi test).
  - Bỏ `PRAGMA journal_mode=WAL` (PostgreSQL tự có WAL).
  - `INSERT OR REPLACE` → `INSERT ... ON CONFLICT ... DO UPDATE`.
- `users` giữ nguyên theo `app/models.py`, quản lý bằng Alembic.
- Dùng **Alembic** cho cả delivery store lẫn users (thay bảng `delivery_migrations` thủ công).

### 5.2 Danh sách bảng (map từ `delivery_store.SCHEMA`)

| Bảng | Khóa chính | Thay đổi so với SQLite |
|---|---|---|
| `applications` | `id` | `payload JSONB`; `namespace` UNIQUE giữ nguyên |
| `application_services` | `(application_id, name)` | `payload JSONB` |
| `pipeline_runs` | `id` | `payload JSONB`; thêm `worker_id`, `claimed_at`, `attempt` làm cột rõ (thay vì nằm trong JSON) |
| `pipeline_stages` | `(pipeline_run_id, name)` | `payload JSONB` |
| `deployments` | `id` | `payload JSONB`; `UNIQUE(application_id, version)` giữ nguyên |
| `deployment_services` | `(deployment_id, service_name)` | `payload JSONB` |
| `jobs` | `id` | `payload JSONB` |
| `audit_logs` | `id` | `payload JSONB` |
| `webhook_deliveries` | `delivery_id` | `payload JSONB` |
| `users` | `id` | không đổi |

### 5.3 Điểm cần xử lý khi port

1. **Claim nguyên tử** (`delivery_store.py:570`): thay `BEGIN IMMEDIATE` bằng:
   ```sql
   SELECT ... FROM pipeline_runs
   WHERE status = 'Queued'
   ORDER BY created_at, id
   FOR UPDATE SKIP LOCKED
   LIMIT 1;
   ```
2. **Version monotonic** (`delivery_store.py:775` `create_deployment_record`):
   thay `BEGIN IMMEDIATE` + `MAX(version)+1` bằng lock dòng trên `applications`
   hoặc cột `version` dùng sequence; đảm bảo 2 worker không trùng version.
3. **Giới hạn đồng thời** (`reserve_pipeline_run`): giữ ngữ nghĩa "1 pipeline/application
   + `PIPELINE_MAX_CONCURRENT` toàn cục", nhưng dùng `SELECT ... FOR UPDATE` và
   unique partial index thay cho `BEGIN IMMEDIATE`.

### 5.4 Ví dụ DDL (đoạn trích)

```sql
CREATE TABLE IF NOT EXISTS pipeline_runs (
    id            TEXT PRIMARY KEY,
    application_id TEXT NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
    status        TEXT NOT NULL,
    trigger_type  TEXT NOT NULL DEFAULT 'Manual',
    actor         TEXT NOT NULL DEFAULT 'system',
    actor_id      INTEGER,
    actor_role    TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL DEFAULT '',
    updated_at    TEXT NOT NULL DEFAULT '',
    finished_at   TEXT NOT NULL DEFAULT '',
    worker_id     TEXT NOT NULL DEFAULT '',
    claimed_at    TEXT NOT NULL DEFAULT '',
    attempt       INTEGER NOT NULL DEFAULT 0,
    payload       JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS ix_pipeline_runs_claim
    ON pipeline_runs(status, created_at, id) WHERE status = 'Queued';
```

---

## 6. Tầng data access — giữ tương thích 112 test

**Vấn đề:** test hiện tại gọi `replace_applications([...], database)` với `database`
là `Path` trỏ tới file SQLite temp. Không được đổi chữ ký.

**Giải pháp:** thêm hàm phân giải engine bên trong `delivery_store`, giữ toàn bộ
hàm public hiện tại:

```python
def _engine(path: Path | None = None) -> Engine:
    if path is not None:
        return create_engine(f"sqlite:///{path}")          # test/dev tường minh
    url = os.getenv("DATABASE_URL", "")
    if url:
        return create_engine(url)                           # production PostgreSQL
    return create_engine(f"sqlite:///{database_path()}")    # dev mặc định
```

- Test: `path=` → SQLite (giữ nguyên hành vi).
- Production: đặt `DATABASE_URL=postgresql+psycopg://...` → PostgreSQL.
- `readyz` (`__init__.py:79`) giữ nguyên gọi `check_database()`.

**Danh sách hàm giữ nguyên chữ ký** (để test không đổi):
`initialize_schema`, `check_database`, `database_path`, `migrate_json_state`,
`migrate_default_json_state`, `replace_applications`, `list_applications`,
`upsert_pipeline_run`, `list_pipeline_runs`, `claim_next_pipeline_run`,
`has_active_pipeline`, `reserve_pipeline_run`, `replace_jobs`, `list_jobs`,
`replace_audit_logs`, `list_audit_logs`, `create_deployment_record`,
`update_deployment_record`, `get_deployment`, `list_deployments`,
`claim_webhook_delivery`.

---

## 7. Redis + Celery — queue pipeline

### 7.1 Task định nghĩa

```python
# app/worker.py (mới)
from celery import Celery

celery_app = Celery("platform", broker=os.getenv("REDIS_URL"), backend=os.getenv("REDIS_URL"))

@celery_app.task(bind=True, acks_late=True, autoretry_for=(Exception,), max_retries=2)
def run_pipeline(self, pipeline_run_id: str) -> None:
    run = get_pipeline_run(pipeline_run_id)          # đọc từ PostgreSQL
    _run_pipeline_safely(run)                         # giữ nguyên engine hiện tại
```

### 7.2 Thay đổi điểm enqueue

- `engine.trigger_pipeline` (`engine.py:593`): thay `reserve_pipeline_run(pipeline_run)`
  bằng `run_pipeline.delay(run_id)`. Vẫn ghi row `Queued` vào PostgreSQL trước
  để UI/API thấy trạng thái ngay.
- `pipeline_worker.py`: không còn claim loop. Thay bằng lệnh
  `celery -A app.worker worker --concurrency=<N> --loglevel=info`.
- `PIPELINE_MAX_CONCURRENT` chuyển thành `--concurrency` của Celery; giới hạn
  "1 pipeline/application" giữ ở tầng PostgreSQL (unique partial index trên
  `(application_id) WHERE status IN ('Queued','Running')`).

### 7.3 Ngữ nghĩa giữ nguyên

| Hành vi hiện tại | Cách giữ trong Celery |
|---|---|
| Worker restart → run `Running` bị đánh `Interrupted` (`engine.recover_interrupted_pipeline_runs`) | Giữ `prepare()` ở `worker_init`/Celery signal `worker_ready` |
| Retry thủ công tạo run mới | Không đổi `retry_pipeline` |
| Claim tăng `attempt`, gán `worker_id` | Celery `self.request.id` làm `worker_id`, `self.request.retries` làm attempt |

---

## 8. Pub/Sub — định dạng message realtime

### 8.1 Kênh (channel)

| Kênh | Nội dung | Publisher |
|---|---|---|
| `platform:pipeline:{run_id}` | trạng thái stage + status pipeline | Celery worker (`_update_stage`) |
| `platform:pipeline:{run_id}:logs` | từng dòng log (đã mask secret) | Celery worker |
| `platform:alerts` | alert fire/resolve (dự phòng Phase C) | `alerting.collect_and_persist` |

### 8.2 Message envelope

```json
{
  "event": "pipeline.stage",
  "pipeline_run_id": "run-map-4f2a...",
  "application_id": "map",
  "stage": "DEPLOY",
  "status": "Running",
  "message": "Deploying to K3s cluster...",
  "ts": "2026-08-25T09:00:00Z"
}
```

Các giá trị `event`: `pipeline.stage`, `pipeline.status` (Queued/Running/Success/
Failed/Interrupted), `pipeline.log`.

### 8.3 Điểm chèn publish

- `engine._update_stage` (`engine.py:97`): sau khi `_save_pipeline_run`, publish
  `pipeline.stage`.
- `engine._stop_failed_pipeline` / kết thúc pipeline: publish `pipeline.status`.
- Log: bọc `add_activity` + `get_application_logs` qua một `publish_log` (luôn đi
  qua `mask_secrets`).

---

## 9. Realtime transport — SSE endpoint

### 9.1 Endpoint

```
GET /api/v1/pipeline/{run_id}/events
Authorization: Bearer <token> hoặc session cookie
Accept: text/event-stream
```

### 9.2 Luồng

1. Web nhận request → kiểm tra `can_access_application(application, user)`
   (scope application, giống `ui/routes.py:_get_authorized_application`).
2. Web đăng ký subscribe `platform:pipeline:{run_id}` (listener thread dùng
   `redis.asyncio`/`redis-py` pubsub).
3. Mỗi message → ghi vào stream theo format SSE:
   ```
   event: pipeline.stage
   data: {"stage":"DEPLOY","status":"Running",...}
   ```
4. Khi pipeline kết thúc (nhận `pipeline.status` terminal) → đóng stream.

### 9.3 Ghi chú

- **Fallback:** nếu không có Redis (dev), frontend tự polling `/api/v1/pipeline/{run_id}`
  mỗi 2s (giữ hành vi hiện tại của monitoring).
- **Bảo mật:** không broadcast kênh chung; mỗi connection subscribe kênh theo
  `run_id` sau khi đã authorize.

---

## 10. REST API — Control Plane (`/api/v1/*`)

### 10.1 Nguyên tắc

- JSON, version `/api/v1`, auth: session (UI) hoặc API token (`Authorization: Bearer`).
- Reuse service layer (`app/modules/*/service.py`), không viết logic mới trong route.
- CSRF áp dụng cho session, không áp dụng cho bearer token (giống ngoại lệ webhook).

### 10.2 Endpoint (theo `docs/api-design.md`, mở rộng)

| Method | Path | Chức năng |
|---|---|---|
| `GET` | `/api/v1/servers` | Danh sách server |
| `POST` | `/api/v1/servers` | Thêm server (`add_server`) |
| `POST` | `/api/v1/servers/{id}/test-ssh` | Kiểm tra SSH |
| `POST` | `/api/v1/servers/{id}/test-ansible` | Ansible ping |
| `GET` | `/api/v1/clusters` | Danh sách cluster |
| `POST` | `/api/v1/clusters` | Tạo cluster (`create_cluster`) |
| `POST` | `/api/v1/clusters/{id}/install` | Cài K3s (`install_kubernetes`) |
| `GET` | `/api/v1/clusters/{id}/nodes` | Danh sách node |
| `GET` | `/api/v1/applications` | Danh sách application (theo scope) |
| `POST` | `/api/v1/applications` | Tạo application (`create_application`) |
| `GET` | `/api/v1/applications/{id}` | Chi tiết |
| `POST` | `/api/v1/applications/{id}/pipeline` | Trigger pipeline |
| `GET` | `/api/v1/applications/{id}/pipeline` | Lịch sử pipeline |
| `GET` | `/api/v1/pipeline/{run_id}` | Trạng thái 1 run |
| `GET` | `/api/v1/pipeline/{run_id}/events` | SSE realtime (mục 9) |
| `POST` | `/api/v1/applications/{id}/scale` | Scale |
| `POST` | `/api/v1/applications/{id}/restart` | Restart |
| `GET` | `/api/v1/applications/{id}/logs` | Logs |
| `POST` | `/api/v1/applications/{id}/deployments/{d}/rollback` | Rollback |
| `GET` | `/api/v1/monitoring/metrics` | Metrics JSON (tái dùng `_get_monitoring_data`) |
| `POST` | `/api/v1/webhooks/github` | Webhook (giữ HMAC) |

### 10.3 Response chuẩn

```json
{ "data": ..., "error": null }
{ "data": null, "error": {"code": "APPLICATION_NOT_FOUND", "message": "..."} }
```

Mã lỗi HTTP: 200/201 thành công, 400 validation, 401 auth, 403 quyền,
404 không tồn tại, 409 xung đột, 502 phụ thuộc ngoài (Prometheus/Grafana).

---

## 11. Redis cache — metrics & replica

- Thay `_cache` (dict) + `_chart_history` (deque) trong `collector.py` bằng Redis:
  - `cache:monitoring:snapshot` (string JSON) — snapshot mới nhất.
  - `cache:monitoring:chart:{metric}` (list/ZSET) — ring buffer 60 mẫu.
- Background collector vẫn chạy (giữ logic thu thập), chỉ đổi nơi ghi từ memory
  sang Redis → nhiều web replica đọc chung một snapshot.
- `/monitoring/api/metrics` đọc từ Redis thay vì `_get_monitoring_data()` gọi lại
  Prometheus/kubectl mỗi request.

---

## 12. Chiến lược di trú

### 12.1 Cấu hình

```env
DATABASE_URL=postgresql+psycopg://platform:...@postgres:5432/platform
REDIS_URL=redis://redis:6379/0
DELIVERY_DATABASE_PATH=              # bỏ/để trống khi dùng PostgreSQL
PIPELINE_MAX_CONCURRENT=4            # giờ map sang Celery concurrency
```

### 12.2 Các bước big-bang

1. Chạy `backup` (scripts hiện có) trước khi di trú.
2. Tạo schema PostgreSQL bằng Alembic (hoặc `initialize_schema` cho Core).
3. Import dữ liệu SQLite → PostgreSQL bằng `migrate_json_state` (tái dùng, đổi
   target connection) hoặc công cụ ETL một lần.
4. Bật feature flag `DATABASE_URL`/`REDIS_URL`; chạy smoke (healthz/readyz, tạo
   app, trigger pipeline).
5. Nếu lỗi: tắt flag, quay về SQLite (data đã backup).

### 12.3 Kế hoạch kiểm thử

- **Unit (giữ 112 test):** chạy trên SQLite in-memory qua `path=` (không đổi).
- **Integration mới:**
  - `fakeredis` / `redis-mock` cho pub/sub + cache + broker.
  - Testcontainers PostgreSQL (hoặc SQLite fallback) cho `claim_next_pipeline_run`
    chạy nhiều thread, `create_deployment_record` version monotonic.
  - Test SSE: subscribe, nhận event stage, authorize 403 khi không đúng scope.
- **Manual:** deploy thật → mở `pipeline.html` thấy 6 stage chuyển realtime.

---

## 13. Rủi ro & giảm thiểu

| Rủi ro | Mức | Giảm thiểu |
|---|---|---|
| Port SQLite→PostgreSQL vỡ 112 test | Cao | Giữ chữ ký `delivery_store`, SQLAlchemy Core, test chạy SQLite qua `path=` |
| Claim đồng thời sai (trùng worker nhận 1 task) | Cao | `FOR UPDATE SKIP LOCKED` + unique partial index; test đa luồng |
| Version deployment trùng khi 2 worker deploy cùng app | Trung | Lock dòng application; test race |
| SSE bị proxy/gunicorn đệm | Trung | Header `X-Accel-Buffering: no`; gunicorn async worker (gevent/thread) |
| Secret lọt vào stream/log | Cao | `mask_secrets` trước khi publish; test log mask (đã có `test_phase3`) |
| Celery tăng độ phức tạp vận hành | Trung | Giữ `--once` debug path; document runbook |

---

## 14. Deliverable & tiêu chí nghiệm thu

1. `DATABASE_URL` → app + worker chạy trên PostgreSQL; `readyz` trả `database: ok`.
2. 112 test hiện tại xanh không sửa (test vẫn dùng `path=` SQLite).
3. Nhiều Celery worker claim đúng 1 task, không trùng; `PIPELINE_MAX_CONCURRENT` > 1.
4. Bấm Deploy → UI hiển thị 6 stage realtime qua SSE, log stream mask secret.
5. `/api/v1/*` trả JSON đúng chuẩn, auth + scope application.
6. Monitoring metrics đọc từ Redis cache, chia sẻ giữa nhiều replica.

---

## 15. Câu hỏi mở cần chốt trước khi code

1. Celery hay RQ? (đề xuất Celery)
2. SSE hay WebSocket? (đề xuất SSE cho Phase A)
3. Gộp `users` + delivery store chung DB hay tách schema? (đề xuất chung DB, 2 schema)
4. Import dữ liệu cũ: tái dùng `migrate_json_state` hay viết ETL một lần riêng?
