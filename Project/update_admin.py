import sqlite3
db = sqlite3.connect("cictadmin.db")
db.execute("UPDATE users SET role='admin' WHERE username='admin'")
db.commit()
db.close()
    