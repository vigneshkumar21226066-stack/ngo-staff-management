import sqlite3
from werkzeug.security import generate_password_hash

conn = sqlite3.connect('ngo.db')
cursor = conn.cursor()

username = "admin"
password = "changeme123"
hashed_password = generate_password_hash(password)

cursor.execute(
    "INSERT INTO staff (username, password, role) VALUES (?, ?, ?)",
    (username, hashed_password, "admin")
)

conn.commit()
conn.close()

print(f"Admin account created: username='{username}', password='{password}'")