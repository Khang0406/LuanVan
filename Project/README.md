# CICTAdmin — Hướng dẫn cài đặt (Tiếng Việt)

Một ứng dụng quản trị máy chủ/website nhỏ viết bằng Flask.

---

## 🧰 Yêu cầu

- Python 3.8 hoặc mới hơn
- pip (đi kèm Python)
- (Tùy chọn) Quyền cài đặt/biên dịch trên hệ thống để cài một số thư viện như `paramiko` / `cryptography` trên Windows

## ⚙️ Cài đặt nhanh

1. Clone repository:

```bash
git clone <https://github.com/KietB2204943/Server-Control---CICTAdmin.git>
cd Project
```

2. Tạo và kích hoạt virtual environment (khuyến nghị):

- Trên Windows (PowerShell):

```powershell
python -m venv venv
venv\Scripts\Activate.ps1
```

- Trên Windows (cmd):

```cmd
python -m venv venv
venv\Scripts\activate.bat
```

- Trên macOS / Linux:

```bash
python3 -m venv venv
source venv/bin/activate
```

3. Cài phụ thuộc:

```bash
pip install Flask paramiko psutil
```

Hoặc tạo file `requirements.txt` với nội dung đề xuất sau và chạy `pip install -r requirements.txt`:

```
Flask>=2.0
paramiko>=2.11
psutil>=5.8
```

> Mẹo: nếu gặp lỗi khi cài `paramiko` (liên quan đến `cryptography`) trên Windows thì hãy chạy `pip install --upgrade pip wheel setuptools` trước và cài đặt lại. Hoặc cài Visual C++ Build Tools nếu cần.

## 🧩 Cấu hình

- File cấu hình chính là `app.py`.
- Mặc định `app.secret_key` được đặt sẵn trong `app.py`; **hãy thay bằng một chuỗi bí mật khác** trước khi đưa vào môi trường production.
- Cơ sở dữ liệu SQLite sẽ được tạo tự động trong file `cictadmin.db` khi chạy ứng dụng lần đầu.

## 🚀 Khởi chạy ứng dụng

Chạy ứng dụng ở chế độ phát triển:

```bash
python app.py
```

Ứng dụng sẽ chạy tại: `http://127.0.0.1:5001` (mặc định).

> Khi chạy lần đầu, hệ thống sẽ tạo `cictadmin.db` và tạo một tài khoản quản trị mặc định `admin` / `admin`. Hãy đăng nhập và đổi mật khẩu ngay sau đó.

## 🛡️ Triển khai production

- Trên Linux, bạn có thể dùng `gunicorn`:

```bash
pip install gunicorn
gunicorn -w 4 -b 0.0.0.0:8000 app:app
```

- Trên Windows, bạn có thể dùng `waitress`:

```bash
pip install waitress
waitress-serve --listen=*:5000 app:app
```

Luôn đặt phía trước một reverse-proxy (Nginx) và **sử dụng HTTPS** cho môi trường production.

## 🔒 Lưu ý bảo mật

- Đổi `app.secret_key` và mật khẩu `admin` mặc định ngay lập tức.
- Sử dụng mật khẩu mạnh và/hoặc xác thực 2 yếu tố nếu triển khai thực tế.
- Hạn chế truy cập tới file `cictadmin.db` và sao lưu định kỳ.
- Kiểm tra danh sách lệnh bị cấm trong `FORBIDDEN_CMDS` (nếu sử dụng terminal từ web).

## 🧪 Kiểm tra & Khắc phục lỗi

- Nếu gặp lỗi import khi chạy `python app.py`, kiểm tra xem bạn đã kích hoạt virtualenv và cài đủ các package chưa (`Flask`, `paramiko`, `psutil`).
- Lỗi cài `paramiko` thường do phụ thuộc `cryptography`; thử nâng pip/setuptools/ wheel hoặc cài compiler cần thiết trên Windows.

## ℹ️ Thông tin thêm

- Cơ sở dữ liệu: `cictadmin.db` (SQLite)
- Script hữu ích: `update_admin.py` để đảm bảo user `admin` có quyền `admin` trong DB.

---
