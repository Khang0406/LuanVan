# Cập nhật Sơ đồ Kiến trúc và Luồng xử lý theo Thực tế Triển khai (CICT Platform)

Qua quá trình phân tích tài liệu thiết kế (trong thư mục `LVdocs/` và `docs/`) và đối chiếu với mã nguồn thực tế của dự án, có thể thấy dự án đã phát triển và hoàn thiện hơn rất nhiều so với bản thiết kế ban đầu. 

Đặc biệt, hệ thống đã triển khai đầy đủ Backend (bằng Python Flask), tự xây dựng CI/CD Pipeline nội bộ (không cần phụ thuộc vào GitHub Actions như bản nháp), và các module quản lý Cluster, Deployment đều đã được lập trình thực thi bằng code thông qua Ansible và Kubectl.

Dưới đây là các sơ đồ (Mermaid) được vẽ lại để phản ánh chính xác cấu trúc và logic code hiện hành của dự án.

## 1. Kiến trúc Hệ thống Tổng thể (System Architecture)

- **Ban đầu (Theo docs):** Thiết kế mô tả Backend sẽ bổ sung sau, dùng GitHub Actions cho CI/CD.
- **Thực tế:** Backend đã được phát triển hoàn chỉnh. Dùng Local JSON storage (thay cho Database SQL truyền thống). CI/CD Pipeline được code trực tiếp trong hệ thống (`app/modules/pipeline/engine.py`) thực hiện Pull code từ Github, Build Docker bằng command, Push lên Registry và gọi Deploy.

```mermaid
flowchart TD
    subgraph Users ["Người dùng"]
        Admin[Admin / DevOps]
        Dev[Developer]
    end

    subgraph CICT_Platform ["CICT Platform (Flask)"]
        UI[Web UI / Jinja2 Templates]
        
        subgraph Backend_Modules ["Backend Services (Python)"]
            Auth[Auth / Users]
            ServerMgr[Server Inventory]
            ClusterMgr[Cluster Management]
            AppMgr[Application Management]
            Pipeline[CI/CD Pipeline Engine]
            DeployMgr[Deployment Service]
            MonitorMgr[Monitoring & Alerts]
            Audit[Audit Log]
        end
        
        DB[(Local JSON Data Store)]
    end

    subgraph External_Systems ["Hệ thống bên ngoài"]
        GitHub[GitHub Repository]
        Registry[Docker Registry]
    end

    subgraph Infrastructure ["Hạ tầng & Kubernetes"]
        AnsibleRunner[Ansible Runner]
        KubeClient[Kubectl Client]
        
        subgraph K3s_Cluster ["K3s/Kubernetes Cluster"]
            Master[Master Nodes]
            Worker[Worker Nodes]
            Prometheus[Prometheus & Grafana]
            Apps[Web / Microservices]
        end
    end

    Admin --> UI
    Dev --> UI

    UI --> Backend_Modules
    Backend_Modules --> DB

    ClusterMgr --> AnsibleRunner
    AnsibleRunner -- "SSH (Cài đặt K3s)" --> Master
    AnsibleRunner -- "SSH (Join node)" --> Worker

    DeployMgr --> KubeClient
    MonitorMgr --> KubeClient
    KubeClient -- "API / kubeconfig" --> Master

    Pipeline -- "Clone / Pull" --> GitHub
    Pipeline -- "Build & Push" --> Registry
    Registry -- "Pull Image" --> Apps

    KubeClient --> Apps
    KubeClient --> Prometheus
```

## 2. Thiết kế Module (Module Architecture)

Sơ đồ Class/Module này phản ánh cấu trúc thư mục code thực tế của nền tảng (theo mô hình MVC mở rộng).

```mermaid
classDiagram
    class PresentationLayer {
        +UI Templates (app/templates)
        +Routes (app/*/routes.py)
    }
    
    class ServiceLayer {
        +servers/service.py
        +clusters/service.py
        +applications/service.py
        +pipeline/engine.py
        +deployments/manifest.py
        +monitoring/prometheus.py
    }
    
    class DataLayer {
        +JSON Files (app/data/*.json)
        +load_()
        +save_()
    }
    
    class InfrastructureLayer {
        +ansible/runner.py (Subprocess Ansible)
        +deployments/kubectl.py (Subprocess Kubectl)
        +pipeline/build.py (Subprocess Docker)
    }

    PresentationLayer --> ServiceLayer : Gọi API / Thực thi logic
    ServiceLayer --> DataLayer : Đọc/Ghi trạng thái
    ServiceLayer --> InfrastructureLayer : Trigger Commands Hệ thống
```

## 3. Luồng thực thi CI/CD Pipeline (Sequence Diagram)

Hệ thống có Pipeline Engine (`engine.py`) chạy ngầm bằng `threading.Thread` đi qua 6 bước (Stages): SOURCE, BUILD, TEST, PUSH, DEPLOY, VERIFY.

```mermaid
sequenceDiagram
    actor Dev as Developer
    participant UI as Web UI
    participant Pipe as Pipeline Engine (Thread)
    participant Git as Git Repo
    participant Docker as Docker Engine
    participant Reg as Docker Registry
    participant K8S as Kubectl / K3s

    Dev->>UI: Bấm "Run Pipeline"
    UI->>Pipe: _run_pipeline_thread(app, branch)
    UI-->>Dev: Trả về trạng thái "Đang chạy"
    
    Note over Pipe: Stage 1: SOURCE
    Pipe->>Git: Clone repo & checkout branch
    Git-->>Pipe: Source code
    
    Note over Pipe: Stage 2: BUILD
    Pipe->>Docker: Build Docker Image (Dockerfile)
    Docker-->>Pipe: Build success & Image tag
    
    Note over Pipe: Stage 3: TEST
    Pipe->>Docker: docker run (Smoke test)
    Docker-->>Pipe: Container running
    
    Note over Pipe: Stage 4: PUSH
    Pipe->>Reg: docker push image:tag
    Reg-->>Pipe: Push success
    
    Note over Pipe: Stage 5: DEPLOY
    Pipe->>K8S: Tạo & Apply Manifests (Namespace, Deploy, Svc)
    K8S-->>Pipe: Apply success
    
    Note over Pipe: Stage 6: VERIFY
    Pipe->>K8S: Kiểm tra Rollout status & Pods
    K8S-->>Pipe: Ứng dụng Ready
    
    Pipe->>UI: Lưu kết quả log & Cập nhật Status
```

## 4. Luồng Cài đặt Kubernetes Cluster (Activity Diagram)

Mô phỏng lại luồng chạy thực thi trong file `clusters/service.py` bằng Ansible.

```mermaid
flowchart TD
    A[Admin chọn Master & Worker nodes] --> B[Bấm Install Kubernetes]
    B --> C[Backend: Hàm `create_cluster` tạo Cluster ID]
    C --> D[Backend: Hàm `build_cluster_inventory` sinh file cluster.ini]
    D --> E[Chạy Ansible playbook `install_k3s_cluster.yml`]
    E --> F{Ansible chạy thành công?}
    F -- Không --> G[Báo lỗi UI & Lưu output log]
    F -- Có --> H[Chạy lệnh SSH lấy nội dung `/etc/rancher/k3s/k3s.yaml` từ Master]
    H --> I[Thay thế IP & Lưu thành file `.kube/config` trên Platform host]
    I --> J[Gán Cluster ID cho các Server trong DB JSON]
    J --> K[Hoàn tất & Cập nhật trạng thái cụm thành Active]
```

## 5. Luồng Deploy Ứng dụng thủ công (Activity Diagram)

Mô phỏng quá trình `deploy_application` trong `deployments/kubectl.py` và `manifest.py`.

```mermaid
flowchart TD
    A[Người dùng cấu hình Application] --> B[Khai báo Image, Replicas, Ports, Env variables]
    B --> C[Bấm Deploy]
    C --> D[Backend: Sinh Namespace YAML]
    D --> E[Backend: Sinh Secret/ConfigMap YAML (nếu có Env/Config)]
    E --> F[Backend: Sinh Deployment YAML]
    F --> G[Backend: Sinh Service YAML (NodePort/ClusterIP) & Ingress]
    G --> H[Chạy subprocess `kubectl apply -f` từng manifest]
    H --> I{kubectl apply thành công?}
    I -- Không --> J[Lưu log lỗi vào DB & Đánh dấu Failed]
    I -- Có --> K[Chờ Pods khởi chạy & Rollout]
    K --> L[Ghi nhận Activity/Audit Log "Deployed"]
    L --> M[Cập nhật trạng thái ứng dụng thành Active]
```
