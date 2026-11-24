"""PostgreSQL access (psycopg 3). One small helper layer; SQL stays explicit."""
from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, Iterator

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from .config import REPO_ROOT, get_settings

_pool: ConnectionPool | None = None

import atexit


def _configure(conn: psycopg.Connection) -> None:
    conn.execute("SET search_path = itsm, public")
    conn.commit()


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        _pool = ConnectionPool(get_settings().database_url, min_size=1, max_size=16,
                               kwargs={"row_factory": dict_row}, configure=_configure, open=True)
        atexit.register(close_pool)
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def tx() -> Iterator[psycopg.Connection]:
    """A connection inside a transaction; commits on success, rolls back on error."""
    with pool().connection() as conn:
        with conn.transaction():
            yield conn


def q(sql: str, params: Any = None) -> list[dict]:
    with tx() as c:
        cur = c.execute(sql, params)
        return cur.fetchall() if cur.description else []


def q1(sql: str, params: Any = None) -> dict | None:
    rows = q(sql, params)
    return rows[0] if rows else None


def j(v: Any) -> Jsonb:
    return Jsonb(v)


def migrate(database_url: str | None = None) -> None:
    """Apply schema/schema.sql to an empty database (idempotent guard on the itsm schema)."""
    url = database_url or get_settings().database_url
    with psycopg.connect(url, autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM information_schema.schemata WHERE schema_name = 'itsm'").fetchone()
        if exists:
            return
        conn.execute((REPO_ROOT / "schema/schema.sql").read_text())


def dumps(v: Any) -> str:
    """Canonical JSON: sorted keys, no insignificant whitespace (docs/02 §5.3)."""
    return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
