import sqlite3

conn = sqlite3.connect('ngo.db')
cursor = conn.cursor()

cursor.execute("PRAGMA table_info(photos)")
print("Columns:", [row[1] for row in cursor.fetchall()])
print("-" * 70)

cursor.execute("SELECT id, filename, programme, uploaded_by, parent_folder FROM photos ORDER BY id")
rows = cursor.fetchall()

if not rows:
    print("No rows found in photos table.")
else:
    for row in rows:
        rid, filename, programme, uploaded_by, parent_folder = row
        print(f"id={rid} | filename={filename!r} | programme={programme!r} | parent_folder={parent_folder!r}")

conn.close()