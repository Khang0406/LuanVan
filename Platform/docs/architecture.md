# Kiến trúc CICT Platform

```mermaid
flowchart LR
    Dev[Developer] --> GitHub[GitHub Repository]
    GitHub --> Actions[GitHub Actions]
    Actions --> Registry[Docker Registry]

    Admin[Admin/DevOps] --> Web[Management Web UI]
    Dev --> Web

    Web --> API[Backend API]
    API --> DB[(Database)]
    API --> Ansible[Ansible Runner]
    API --> Kubectl[kubectl / Kubernetes Client]

    Ansible --> M1[Master Server]
    Ansible --> W1[Worker Server 1]
    Ansible --> W2[Worker Server 2]

    Kubectl --> K8S[K3s/Kubernetes Cluster]
    Registry --> K8S
    K8S --> App[Deployed Web/Microservices]
```

Management server chạy Web UI, Backend, database demo, Ansible và kubectl. Server này điều khiển các máy target bằng SSH/Ansible để cài K3s, sau đó dùng Kubernetes API/kubectl để deploy ứng dụng.
