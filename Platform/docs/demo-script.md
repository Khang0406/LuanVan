# Demo script UI thứ 6

## Mục tiêu demo

Cho thầy thấy sản phẩm sẽ hỗ trợ người dùng làm gì, luồng thao tác ra sao và backend/business logic sẽ được nối vào các màn hình nào.

## Kịch bản 1: Admin cài Kubernetes từ website

1. Mở Dashboard để giới thiệu mô hình end-to-end.
2. Vào Server Inventory, trình bày danh sách máy management/master/worker.
3. Mở form thêm server, giải thích thông tin SSH/role.
4. Vào Kubernetes Setup, trình bày wizard chọn node master/worker.
5. Mở Job Logs, giải thích nơi hiển thị log Ansible khi backend được bổ sung.
6. Mở Cluster Detail, giải thích nơi hiển thị `kubectl get nodes`.

## Kịch bản 2: Developer deploy website

1. Vào Applications, tạo application/namespace.
2. Mở form tạo service, khai báo image/repo, port, replicas, env.
3. Vào Deployment Management, trình bày deploy/restart/scale/rollback.
4. Vào Pod Logs, trình bày logs ứng dụng.

## Kịch bản 3: CI/CD

1. Vào CI/CD Flow.
2. Trình bày Developer push code lên GitHub.
3. GitHub Actions build image và push Registry.
4. Platform nhận image/webhook và redeploy lên Kubernetes.

## Ghi chú

Backend thật sẽ bổ sung sau theo module đã tạo sẵn. Bản này tập trung UI để chốt yêu cầu, activity/use case/sequence và luồng demo trước.
