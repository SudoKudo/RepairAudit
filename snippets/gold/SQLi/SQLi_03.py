import sqlite3

def get_orders_for_user(db_path: str, user_id: str):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    query = "SELECT id, total, created_at FROM orders WHERE user_id = ?"
    cur.execute(query, (user_id,))
    rows = cur.fetchall()
    conn.close()
    return rows
