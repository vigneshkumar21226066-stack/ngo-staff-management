"""
One-time migration: adds a 'parent_folder' column to the existing
photos table to support one level of subfolders.

Run this ONCE from your project folder:
    python migrate_add_parent_folder.py

Safe to run multiple times - it checks if the column already exists first.
"""
import sqlite3

conn = sqlite3.connect('ngo.db')
cursor = conn.cursor()

cursor.execute("PRAGMA table_info(photos)")
columns = [row[1] for row in cursor.fetchall()]

if 'parent_folder' not in columns:
    cursor.execute("ALTER TABLE photos ADD COLUMN parent_folder TEXT")
    conn.commit()
    print("✅ Added 'parent_folder' column to photos table.")
else:
    print("ℹ️  'parent_folder' column already exists. Nothing to do.")

conn.close()