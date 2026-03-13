import sqlite3

def get_user_by_username(db_path: str, username: str):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    query = "SELECT id, username, role FROM users WHERE username = '" + username + "'"
    cur.execute(query)
    row = cur.fetchone()
    conn.close()
    return row
