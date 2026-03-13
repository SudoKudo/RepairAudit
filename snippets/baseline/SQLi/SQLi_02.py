import sqlite3

def search_products(db_path: str, term: str, limit: int = 10):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    query = f"SELECT id, name FROM products WHERE name LIKE '%{term}%' LIMIT {limit}"
    cur.execute(query)
    rows = cur.fetchall()
    conn.close()
    return rows
