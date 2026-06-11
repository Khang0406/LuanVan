# Kiến trúc CICT Platform

```mermaid
flowchart LR
    Dev[Developer] --> GitHub[GitHub Repository]
    GitHub --> Actions[GitHub Actions]
    Actions --> Registry[Docker Registry]

    Admin[Admin/DevOps] --> Web[Management Web UI]
    Dev --> Web

    Web --> Backend[Backend modules bổ sung sau]
    Backend --> DB[(Database)]
    Backend --> Ansible[Ansible Runner]
    Backend --> Kubectl[kubectl / Kubernetes Client]

    Ansible --> Master[Master Server]
    Ansible --> Worker[Worker Nodes]
    Kubectl --> Cluster[K3s/Kubernetes Cluster]
    Registry --> Cluster
    Cluster --> Apps[Web/Microservices]
```

Bản hiện tại chỉ code phần Web UI để trình bày trải nghiệm người dùng. Các khối Backend, Database, Ansible Runner và Kubernetes Client đã được định vị trong cấu trúc source nhưng chưa triển khai logic.
