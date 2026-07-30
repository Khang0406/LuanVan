# Biên bản nghiệm thu Giai đoạn 3 rút gọn

Ngày nghiệm thu: 2026-07-29<br>
Namespace cô lập: `phase3-acceptance-20260729-141357`<br>
Label cleanup: `purpose=phase3-acceptance`

## Kết quả

- Prometheus thu thập CPU theo từng pod trong namespace test.
- Grafana `cluster-overview` có đủ sáu nhóm panel theo application:
  CPU, RAM, ready/desired replicas, restart count, pod status,
  deployment version/image digest.
- Platform application detail hiển thị metric application/pod, digest,
  version, HPA scale event và liên kết sang logs.
- Logs hỗ trợ chọn service, pod, container, tail và khoảng thời gian
  `15m`–`24h`; output được masking theo cả tên trường nhạy cảm và giá trị
  secret đang lưu.
- Prometheus nạp đúng năm rule, tất cả có trạng thái health `ok`:
  `NodeOffline`, `HighNodeResourceUsage`,
  `PodRestartOrCrashLoopBackOff`, `ApplicationNoReadyReplica`,
  `HPAMaxReplicas`.
- Alert Platform dùng fingerprint ổn định, chỉ phát thông báo khi chuyển
  `Firing` hoặc `Resolved`.
- SMTP acceptance receiver nhận đúng hai message: một `Firing`, một
  `Resolved`; lần thu thập lặp cùng alert không gửi thêm.
- HPA thực tế: min `2`, max `5`, CPU target `50%`.

## Kịch bản xuyên suốt

1. Application bắt đầu với `2/2` pod Ready.
2. Load generator tạo tải; HPA đo `205%/50%`.
3. HPA tăng workload lên `5/5` pod Ready.
4. Prometheus trả CPU millicore riêng cho cả năm pod.
5. Alert HPA-max được tạo và SMTP receiver nhận transition `Firing`.
6. Logs thật được truy vấn theo service `phase3-web`.
7. Dừng load generator.
8. HPA giảm về `2/2`, CPU còn `10%/50%`.
9. Scale event lưu chuỗi `[5, 2]`.
10. SMTP receiver nhận transition `Resolved`; không có message lặp.

Image nghiệm thu được pin bằng digest:
`sha256:97d490c12ba55b4946b01546d1c3ed324e8d41ab1c9fcb2a616aa470620e5b46`.

## Regression và cleanup

- `map-web`: `1/1`; public path HTTP `200`; NodePort `30082`.
- `map-mysql`: `1/1`; PVC production vẫn `Bound`.
- `demo-nginx-web`: `1/1`; HTTP `200`; NodePort `30146`.
- Prometheus, Grafana, kube-state-metrics: `1/1`; monitoring PVC vẫn
  `Bound`.
- Namespace test đã được kiểm tra đúng label trước khi xóa và đã xóa
  thành công. Không xóa PVC production.

## Automated checks

- `92/92` unit tests pass, gồm tám test Giai đoạn 3.
- `python -m compileall -q app tests`: pass.
- `git diff --check`: pass.
- Không commit/push.

## Cấu hình SMTP production

Code không lưu credential. SMTP production đọc các biến môi trường
`SMTP_HOST`, `SMTP_PORT`, `SMTP_FROM`, `SMTP_TO`, và tùy chọn
`SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_STARTTLS`, `SMTP_SSL`.

Môi trường nghiệm thu không có SMTP production credential, vì vậy delivery
được chứng minh bằng SMTP receiver cô lập. Khi bàn giao production cần cấp
các biến môi trường trên để gửi tới hộp thư thật; không cần sửa code.
