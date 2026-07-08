# Hướng dẫn nâng cấp Monitoring → Prometheus + Grafana

## Tổng quan hiện trạng

**Monitoring hiện tại (`app/modules/monitoring/`)**:
| File | Vai trò |
|------|---------|
| `__init__.py` | Module docstring |
| `collector.py` | Gọi `kubectl top nodes` + `kubectl get pods -o json` để lấy metrics thô (CPU, RAM, pod status). Lưu snapshot vào JSON file |
| `alerting.py` | Kiểm tra ngưỡng (CPU > 80%, RAM > 90%, pod down/restart) → sinh alerts, lưu vào `alerts.json` |

**Hạn chế của cách hiện tại**:
- Phụ thuộc `kubectl top` (cần metrics-server chạy, chỉ có real-time, không có historical)
- Không có đồ thị timeline (chỉ là snapshot tại thời điểm gọi)
- Không có dashboard trực quan (chỉ là template HTML render bảng số)
- Không scrape được application-level metrics (request latency, error rate, throughput)
- Cảnh báo đơn giản, không có rule engine (không có alert grouping, inhibition, routing)

**Mục tiêu nâng cấp**: Triển khai Prometheus + Grafana stack để có:
- Metrics time-series database với retention 15 ngày
- Đồ thị dashboard đẹp (Grafana iframe nhúng thẳng vào platform)
- Application metrics scraping tự động (ServiceMonitor)
- Alertmanager rules mạnh hơn (grouping, inhibition, webhook)

---

## Kiến trúc mới

```
┌──────────────────────────────────────────────────────────────────┐
│                        PLATFORM (Flask)                         │
│                                                                 │
│  ┌──────────────────┐   ┌──────────────────┐                   │
│  │ prometheus.py    │   │ grafana.py       │                   │
│  │ (PromQL queries) │   │ (Dashboard API)  │                   │
│  └────────┬─────────┘   └────────┬─────────┘                   │
│           │                      │                              │
│           │ HTTP :9090           │ HTTP :3000                   │
└───────────┼──────────────────────┼──────────────────────────────┘
            │                      │
    ┌───────▼──────────────────────▼──────────┐
    │          K3S CLUSTER                     │
    │                                          │
    │  namespace: monitoring                   │
    │  ┌────────────┐  ┌────────────┐          │
    │  │ Prometheus │  │  Grafana   │          │
    │  │ Deployment │  │ Deployment │          │
    │  │ :9090      │  │ :3000      │          │
    │  └───▲────────┘  └────────────┘          │
    │      │ scrape                             │
    │  ┌───┴─────────┐  ┌──────────────────┐   │
    │  │Node Exporter│  │ Kube-state-      │   │
    │  │(DaemonSet)  │  │ metrics          │   │
    │  │:9100        │  │ :8080            │   │
    │  └─────────────┘  └──────────────────┘   │
    │                                          │
    │  namespace: map (app namespace)          │
    │  ┌──────────────────────────────────┐    │
    │  │ App Pod  ── ServiceMonitor       │    │
    │  │ (annotated: scrape=true, :80)    │    │
    │  └──────────────────────────────────┘    │
    └──────────────────────────────────────────┘
```

---

## Các file mới cần implement

### 1. `app/modules/monitoring/prometheus.py` — Prometheus HTTP API Client

**Đã tạo sẵn file trống.** Cần implement:

```python
class PrometheusClient:
    def __init__(self, prometheus_url: str = "http://prometheus.monitoring.svc.cluster.local:9090", timeout: int = 10):
        """Khởi tạo client với base URL của Prometheus server."""

    def instant_query(self, query: str) -> list[dict]:
        """GET /api/v1/query?query=<promql>
        Trả về list các metric hiện tại.
        VD: 'avg(rate(node_cpu_seconds_total[5m])) * 100' → CPU % của từng node
        """

    def range_query(self, query: str, start: str, end: str, step: str = "1m") -> list[dict]:
        """GET /api/v1/query_range
        Trả về time-series data cho đồ thị.
        """

    def health_check(self) -> bool:
        """GET /-/healthy → True nếu Prometheus đang chạy"""
```

**Các PromQL queries hữu ích** (port từ `collector.py`):

| Metric hiện tại (`collector.py`) | PromQL tương đương |
|------|------|
| `node_metrics()` — CPU % mỗi node | `avg(rate(node_cpu_seconds_total{mode!="idle"}[5m])) by (instance) * 100` |
| `node_metrics()` — RAM % mỗi node | `(1 - avg(node_memory_MemAvailable_bytes) by (instance) / avg(node_memory_MemTotal_bytes) by (instance)) * 100` |
| `node_metrics()` — Disk usage | `(1 - sum(node_filesystem_avail_bytes{mountpoint="/"}) by (instance) / sum(node_filesystem_size_bytes{mountpoint="/"}) by (instance)) * 100` |
| `application_metrics()` — Pod Ready count | `count(kube_pod_status_ready{condition="true", namespace="map"}) by (namespace)` |
| `application_metrics()` — Pod Restarts | `sum(kube_pod_container_status_restarts_total{namespace="map"}) by (pod)` |
| Network I/O | `rate(node_network_receive_bytes_total[5m]) * 8` (bps) |
| App request latency (nếu app expose `/metrics`) | `histogram_quantile(0.95, rate(http_request_duration_seconds_bucket[5m]))` |

---

### 2. `app/modules/monitoring/grafana.py` — Grafana API Client

**Đã tạo sẵn file trống.** Cần implement:

```python
class GrafanaClient:
    def __init__(self, grafana_url: str = "http://grafana.monitoring.svc.cluster.local:3000", api_key: str = ""):
        """Khởi tạo client. API key lấy từ Grafana Admin → API Keys."""

    def ensure_datasource(self, name: str, url: str, type: str = "prometheus") -> bool:
        """POST /api/datasources — tạo Prometheus datasource nếu chưa có."""

    def import_dashboard(self, name: str, dashboard_json: dict) -> bool:
        """POST /api/dashboards/db — import dashboard từ JSON model."""

    def proxy_embed_url(self, dashboard_uid: str) -> str:
        """Tạo URL nhúng iframe: grafana/d/uid?kiosk=tv&theme=light"""
```

**Flow trong template `monitoring.html`**:
- Thay vì render bảng số từ `collector.py`, nhúng iframe Grafana dashboard
- Dashboard suggested: "Kubernetes Cluster Overview" (ID 315), "Node Exporter Full" (ID 1860)
- Có thể tự build dashboard JSON bằng Grafana Dashboard API

---

### 3. `app/modules/monitoring/k8s_manifests.py` — K8s Manifests Generator

**Đã tạo sẵn file trống.** Cần implement các hàm:

```python
# Tạo prometheus.yml ConfigMap từ template Python (hoặc Jinja2)
def prometheus_config_map(scrape_interval: int = 15) -> str

# Apply Deployment + ConfigMap + PVC + Service cho Prometheus (NodePort:30900)
def deploy_prometheus(storage_size: str = "10Gi", retention: str = "15d")

# Apply Deployment + ConfigMap + PVC + Service cho Grafana (NodePort:30300)
def deploy_grafana(storage_size: str = "5Gi")

# Tạo ServiceMonitor CRD cho một application cụ thể
def create_service_monitor(app_id: str, namespace: str, service_name: str)

# Deploy toàn bộ monitoring stack (namespace + prometheus + grafana + node-exporter + kube-state-metrics)
def deploy_monitoring_stack()
```

**Lưu ý**: Thay vì implement từng resource trong Python, có thể dùng Ansible playbook `install_monitoring_stack.yml` (đã có sẵn) để deploy toàn bộ stack. Module `k8s_manifests.py` dùng để:
- Deploy từ bên trong Platform (qua giao diện admin) không cần chạy Ansible
- Tự động tạo ServiceMonitor khi deploy application mới

---

### 4. `k8s/templates/servicemonitor.yaml.j2` — ServiceMonitor Template

**Đã tạo sẵn.** Template Jinja2 để gen ServiceMonitor CRD cho mỗi application. Sử dụng trong pipeline khi deploy app mới:

```python
from jinja2 import Template
from app.modules.deployments.manifest import GENERATED_DIR

template = Template((GENERATED_DIR.parent / "templates" / "servicemonitor.yaml.j2").read_text())
yaml = template.render(namespace="map", app_id="map", service_name="web")
# kubectl apply -f yaml
```

---

### 5. `ansible/playbooks/install_monitoring_stack.yml` — Ansible Playbook

**Đã tạo sẵn — đã implement đầy đủ!** File này đã có toàn bộ code để deploy Prometheus + Grafana + Node Exporter + Kube-state-metrics. Chỉ cần chạy:

```bash
ansible-playbook -i ansible/inventories/generated/cluster.ini \
  ansible/playbooks/install_monitoring_stack.yml
```

---

## Các task cần làm (theo thứ tự)

### Phase 1: Deploy Prometheus + Grafana lên cluster (1-2 ngày)

1. **Chạy Ansible playbook**:
   ```bash
   ansible-playbook -i ansible/inventories/generated/cluster.ini ansible/playbooks/install_monitoring_stack.yml
   ```
   → Verify: `kubectl get pods -n monitoring` thấy prometheus, grafana, node-exporter, kube-state-metrics đều Ready

2. **Kiểm tra truy cập**:
   - Prometheus: `http://<master-ip>:30900`
   - Grafana: `http://<master-ip>:30300` (admin/admin)
   - Vào Grafana → Explore → chọn Prometheus datasource → thử query `up` → phải thấy tất cả targets up

### Phase 2: Implement Prometheus client (1-2 ngày)

3. **Code `prometheus.py`**:
   - Implement `PrometheusClient` class với `requests.get(f"{url}/api/v1/query", params={"query": promql})`
   - Port toàn bộ `collector.py` từ `kubectl top` → PromQL queries
   - Thêm cache layer (`functools.lru_cache` với TTL ~15s)
   - Fallback về `collector.py` cũ nếu Prometheus chưa sẵn sàng

4. **Update `alerting.py`**:
   - Giữ nguyên threshold logic
   - Chỉ thay `node_metrics()` → gọi `prometheus.instant_query(...)` thay vì `kubectl top`
   - Tương tự cho `application_metrics()`

### Phase 3: Implement Grafana integration (2-3 ngày)

5. **Code `grafana.py`**:
   - Implement `GrafanaClient` class
   - Tạo API key tự động qua Grafana API
   - Import các dashboard có sẵn (ID 315, 1860 từ grafana.com)
   - Tạo dashboard custom cho application metrics

6. **Update `monitoring.html` template**:
   - Thêm iframe nhúng Grafana dashboard (dùng `kiosk=tv` mode)
   - Giữ lại alert list phía dưới (từ `alerting.py`)
   - Thêm link mở Grafana full page trong tab mới

7. **Update `ui/routes.py`**:
   - Route `/monitoring` → render template với iframe Grafana + alert list
   - Route `/monitoring/grafana-proxy` → proxy requests tới Grafana API (tránh CORS)
   - Route `/monitoring/prometheus-proxy` → proxy queries tới Prometheus

### Phase 4: Auto-ServiceMonitor cho application (1 ngày)

8. **Update `deployments/manifest.py`** hoặc `kubectl.py`**:
   - Khi deploy application, tự động generate ServiceMonitor từ template `servicemonitor.yaml.j2`
   - Apply vào namespace monitoring

9. **Update `pipeline/engine.py`**:
   - Trong `pipeline_apply()` stage, sau khi deploy, gọi `create_service_monitor(app_id, namespace)`

### Phase 5: Dashboard custom + Alertmanager rules (2-3 ngày)

10. **Tạo dashboard JSON riêng cho platform**:
    - "Platform Overview": tổng quan cluster health, apps count, pod health
    - "Application Detail": drill-down metrics cho từng app (CPU, RAM, requests, errors)
    - Lưu dưới dạng file JSON trong `app/data/grafana-dashboards/`

11. **Cấu hình Alertmanager rules** (nâng cấp từ `alerting.py`):
    - Rule: `NodeCPUUsage > 80%` for 5m → cảnh báo
    - Rule: `PodRestarts > 5` → cảnh báo
    - Rule: `DeploymentReplicasMismatch` → cảnh báo
    - Route alerts → webhook gọi về platform Flask API
    - Thêm Alertmanager Deployment vào playbook nếu cần

---

## Integration với code hiện tại

### Những file cần sửa

| File | Thay đổi |
|------|----------|
| `app/modules/monitoring/collector.py` | Thêm fallback: nếu Prometheus khả dụng → dùng PromQL, nếu không → dùng kubectl như cũ |
| `app/modules/monitoring/alerting.py` | Thêm rule engine dùng Prometheus Alertmanager API (optional, giữ logic cũ cũng được) |
| `app/ui/routes.py` | Route `/monitoring` → dùng `prometheus.Client` + `grafana.Client`, render iframe |
| `app/templates/monitoring.html` | Nhúng Grafana iframe, giữ alert list |
| `app/modules/pipeline/engine.py` | Gọi `create_service_monitor()` sau khi deploy app |

### Những file KHÔNG cần sửa (giữ nguyên)

- `app/models.py` — không cần thêm model mới
- `app/config.py` — thêm 2 dòng config `PROMETHEUS_URL` + `GRAFANA_URL` (optional, dùng default)
- `app/db.py` — giữ nguyên

---

## Biến môi trường cần thêm (`.env`)

```bash
# Monitoring stack URLs (optional — platform auto-discovers via K8s service)
PROMETHEUS_URL=http://prometheus.monitoring.svc.cluster.local:9090
GRAFANA_URL=http://grafana.monitoring.svc.cluster.local:3000
GRAFANA_API_KEY=          # tạo từ Grafana Admin → API Keys, set sau khi deploy
```

Thêm vào `app/config.py`:
```python
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://prometheus.monitoring.svc.cluster.local:9090")
GRAFANA_URL = os.environ.get("GRAFANA_URL", "http://grafana.monitoring.svc.cluster.local:3000")
GRAFANA_API_KEY = os.environ.get("GRAFANA_API_KEY", "")
```

---

## Verify hoàn thành

Sau khi implement xong, kiểm tra:

1. **`/monitoring` page** hiển thị Grafana iframe dashboard (không còn bảng số thô)
2. **Alerts list** vẫn hoạt động (từ `alerting.py`)
3. **Prometheus targets** (`http://<ip>:30900/targets`) thấy:
   - prometheus (localhost:9090) — UP
   - kubernetes-pods (discovered) — UP
   - kubernetes-nodes — UP
4. **Grafana dashboards** hiển thị đúng metrics cho cluster và từng application
5. **ServiceMonitor** tự động được tạo khi deploy application mới
6. **Application metrics** (nếu app có `/metrics` endpoint) được scrape thành công

---

## Tài liệu tham khảo

- [Prometheus HTTP API](https://prometheus.io/docs/prometheus/latest/querying/api/)
- [Grafana HTTP API](https://grafana.com/docs/grafana/latest/developers/http_api/)
- [PromQL cheatsheet](https://promlabs.com/promql-cheat-sheet/)
- [Grafana Dashboard JSON model](https://grafana.com/docs/grafana/latest/dashboards/build-dashboards/view-dashboard-json-model/)
- [kube-prometheus-stack](https://github.com/prometheus-community/helm-charts/tree/main/charts/kube-prometheus-stack)