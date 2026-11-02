"""SQLite access: WAL, foreign keys, ordered SQL migrations, short transactions.

Choice: stdlib `sqlite3` with short synchronous transactions instead of `aiosqlite`. Every
statement here is a sub-millisecond local write; a single connection guarded by a lock keeps
transactions serialized and simple, and per-group asyncio locks sit above it. This avoids a
thread hop per query. A future PostgreSQL move replaces this module and repositories only.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._depth = 0
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 5000")
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")
        self.migrate()

    def migrate(self) -> list[str]:
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT DEFAULT CURRENT_TIMESTAMP)"
            )
            done = {r[0] for r in self._conn.execute("SELECT name FROM schema_migrations")}
            applied = []
            files = sorted(
                (f for f in resources.files("app.persistence.migrations").iterdir() if f.name.endswith(".sql")),
                key=lambda f: f.name,
            )
            for f in files:
                if f.name in done:
                    continue
                sql = f.read_text()
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    for stmt in _split_sql(sql):
                        self._conn.execute(stmt)
                    self._conn.execute("INSERT INTO schema_migrations(name) VALUES (?)", (f.name,))
                    self._conn.execute("COMMIT")
                except Exception:
                    self._conn.execute("ROLLBACK")
                    raise
                applied.append(f.name)
            return applied

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Re-entrant transaction: outermost block commits or rolls back."""
        with self._lock:
            outer = self._depth == 0
            if outer:
                self._conn.execute("BEGIN IMMEDIATE")
            self._depth += 1
            try:
                yield self._conn
            except BaseException:
                self._depth -= 1
                if outer:
                    self._conn.execute("ROLLBACK")
                raise
            self._depth -= 1
            if outer:
                self._conn.execute("COMMIT")

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _split_sql(sql: str) -> list[str]:
    lines = [ln for ln in sql.splitlines() if not ln.strip().startswith("--")]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]
