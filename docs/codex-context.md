# Codex Context – Định hướng phát triển project thành luận văn

Tài liệu này tóm tắt context quan trọng đã trao đổi trong task Codex hiện tại để task mới có thể đọc nhanh và tiếp tục công việc. Nội dung không phải transcript từng câu, mà là các quyết định, phân tích và hướng phát triển chính.

## 1. Bối cảnh project

- Repository hiện tại nằm tại `/workspace/LuanVan`.
- Project chính nằm trong thư mục `Project/`.
- Đây là project niên luận của bạn cùng nhóm/bạn học trước đó, tên CICTAdmin.
- Ban đầu project được phân tích theo hướng **web quản trị máy chủ/website từ xa**.
- Sau quá trình trao đổi, thầy hướng dẫn định hướng lại đề tài luận văn theo hướng:
  - **Hệ thống quản trị các ứng dụng web với kiến trúc microservices**.
- Vì vậy, định hướng mới không còn chỉ là “điều khiển máy chủ từ xa”, mà cần chuyển sang **quản trị vòng đời ứng dụng web/microservices**.

## 2. Project gốc hiện tại là gì và đang dùng công nghệ nào

Project gốc CICTAdmin hiện là một ứng dụng web nhỏ để quản trị máy chủ/website, viết chủ yếu bằng Flask.

### Công nghệ hiện tại

- Backend: Python Flask.
- Template/UI: Jinja2 templates trong `Project/templates/`.
- Static assets: `Project/static/`.
- Database runtime chính: SQLite (`cictadmin.db`).
- Remote access: Paramiko SSH/SFTP.
- System metrics: psutil và shell command qua SSH.
- Một file `db.py` có kết nối MySQL, nhưng logic chính trong `app.py` vẫn đang dùng SQLite.
- Tài liệu cài đặt: `Project/README.md`.

### Các chức năng hiện có

- Đăng ký, đăng nhập, đăng xuất.
- Role cơ bản `admin/user`.
- Quản lý user: thêm, sửa, xóa, khóa/mở khóa.
- Quản lý server: thêm/xóa/xem danh sách server.
- Quản lý website/service: website gắn với server và `service_name`.
- Gán user với server và website.
- Remote service control qua SSH/systemctl.
- SSH terminal và admin terminal.
- Monitoring server bằng SSH command/psutil.
- Process/service management.
- Security log, failed login, block user.
- Firewall route ở mức demo/dang dở.
- Remote file manager qua SFTP: list/view/edit/create/delete/rename/upload file.

## 3. Mục tiêu phát triển project niên luận thành luận văn

Mục tiêu ban đầu được đề xuất là nâng từ project “điều khiển server từ xa” thành nền tảng quản trị hạ tầng có RESTful API, automation, RBAC, audit log, monitoring và Docker deployment.

Tuy nhiên, sau khi thầy hướng dẫn định hướng lại, mục tiêu luận văn nên điều chỉnh thành:

> Phát triển hệ thống quản trị các ứng dụng web theo kiến trúc microservices, hỗ trợ quản lý application/service, triển khai phiên bản, restart/scale/rollback, theo dõi trạng thái, xem logs/metrics và ghi nhận lịch sử thao tác.

Ý chính:

- Không phát minh công nghệ nền tảng mới.
- Dùng các công nghệ hiện có để xây sản phẩm có ích, dễ sử dụng, có tính mới ở cách tích hợp và áp dụng.
- Tập trung vào quản trị ứng dụng web/microservices, không chỉ quản trị server.
- Có sản phẩm chạy được, có demo, có sơ đồ, có đánh giá thực nghiệm.

## 4. Công nghệ đã bàn hoặc định sử dụng

### 4.1. Flask project hiện tại

- Dùng làm nguồn tham khảo domain và chức năng cũ.
- Teammate hiểu project Flask hiện tại, giao diện và logic cũ.
- Không nên tiếp tục mở rộng toàn bộ trong `app.py` theo kiểu monolith.
- Có thể giữ giao diện/templates cũ làm tham khảo hoặc tái sử dụng một phần nếu phù hợp.

### 4.2. Spring Boot RESTful API

- Bạn đã học Java RESTful API nên Spring Boot là lựa chọn phù hợp để xây backend mới.
- Vai trò chính:
  - Chuẩn hóa backend API.
  - Tách Controller/Service/Repository/DTO.
  - Quản lý user/role/permission.
  - Quản lý application, microservice, deployment, audit log.
  - Tích hợp với Kubernetes/K3s hoặc gọi `kubectl`/Kubernetes API nếu làm microservices management.
- Công nghệ liên quan:
  - Spring Web.
  - Spring Security.
  - JWT/session authentication.
  - Spring Data JPA.
  - OpenAPI/Swagger.
  - Validation.

### 4.3. Database

- Project cũ dùng SQLite, chỉ phù hợp demo/local.
- Bản luận văn nên dùng PostgreSQL hoặc MySQL.
- Nếu muốn gần với file `db.py` cũ thì MySQL dễ tiếp cận hơn.
- Nếu muốn backend hiện đại, quan hệ dữ liệu rõ và dễ mở rộng thì PostgreSQL là lựa chọn tốt.
- Dữ liệu mới nên chuyển từ server-centric sang application-centric.

Các thực thể mới nên cân nhắc:

- `users`, `roles`, `permissions`, `user_roles`, `role_permissions`.
- `applications`.
- `microservices` hoặc `services`.
- `environments`.
- `deployments`.
- `deployment_histories`.
- `service_instances`.
- `service_endpoints`.
- `container_images` nếu cần.
- `audit_logs`.
- `monitoring_snapshots` hoặc chỉ lưu summary nếu dùng Prometheus.
- `alerts`.

### 4.4. Frontend/templates/static hiện tại

- Project hiện tại dùng Flask templates trong `Project/templates/`.
- Có nhiều giao diện sẵn như dashboard, login, users, servers, websites, monitoring, terminal, file manager.
- Có thể tham khảo layout và flow giao diện cũ.
- Nếu làm backend Spring Boot API riêng, frontend có thể:
  - Giữ template/server-render đơn giản ở giai đoạn đầu.
  - Hoặc xây frontend mới gọi REST API.
  - Hoặc dùng lại một số HTML/CSS cũ để tiết kiệm thời gian.

### 4.5. Linux deployment

- Vì đề tài liên quan quản trị ứng dụng web/microservices, môi trường Linux vẫn quan trọng.
- Cần chuẩn bị ít nhất một máy Ubuntu/Linux hoặc VM để demo.
- Các phần cần triển khai:
  - Backend/API.
  - Database.
  - Docker runtime.
  - K3s/Minikube nếu demo Kubernetes.
  - Prometheus/Grafana nếu làm monitoring.

### 4.6. Nginx/systemd nếu phù hợp

- Trong project cũ, quản trị service thường xoay quanh `nginx`, `apache2`, `systemctl`.
- Với hướng microservices, Nginx/systemd không còn là lõi chính nhưng vẫn có thể dùng:
  - Nginx làm reverse proxy cho dashboard/backend.
  - systemd dùng để chạy backend nếu deploy không dùng container.
  - Nginx cũng có thể là service demo trong phần so sánh server-centric cũ.
- Nếu dùng Kubernetes/K3s, nên ưu tiên Deployment/Service/Ingress hơn systemd.

### 4.7. Docker/Kubernetes/K3s

- Khi đề tài chuyển sang quản trị ứng dụng web microservices, Docker/Kubernetes/K3s trở nên quan trọng hơn trước.
- Docker gần như bắt buộc để đóng gói các microservice demo.
- K3s/Minikube nên dùng để demo môi trường Kubernetes nhẹ:
  - Deploy service.
  - Restart deployment.
  - Scale replicas.
  - Xem pod/deployment/service status.
  - Xem logs pod/service.
- Không nên làm full Kubernetes platform quá rộng.

### 4.8. Prometheus/Grafana

- Monitoring là phần nên làm nếu kịp.
- Với microservices, monitoring nên tập trung vào:
  - Service up/down.
  - HTTP health check.
  - Response time.
  - Pod/container status.
  - CPU/RAM nếu có metrics-server/Prometheus.
  - Dashboard Grafana.
- Có thể làm Prometheus/Grafana ở mức cơ bản.

### 4.9. Ansible

- Ban đầu Ansible được đề xuất làm lõi để thay SSH command thủ công.
- Sau khi thầy đổi hướng sang microservices, Ansible giảm vai trò.
- Có thể dùng Ansible để hỗ trợ setup hạ tầng:
  - Cài Docker/K3s.
  - Cài Prometheus/Grafana.
  - Cấu hình server lab.
- Không nên để Ansible là lõi chính nếu đề tài là quản trị ứng dụng microservices.

## 5. Vai trò của từng người

### Teammate

- Hiểu project Flask hiện tại.
- Nắm giao diện/templates/static và logic cũ.
- Có thể phụ trách:
  - Rà lại UI/flow cũ.
  - Tận dụng giao diện cũ nếu phù hợp.
  - Hỗ trợ vẽ use case/activity/sequence dựa trên chức năng cũ.
  - Hỗ trợ frontend/dashboard.

### Tôi

- Phát triển RESTful API bằng Spring Boot.
- Chuẩn hóa backend/database.
- Thiết kế schema mới theo hướng application/microservices.
- Hỗ trợ deploy Linux.
- Hỗ trợ Docker/K3s/Prometheus nếu đưa vào MVP.
- Phụ trách phần kiến trúc backend, API contract, database migration/model.

## 6. Những vấn đề kỹ thuật đã phát hiện trong project gốc

- `app.py` quá lớn, gom hầu hết route, DB init, SSH, monitoring, security, file manager vào một file.
- Kiến trúc monolith, khó test và khó mở rộng.
- Password user đang lưu plaintext.
- SSH credential server đang lưu plaintext.
- `app.secret_key` hard-code trong source.
- Tài khoản admin mặc định `admin/admin`.
- Chạy command qua SSH/subprocess trực tiếp, có rủi ro bảo mật.
- Cơ chế chặn lệnh nguy hiểm bằng blacklist chưa đủ an toàn.
- Monitoring chủ yếu là snapshot bằng SSH command/psutil, chưa có metrics history/alerting chuẩn.
- Database SQLite phù hợp demo nhưng không phù hợp bản luận văn/product-like.
- `db.py` dùng MySQL nhưng app chính dùng SQLite, gây không nhất quán.
- Migration DB thủ công trong `init_db()`, chưa có migration tool.
- Route firewall dùng bảng `firewall_rules` nhưng khi rà code chưa thấy schema tạo bảng tương ứng.
- Một số hàm/biến có dấu hiệu thiếu hoặc lỗi tiềm ẩn như `get_server_by_id`, `user_has_access`, `session["username"]` trong một số đoạn.
- File manager SFTP có nhiều quyền nhạy cảm, cần giới hạn path/quyền/audit nếu giữ.

## 7. Hướng kiến trúc đã thống nhất

### Trước khi thầy đổi đề tài

Hướng ban đầu:

```text
Web UI
  -> RESTful API Backend
      -> Auth/RBAC
      -> Server Inventory
      -> Website/Service Management
      -> Automation Jobs
      -> Monitoring
      -> Audit Log
  -> Database
  -> Ansible Runner
  -> Managed Servers
  -> Prometheus/Grafana
```

### Sau khi thầy định hướng lại microservices

Hướng mới nên là:

```text
Web UI / Admin Dashboard
  -> Management API (Spring Boot)
      -> Auth/RBAC
      -> Application Management
      -> Microservice Registry
      -> Deployment Management
      -> Monitoring/Logs
      -> Audit Log
  -> Database (PostgreSQL/MySQL)
  -> Kubernetes/K3s API hoặc kubectl integration
      -> Deployments
      -> Pods
      -> Services
      -> Replica scaling
      -> Rollout/Rollback
  -> Prometheus/Grafana
  -> Container Registry nếu có
```

Trọng tâm mới:

- Application-centric, không server-centric.
- Quản trị vòng đời ứng dụng web microservices.
- Docker/K3s/Kubernetes quan trọng hơn Ansible.
- Spring Boot RESTful API vẫn phù hợp.

## 8. Các module nên tách hoặc refactor

Nếu tiếp tục tận dụng project Flask cũ để tham khảo/refactor, nên tách các nhóm:

- Auth/User/RBAC.
- Server management.
- Website/service management.
- Assignment/permission.
- Monitoring.
- Remote action/automation.
- Audit/security log.
- File manager.
- Firewall.

Tuy nhiên, với đề tài microservices mới, nên thiết kế module mới theo hướng:

- Auth/RBAC Module.
- Application Management Module.
- Microservice Management Module.
- Deployment Management Module.
- Kubernetes Integration Module.
- Monitoring/Logging Module.
- Audit Log Module.
- Environment/Namespace Management Module.

## 9. Các module nên xây bằng Spring Boot trước

Ưu tiên xây các module backend Spring Boot theo thứ tự:

1. Auth + User + Role/Permission cơ bản.
2. Application CRUD.
3. Microservice CRUD.
4. Environment/namespace model.
5. Deployment model và deployment history.
6. API lấy trạng thái service/deployment.
7. API deploy/restart/scale service trên K3s/Minikube.
8. Audit log.
9. Log viewer cơ bản.
10. Monitoring summary hoặc tích hợp Prometheus.

Không nên bắt đầu bằng Kubernetes phức tạp ngay trước khi có model/API rõ.

## 10. Roadmap phát triển tiếp

### Giai đoạn 1: Chốt đề tài và tài liệu thiết kế

- Chốt lại tên đề tài microservices với thầy.
- Rà tài liệu/hình ảnh đề tài anh khóa trước nếu có.
- Vẽ use case tổng thể.
- Vẽ activity flow cho deploy/restart/scale microservice.
- Vẽ sequence diagram cho deploy service.
- Vẽ architecture diagram.
- Vẽ ERD mới application-centric.
- Chốt MVP 2 người/2.5 tháng.

### Giai đoạn 2: Backend core

- Tạo Spring Boot project.
- Cấu hình database PostgreSQL/MySQL.
- Xây Auth/RBAC cơ bản.
- Xây CRUD application/microservice/environment.
- Viết Swagger/OpenAPI.

### Giai đoạn 3: Kubernetes/K3s integration

- Chuẩn bị K3s/Minikube local/lab.
- Dockerize microservice demo.
- Tạo manifest mẫu.
- API deploy/restart/scale.
- API xem status deployment/pod/service.
- API xem logs.

### Giai đoạn 4: Monitoring/logging/audit

- Audit log mọi thao tác quan trọng.
- Health check service.
- Monitoring cơ bản.
- Tích hợp Prometheus/Grafana nếu kịp.
- Alert cơ bản nếu kịp.

### Giai đoạn 5: Demo và đánh giá

- Chuẩn bị demo app microservices mẫu.
- Demo deploy version mới.
- Demo scale service.
- Demo restart/rollback nếu có.
- Demo xem logs/metrics.
- Đo API response time, deploy duration, rollback/restart duration.
- Kiểm thử quyền và audit log.

## 11. Những quyết định quan trọng đã thống nhất trong task này

- Đề tài luận văn không cần tạo công nghệ mới; mục tiêu là tích hợp công nghệ có sẵn thành sản phẩm có ích, dễ dùng, có tính mới trong cách áp dụng.
- Không nên chỉ tiếp tục “web điều khiển server từ xa” vì dễ bị xem là thô sơ.
- Project Flask cũ có giá trị tham khảo, nhưng không nên tiếp tục mở rộng monolith `app.py` quá nhiều.
- Spring Boot RESTful API là hướng phù hợp với vai trò của bạn.
- Database nên chuyển sang PostgreSQL/MySQL thay vì SQLite.
- Docker là cần thiết.
- Với hướng microservices mới, Kubernetes/K3s nên được ưu tiên hơn so với hướng server-management cũ.
- Prometheus/Grafana nên làm ở mức cơ bản nếu đủ thời gian.
- Ansible không còn là lõi chính sau khi đề tài chuyển sang microservices; chỉ còn vai trò hỗ trợ setup hạ tầng nếu cần.
- Cần vẽ sơ đồ trước khi code: use case, activity, sequence, architecture, ERD.
- ERD mới nên kế thừa ý tưởng từ project cũ nhưng chuyển sang application-centric.

## 12. Những việc chưa làm

- Chưa cập nhật lại `docs/note_de_tai_goi_y.md` theo đề tài microservices mới.
- Chưa tạo bộ sơ đồ chính thức trong repo.
- Chưa có ERD microservices chính thức.
- Chưa tạo Spring Boot backend.
- Chưa chốt database PostgreSQL hay MySQL.
- Chưa chốt frontend sẽ dùng lại templates cũ hay làm UI mới.
- Chưa tạo K3s/Minikube lab.
- Chưa có microservice demo.
- Chưa tích hợp Prometheus/Grafana.
- Chưa có CI/CD.
- Chưa phân tích được thư mục `LVdoc` vì tại thời điểm kiểm tra workspace chưa thấy thư mục này.

## 13. Bước tiếp theo task mới cần làm

1. Kiểm tra lại repo xem thư mục `LVdoc` đã xuất hiện chưa.
2. Nếu có `LVdoc`, phân tích hình ảnh/tài liệu của đề tài anh khóa trước:
   - chức năng
   - kiến trúc microservices
   - công nghệ
   - điểm mạnh
   - điểm yếu
   - cơ hội nâng cấp
3. Cập nhật lại note/proposal theo đề tài mới:
   - từ “quản trị hạ tầng máy chủ” sang “quản trị ứng dụng web microservices”.
4. Vẽ bộ sơ đồ mới:
   - Use Case tổng thể.
   - Activity deploy/restart/scale service.
   - Sequence deploy microservice.
   - Architecture diagram.
   - ERD application-centric.
   - Deployment diagram Docker/K3s.
5. Chốt MVP cho 2 người trong khoảng 2.5 tháng.
6. Tạo Spring Boot project hoặc ít nhất scaffold backend API.
7. Thiết kế database schema ban đầu.
8. Tạo demo microservices nhỏ để deploy trên Docker/K3s.
9. Chuẩn bị nội dung trình bày với thầy về lý do chọn kiến trúc/công nghệ.

