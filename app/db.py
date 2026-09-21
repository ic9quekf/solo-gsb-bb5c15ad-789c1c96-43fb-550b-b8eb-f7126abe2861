"""SQLite persistence layer.

The database file location is configurable through the ``DATABASE_PATH``
environment variable and resolved lazily so tests can point the app at a
temporary file. The schema is created idempotently at application startup.
"""
from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from typing import Iterator

DEFAULT_DB_PATH = os.path.join("data", "recall.db")

SCHEMA = """
CREATE TABLE IF NOT EXISTS transformations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS transformation_inputs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    transformation_id INTEGER NOT NULL REFERENCES transformations(id) ON DELETE CASCADE,
    batch_id TEXT NOT NULL,
    quantity REAL NOT NULL CHECK (quantity > 0)
);

CREATE TABLE IF NOT EXISTS transformation_outputs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    transformation_id INTEGER NOT NULL REFERENCES transformations(id) ON DELETE CASCADE,
    batch_id TEXT NOT NULL,
    quantity REAL NOT NULL CHECK (quantity > 0)
);

CREATE INDEX IF NOT EXISTS idx_transformation_inputs_batch
    ON transformation_inputs (batch_id);
CREATE INDEX IF NOT EXISTS idx_transformation_outputs_batch
    ON transformation_outputs (batch_id);
CREATE INDEX IF NOT EXISTS idx_transformation_inputs_tid
    ON transformation_inputs (transformation_id);
CREATE INDEX IF NOT EXISTS idx_transformation_outputs_tid
    ON transformation_outputs (transformation_id);
"""


def database_path() -> str:
    """Resolve the SQLite file location (read lazily so tests can override it)."""
    return os.environ.get("DATABASE_PATH", DEFAULT_DB_PATH)


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    """Open a short-lived autocommit connection."""
    conn = sqlite3.connect(database_path(), timeout=10, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def write_transaction() -> Iterator[sqlite3.Connection]:
    """Serialize a read-check-write sequence in a single immediate transaction.

    ``BEGIN IMMEDIATE`` acquires the SQLite write lock up front, so the cycle
    check and the insert that depends on it cannot race another writer.
    """
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise


def init_db() -> None:
    """Create the database file and schema if they do not exist yet."""
    path = database_path()
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with connect() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
