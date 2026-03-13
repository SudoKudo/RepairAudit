import sqlite3

_ALLOWED_SORT_FIELDS = {"id", "username", "role"}

def admin_filter_users(db_path: str, sort_field: str):
    if sort_field not in _ALLOWED_SORT_FIELDS:
        sort_field = "id"

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    query = f"SELECT id, username, role FROM users ORDER BY {sort_field}"
    cur.execute(query)
    rows = cur.fetchall()
    conn.close()
    return rows
