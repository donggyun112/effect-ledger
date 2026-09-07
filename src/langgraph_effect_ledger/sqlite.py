"""Persistent local SQLite backend, safe across processes on one host."""
from __future__ import annotations

import sqlite3
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from ._sql import SQLStore


class SQLiteOperationStore(SQLStore):
    """Use a shared persistent local file, never a copied or network-mounted DB."""

    def __init__(self, path: str | Path) -> None:
        if str(path) in ("", ":memory:"):
            raise ValueError("A persistent SQLite file is required")
        self.path = str(Path(path).resolve())
        self._initialize()

    @contextmanager
    def _transaction(self, scope: str) -> Generator[sqlite3.Connection, None, None]:
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()
