# Backup và restore

## Phạm vi

Backup chứa:

- snapshot nhất quán của `app.db`;
- `backup-manifest.json`;
- `secret-metadata.json` chỉ gồm reference, key và metadata không nhạy cảm.

Backup không chứa application secret value, registry token/password hoặc GitHub
webhook secret. Các giá trị này phải được bảo vệ riêng bằng Kubernetes Secret,
password manager hoặc Vault. Không đóng gói plaintext secret store vào archive.

## Backup

Chạy trong maintenance window hoặc Job có mount PVC read-only phù hợp:

```bash
python scripts/backup_platform_data.py \
  --source /var/lib/platform \
  --output /var/lib/platform-backups
```

Script dùng SQLite online backup API, chạy integrity check và tạo file mode 0600
trong thư mục mode 0700. Sao chép thư mục kết quả sang kho backup mã hóa và có
retention; không dùng chính PVC Platform làm bản sao duy nhất.

## Restore

1. Dừng web và worker để không còn writer.
2. Xác nhận backup manifest có `secret_values_included: false`.
3. Chụp một backup an toàn của trạng thái hiện tại.
4. Chạy:

```bash
python scripts/restore_platform_data.py \
  --backup /secure-backups/platform-TIMESTAMP \
  --destination /var/lib/platform
```

5. Rehydrate secret values từ nguồn bí mật có thẩm quyền.
6. Khởi động lại Pod; kiểm tra `/healthz`, `/readyz` và worker log.
7. Xác nhận application, deployment history, pipeline/job và audit đã trở lại.
8. Chạy secret scan trên output nghiệm thu, không dump database hay Secret.

Restore dùng file tạm và `os.replace`, xóa WAL/SHM cũ sau khi kiểm tra integrity.

## Kịch bản vòng kín

Automated test Giai đoạn 4 tạo application, queued pipeline, deployment pin
digest, job và audit; backup; thay dữ liệu; restore; mở lại DB; xác nhận toàn bộ
record và quét mọi file backup để bảo đảm sentinel secret không xuất hiện.

Nghiệm thu cluster thật phải dùng namespace/application cô lập. Không sửa/xóa
Map, demo-nginx hoặc PVC production. Backup metadata-only không tự phục hồi giá
trị secret khi mất toàn bộ PVC; đây là giới hạn có chủ ý cho tới khi tích hợp
Vault/KMS.
