"""SQLite 连接管理与建库。

使用标准库 sqlite3，开启外键约束与 WAL 以获得更好的并发读体验。
建库在应用启动时执行（CREATE TABLE IF NOT EXISTS），因此容器挂载
空数据卷后首次启动也会自动完成建表，镜像构建时同样会预建一次。
"""

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    id              TEXT PRIMARY KEY,
    quantity        REAL NOT NULL CHECK (quantity >= 0),
    produced_by_txn TEXT REFERENCES transformations(id),
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS transformations (
    id              TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS transformation_items (
    txn_id    TEXT NOT NULL REFERENCES transformations(id),
    batch_id  TEXT NOT NULL REFERENCES batches(id),
    role      TEXT NOT NULL CHECK (role IN ('input', 'output')),
    quantity  REAL NOT NULL CHECK (quantity > 0),
    ord       INTEGER NOT NULL,
    PRIMARY KEY (txn_id, role, batch_id)
);

CREATE INDEX IF NOT EXISTS idx_items_batch ON transformation_items(batch_id);
CREATE INDEX IF NOT EXISTS idx_items_txn ON transformation_items(txn_id);
CREATE INDEX IF NOT EXISTS idx_batches_producer ON batches(produced_by_txn);
"""


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or config.DB_PATH
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def init_db(db_path: str | None = None) -> None:
    """幂等建库。"""
    conn = connect(db_path)
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


def get_conn() -> Iterator[sqlite3.Connection]:
    """FastAPI 依赖：每请求一个连接，结束时关闭（原生生成器函数）。"""
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """立即模式事务，保证写入串行化、校验与写入原子提交。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")
