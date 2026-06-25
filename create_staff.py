import sqlite3
from werkzeug.security import generate_password_hash

conn = sqlite3.connect('ngo.db')
cursor = conn.cursor()

username = "staff1"
password = "staff123"
hashed_password = generate_password_hash(password)

cursor.execute(
    "INSERT INTO staff (username, password, role) VALUES (?, ?, ?)",
    (username, hashed_password, "staff")
)

conn.commit()
conn.close()

print(f"Staff account created: username='{username}', password='{password}'")