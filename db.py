import sqlite3

import config

PG = bool(config.DATABASE_URL)  # на Render база Postgres, локально — файл SQLite

if PG:
    import psycopg
    from psycopg.rows import dict_row

    def _connect():
        return psycopg.connect(config.DATABASE_URL, autocommit=True, row_factory=dict_row)

    conn = _connect()
else:
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row


class _Rows:
    def __init__(self, rows, rowcount):
        self.rows, self.rowcount = rows, rowcount

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


def q(sql, *args):
    global conn
    if PG:
        sql = sql.replace("?", "%s")
        try:
            return conn.execute(sql, args)
        except psycopg.OperationalError:  # бесплатная база закрывает простаивающие соединения
            conn = _connect()
            return conn.execute(sql, args)
    cur = conn.execute(sql, args)
    if "RETURNING" in sql:  # sqlite не даёт коммитить, пока строки не прочитаны
        rows = cur.fetchall()
        conn.commit()
        return _Rows(rows, cur.rowcount)
    conn.commit()
    return cur


def get_setting(key, default=None):
    row = q("SELECT value FROM settings WHERE key=?", key).fetchone()
    return row["value"] if row else default


def set_setting(key, value):
    q("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", key, value)


_ID = "BIGSERIAL PRIMARY KEY" if PG else "INTEGER PRIMARY KEY AUTOINCREMENT"
for _stmt in (
    f"""CREATE TABLE IF NOT EXISTS requests(
        id {_ID},
        user_id BIGINT, username TEXT, full_name TEXT,
        purpose TEXT, visit_at TEXT, people INTEGER,
        status TEXT DEFAULT 'pending', code TEXT,
        reminded INTEGER DEFAULT 0, created_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS votes(
        request_id BIGINT, admin_id BIGINT, approve INTEGER,
        PRIMARY KEY(request_id, admin_id))""",
    "CREATE TABLE IF NOT EXISTS admin_msgs(request_id BIGINT, admin_id BIGINT, message_id BIGINT)",
    "CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)",
    "CREATE TABLE IF NOT EXISTS blocked(user_id BIGINT PRIMARY KEY)",
    "CREATE TABLE IF NOT EXISTS relay_usage(user_id BIGINT, day TEXT, n INTEGER DEFAULT 0, PRIMARY KEY(user_id, day))",
    "CREATE TABLE IF NOT EXISTS chat_admins(request_id BIGINT, admin_id BIGINT, PRIMARY KEY(request_id, admin_id))",
):
    q(_stmt)

# миграции для баз, созданных старой версией бота
for _stmt in (
    "ALTER TABLE votes ADD COLUMN reason TEXT",
    "ALTER TABLE blocked ADD COLUMN name TEXT",
):
    try:
        q(_stmt)
    except Exception:  # колонка уже есть
        pass
