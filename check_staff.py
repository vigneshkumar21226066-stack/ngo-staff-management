import sqlite3

conn = sqlite3.connect('ngo.db')
cursor = conn.cursor()
cursor.execute("SELECT username, role FROM staff")
all_staff = cursor.fetchall()
conn.close()

print("Accounts in database:")
for s in all_staff:
    print(s)