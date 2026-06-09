# Note đề tài luận văn gợi ý

## Tên đề tài khuyến nghị

**Phát triển hệ thống quản trị hạ tầng máy chủ tích hợp RESTful API, Ansible Automation và giám sát dịch vụ**

Tên học thuật hơn có thể dùng:

**Nghiên cứu và phát triển nền tảng quản trị hạ tầng máy chủ theo hướng dịch vụ, hỗ trợ tự động hóa vận hành, giám sát và kiểm soát truy cập**

## Lý do chọn đề tài này

Đề tài cũ theo hướng “điều khiển máy chủ từ xa” dễ bị đánh giá là còn thô sơ nếu chỉ dừng ở việc SSH vào server, chạy lệnh và trả kết quả. Hướng đề tài mới mở rộng bài toán thành một nền tảng quản trị hạ tầng tập trung, trong đó điều khiển từ xa chỉ là một phần nhỏ.

Hướng này phù hợp vì:

- Bám sát project CICTAdmin hiện tại: quản lý user, server, website, phân quyền, log, monitoring và SSH remote.
- Tiếp thu đúng gợi ý của thầy về các công nghệ doanh nghiệp như Ansible, Kubernetes, Docker và monitoring.
- Tận dụng được kiến thức Java RESTful API đã học.
- Có tính ứng dụng thực tế, dễ demo và dễ đánh giá bằng số liệu.
- Không yêu cầu tạo ra công nghệ mới, mà tập trung vào tích hợp công nghệ có sẵn thành một sản phẩm dễ dùng, an toàn và có khả năng mở rộng.

## Mục tiêu tổng quát

Xây dựng một hệ thống web quản trị hạ tầng máy chủ theo hướng dịch vụ, tích hợp RESTful API, tự động hóa tác vụ bằng Ansible, giám sát trạng thái server/dịch vụ và kiểm soát quyền thao tác của người dùng nhằm hỗ trợ vận hành hệ thống dễ dàng, an toàn và có khả năng mở rộng.

## Mục tiêu cụ thể

1. Khảo sát các công nghệ liên quan:
   - RESTful API / Spring Boot
   - Ansible Automation
   - Docker / Docker Compose
   - Prometheus / Grafana
   - Kubernetes / K3s
   - RBAC, audit log và security checklist
2. Thiết kế kiến trúc hệ thống theo hướng tách frontend/backend.
3. Xây dựng backend RESTful API quản lý user, server, website/service, phân quyền và audit log.
4. Tích hợp Ansible để thay thế thao tác SSH thủ công bằng các automation workflow có kiểm soát.
5. Xây dựng dashboard giám sát server, service và website.
6. Docker hóa hệ thống để dễ triển khai và demo.
7. Đánh giá hệ thống dựa trên hiệu năng, tính dễ dùng, khả năng phân quyền và khả năng giám sát.

## Công nghệ chính nên chọn

### 1. Backend RESTful API

Khuyến nghị dùng **Java Spring Boot** vì phù hợp với kiến thức đã học về Java RESTful API.

Thành phần nên dùng:

- Spring Web
- Spring Security
- JWT hoặc session authentication
- Spring Data JPA
- PostgreSQL hoặc MySQL
- OpenAPI / Swagger
- Validation
- JUnit cho testing nếu đủ thời gian

### 2. Ansible Automation

Ansible là công nghệ lõi của đề tài. Thay vì cho người dùng nhập command tùy ý, hệ thống cung cấp các tác vụ được định nghĩa sẵn bằng playbook.

Ví dụ playbook:

- `check_server.yml`
- `restart_service.yml`
- `install_nginx.yml`
- `deploy_static_site.yml`
- `backup_website.yml`
- `check_disk.yml`
- `firewall_rule.yml`

### 3. Database

Nên chọn **PostgreSQL** hoặc **MySQL** thay vì SQLite cho bản luận văn chính.

Database cần quản lý:

- Users
- Roles / Permissions
- Servers
- Websites / Services
- User-server assignments
- User-service assignments
- Automation jobs
- Audit logs
- Monitoring history / alerts

### 4. Monitoring

Nên tích hợp monitoring theo một trong hai mức:

- Mức cơ bản: backend tự kiểm tra CPU, RAM, disk, uptime, service status, HTTP status.
- Mức nâng cao: Prometheus + Node Exporter + Grafana.

### 5. Docker

Docker nên dùng để đóng gói:

- Backend API
- Database
- Prometheus / Grafana nếu có

Docker Compose giúp demo nhanh và triển khai dễ hơn.

### 6. Kubernetes / K3s

Kubernetes không nên là trọng tâm chính ngay từ đầu. Nên đưa vào:

- Phần khảo sát công nghệ doanh nghiệp.
- Phần mở rộng hoặc demo nhỏ bằng K3s/Minikube.
- Hướng phát triển tương lai.

## Kiến trúc đề xuất

```text
Web UI
  |
RESTful API Backend
  |
  |-- Auth / RBAC Module
  |-- User Management Module
  |-- Server Inventory Module
  |-- Website / Service Management Module
  |-- Automation Job Module
  |-- Monitoring Module
  |-- Audit Log Module
  |
Database PostgreSQL/MySQL
  |
  |-- Ansible Runner / Playbooks
  |       |
  |       +-- Managed Linux Servers qua SSH
  |
  |-- Prometheus / Grafana
          |
          +-- Metrics / Dashboard / Alert
```

Nếu mở rộng Kubernetes:

```text
RESTful API Backend
  |
  +-- Kubernetes Integration Module
          |
          +-- K3s / Minikube Cluster
              |-- Pods
              |-- Deployments
              |-- Services
              |-- Pod Logs
```

## Module chức năng nên có

### 1. Auth / RBAC

- Đăng nhập
- Role admin/operator/viewer
- Phân quyền theo server
- Phân quyền theo action
- Khóa/mở khóa user
- Đổi mật khẩu
- Hash password

### 2. Server Inventory

- Thêm/sửa/xóa server
- Lưu IP, hostname, SSH port, OS, môi trường dev/staging/production
- Kiểm tra server online/offline
- Gán server cho user/team

### 3. Website / Service Management

- Quản lý website/service chạy trên server
- Kiểm tra HTTP status
- Kiểm tra service status
- Start/stop/restart service bằng Ansible
- Xem log service

### 4. Automation Jobs

- Danh sách action/playbook
- Chạy job
- Theo dõi trạng thái job: pending/running/success/failed
- Xem output job
- Xem lịch sử job
- Phân quyền action theo user/role

### 5. Monitoring / Alerting

- CPU, RAM, disk, uptime
- Service status
- Website status
- Cảnh báo khi server down, service failed, disk cao, website lỗi
- Lưu lịch sử trạng thái để đánh giá

### 6. Audit Log

Mỗi thao tác cần ghi:

- Ai thực hiện
- Thực hiện action gì
- Trên server/website nào
- Thời gian
- Kết quả
- Output rút gọn
- IP người thao tác nếu có

### 7. Security Checklist

Module mở rộng có thể kiểm tra:

- SSH root login
- Password authentication
- Firewall status
- Port đang mở
- Disk usage
- Package cần cập nhật
- Service quan trọng đang chạy hay không

Có thể hiển thị dạng điểm số: `Security Score: 78/100`.

## Phạm vi nên chốt

### Must-have

- RESTful API backend
- User / role / permission
- Server inventory
- Website / service management
- Ansible automation jobs
- Audit log
- Monitoring cơ bản
- Docker deployment

### Should-have

- Prometheus / Grafana
- Alerting
- Security checklist
- Backup website
- Deploy static site
- API documentation bằng Swagger

### Could-have

- Kubernetes / K3s demo
- Terraform / OpenTofu demo
- CI/CD basic
- Vault / secret management
- Argo CD / GitOps

## Điểm mới của đề tài

Điểm mới không nằm ở việc tạo ra công nghệ mới, mà nằm ở việc tích hợp các công nghệ có sẵn thành một nền tảng quản trị hạ tầng dễ dùng, có kiểm soát và có khả năng mở rộng.

So với project điều khiển từ xa đơn giản, đề tài mới cải tiến ở các điểm:

- Chuyển từ chạy lệnh SSH tự do sang automation workflow bằng Ansible.
- Có phân quyền thao tác theo user, role, server và action.
- Có audit log để truy vết ai đã làm gì.
- Có monitoring và cảnh báo trạng thái server/service.
- Có Docker để triển khai dễ hơn.
- Có khả năng mở rộng sang Kubernetes/K3s.

## Kịch bản demo đề xuất

1. Admin đăng nhập hệ thống.
2. Admin thêm một server Ubuntu.
3. Admin thêm website/service `nginx` chạy trên server đó.
4. Admin gán quyền cho user chỉ được xem monitoring và restart service.
5. User đăng nhập và chỉ thấy server/website được cấp quyền.
6. User bấm nút `Restart Nginx`.
7. Backend kiểm tra quyền, tạo automation job và gọi Ansible playbook.
8. Hệ thống hiển thị job status và output.
9. Audit log ghi lại thao tác.
10. Dashboard monitoring hiển thị trạng thái server/service sau khi thao tác.

## Đánh giá thực nghiệm nên làm

- So sánh số bước thao tác SSH thủ công với thao tác qua hệ thống.
- Đo thời gian phản hồi API.
- Đo thời gian chạy Ansible job.
- Kiểm thử user không có quyền có bị chặn hay không.
- Kiểm thử audit log có ghi đầy đủ hay không.
- Kiểm thử phát hiện website/service down.
- Đánh giá mức độ dễ dùng qua khảo sát nhỏ hoặc bảng tiêu chí.

## Câu trả lời ngắn khi trình bày với thầy

Sau khi tiếp thu góp ý của thầy, em không chỉ dừng lại ở đề tài điều khiển máy chủ từ xa bằng SSH. Em sẽ mở rộng thành một nền tảng quản trị hạ tầng máy chủ theo hướng dịch vụ, tích hợp RESTful API, Ansible Automation, phân quyền thao tác, audit log, monitoring và Docker deployment. Kubernetes/K3s sẽ được đưa vào phần khảo sát hoặc demo mở rộng. Trọng tâm của đề tài là chuyển từ thao tác thủ công sang vận hành tự động, có kiểm soát, dễ sử dụng và có khả năng đánh giá bằng số liệu.

## Kết luận

Đề tài khuyến nghị cuối cùng:

**Phát triển hệ thống quản trị hạ tầng máy chủ tích hợp RESTful API, Ansible Automation và giám sát dịch vụ**

Đây là hướng cân bằng nhất giữa tính thực tế, độ mới, khả năng hoàn thành và mức độ phù hợp với góp ý của thầy hướng dẫn.
