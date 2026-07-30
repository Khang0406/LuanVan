# So sánh CICT Platform và Vercel

| Tiêu chí | CICT Platform | Vercel |
|---|---|---|
| Mô hình | Kubernetes/K3s tự quản | Managed frontend/serverless delivery |
| Workload | Container, nhiều service, database, StatefulSet/PVC | Frontend, functions và dịch vụ managed tích hợp |
| Hạ tầng | Server/cluster, namespace, quota, RBAC hạ tầng riêng | Hạ tầng được nhà cung cấp che giấu và tự động hóa |
| Delivery | Pipeline SOURCE–VERIFY, registry và kubectl | Git integration và preview/production workflow tự động cao |
| Scaling | Replica/HPA, resource request/limit, quota | Scaling managed theo sản phẩm |
| Observability | Prometheus, Grafana, logs, alert, HPA event | Observability managed, tích hợp theo plan |
| Dữ liệu | PVC/database do người vận hành quản lý | Thường dùng managed integrations/external data services |
| Trách nhiệm vận hành | Cao: cluster, backup, patching, capacity, security | Thấp hơn; đổi lại phụ thuộc platform/giới hạn dịch vụ |

CICT Platform không nhằm sao chép Vercel. Đề tài tập trung chứng minh một nền
tảng Kubernetes tự quản có thể triển khai container đa service, database bền
vững, quota, HPA, monitoring và hạ tầng riêng. Đây phù hợp lab/doanh nghiệp cần
kiểm soát cluster, mạng và dữ liệu.

Vercel mạnh hơn ở developer experience: kết nối Git, preview deployment, CDN,
serverless runtime, certificate, scaling và vận hành được tự động hóa sâu. Hướng
phát triển của đề tài có thể học trải nghiệm đó—preview environment, policy
template, build cache, rollback một chạm—nhưng vẫn giữ backend Kubernetes và
khả năng chạy workload tổng quát.
