"""資料庫後端的小轉接層：SQLite 檔案路徑，或 ``postgresql://`` DSN（TimescaleDB）。

程式其他地方都寫 SQLite 風格的 ``?`` 佔位符與 ``connection.execute(sql, params)``；
PostgreSQL 連線包一層把 ``?`` 換成 ``%s``，其餘（``fetchone``、``rowcount``、``commit``）
psycopg 3 的介面本來就和 sqlite3 一樣。SQL 本身只用兩邊都支援的語法。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

try:  # psycopg 只有連 PostgreSQL 時才需要
    import psycopg
except ImportError:  # pragma: no cover - 沒裝 psycopg 時仍可用 SQLite
    psycopg = None

DB_ERRORS: tuple[type[Exception], ...] = (sqlite3.Error,) + ((psycopg.Error,) if psycopg else ())


def is_postgres(target: Any) -> bool:
    return isinstance(target, str) and target.startswith(("postgresql://", "postgres://"))


class PgConnection:
    """讓 psycopg 連線用起來像 sqlite3 連線（只涵蓋本專案用到的部分）。"""

    def __init__(self, dsn: str, *, autocommit: bool = False) -> None:
        if psycopg is None:
            raise RuntimeError("psycopg is required for PostgreSQL targets: pip install 'psycopg[binary]'")
        self._connection = psycopg.connect(dsn, autocommit=autocommit, connect_timeout=5)

    def execute(self, sql: str, params: tuple | list = ()):
        if "%" in sql:
            raise ValueError("SQL must not contain a literal % (placeholders are written as ?)")
        return self._connection.execute(sql.replace("?", "%s"), tuple(params))

    def commit(self) -> None:
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def close(self) -> None:
        self._connection.close()


def connect_readonly(target: str | Path):
    """唯讀連線。

    * SQLite：``mode=ro``（檔案不存在也不會建立）+ ``PRAGMA query_only``。
    * PostgreSQL：autocommit 連線。唯讀與否取決於 DSN 裡的角色，所以連上後會**檢查**這個帳號
      對資料表沒有寫入權限；有的話直接拒絕使用（見 ``WriteCapableAccountError``）。
    """
    if is_postgres(target):
        connection = PgConnection(target, autocommit=True)
        try:
            writable = connection.execute(
                "SELECT bool_or(has_table_privilege(current_user, c.oid, 'INSERT, UPDATE, DELETE, TRUNCATE')) "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p')",
            ).fetchone()[0]
            superuser = connection.execute(
                "SELECT rolsuper OR rolcreatedb OR rolcreaterole FROM pg_roles WHERE rolname = current_user",
            ).fetchone()[0]
        except Exception:
            connection.close()
            raise
        if writable or superuser:
            connection.close()
            raise WriteCapableAccountError
        return connection
    connection = sqlite3.connect(Path(target).resolve().as_uri() + "?mode=ro", uri=True, check_same_thread=False)
    connection.execute("PRAGMA query_only = ON")
    return connection


class WriteCapableAccountError(RuntimeError):
    """唯讀用途的連線拿到了有寫入權限的資料庫帳號。"""

    def __init__(self) -> None:
        super().__init__("the database account has write privileges; a read-only role is required")
