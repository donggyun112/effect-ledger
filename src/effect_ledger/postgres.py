"""Optional PostgreSQL backend for hosts sharing one authoritative database."""
from __future__ import annotations

import hashlib
from collections.abc import Generator
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

from ._sql import SQLStore
from .models import _text


class _Connection:
    def __init__(self, connection):
        self.connection = connection

    def execute(self, statement, parameters=()):
        # Only internal static SQL uses this adapter; values remain parameters.
        return self.connection.execute(statement.replace('?', '%s'), parameters)


class PostgresOperationStore(SQLStore):
    """Serialize short transactions per scope using a shared PostgreSQL database.

    Creates missing tables in the configured search_path; does not migrate schemas.
    Opens one connection per transaction. Durability and failover are host duties."""

    def __init__(self, dsn: str) -> None:
        self.dsn = _text(dsn, 'dsn')
        self._initialize()

    @contextmanager
    def _transaction(self, scope: str) -> Generator[_Connection, None, None]:
        # This string names the advisory lock, so hosts only exclude each other
        # while they agree on it. Changing it must not be a rolling deploy.
        lock = int.from_bytes(hashlib.sha256(
            ('effect-ledger:' + scope).encode()).digest()[:8],
            'big', signed=True)
        with psycopg.connect(self.dsn, row_factory=dict_row, connect_timeout=10) as db:
            db.execute('SET TRANSACTION ISOLATION LEVEL READ COMMITTED')
            db.execute("SET LOCAL synchronous_commit = 'on'")
            db.execute("SET LOCAL lock_timeout = '10s'")
            db.execute('SELECT pg_advisory_xact_lock(%s)', (lock,))
            yield _Connection(db)
        # Connection context commits before the store method can return a claim.
