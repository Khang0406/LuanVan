# Demo script thứ 6

## Kịch bản 1: Cài Kubernetes từ website

1. Mở CICT Platform và đăng nhập `admin/admin`.
2. Vào Servers, thêm master và worker.
3. Vào Clusters, tạo `lab-cluster`, chọn role master/worker.
4. Bấm `Test SSH bằng Ansible Ping`.
5. Bấm `Install K3s Cluster` và giải thích log Ansible.
6. Bấm `kubectl get nodes` để xác nhận cluster ready.

## Kịch bản 2: Deploy website

1. Vào Applications, tạo `demo-web` namespace `demo-web`.
2. Thêm service `demo-nginx`, image `nginx:latest`, port `80`, replicas `2`, NodePort `30080`.
3. Bấm deploy.
4. Mở `http://<node-ip>:30080`.
5. Scale từ 2 lên 4 replicas.
6. Xem logs pod.

## Kịch bản CI/CD mô phỏng

Developer push code lên GitHub, GitHub Actions build image và push registry. Trên platform, Developer đổi image tag và bấm redeploy. Webhook tự động là hướng phát triển tiếp theo.
