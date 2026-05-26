import mysql.connector

def get_connection():
    return mysql.connector.connect(
        host="localhost",
        user="root",              # đổi nếu bạn dùng user khác
        password="123456", # mật khẩu MySQL
        database="cictadmin_db",
        charset="utf8mb4"
    )
