import sqlite3

def admin_filter_users(db_path: str, sort_field: str):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    query = "SELECT id, username, role FROM users ORDER BY " + sort_field
    cur.execute(query)
    rows = cur.fetchall()
    conn.close()
    return rows
