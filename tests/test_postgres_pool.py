from __future__ import annotations

import asyncpg
import pytest

from jean.db.postgres import PostgresStore


class _FakeConn:
    def __init__(self) -> None:
        self.executed: list[str] = []

    async def execute(self, sql: str) -> None:
        self.executed.append(sql)


class _FakeAcquire:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakePool:
    def __init__(self) -> None:
        self.conn = _FakeConn()

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self.conn)


@pytest.fixture
def captured_pool(monkeypatch):
    """Capture the kwargs PostgresStore.connect hands to asyncpg.create_pool."""
    calls: dict[str, object] = {}

    async def fake_create_pool(dsn: str, **kwargs: object) -> _FakePool:
        calls["dsn"] = dsn
        calls.update(kwargs)
        return _FakePool()

    monkeypatch.setattr(asyncpg, "create_pool", fake_create_pool)
    return calls


async def test_connect_defaults_to_a_modest_pool(captured_pool):
    # The default must stay small: jean shares a managed Postgres whose
    # `max_connections` budget is easily exhausted by a fat per-worker pool.
    await PostgresStore.connect("postgresql://x/y")
    assert captured_pool["min_size"] == 1
    assert captured_pool["max_size"] == 5


async def test_connect_honors_explicit_pool_size(captured_pool):
    await PostgresStore.connect("postgresql://x/y", min_size=2, max_size=3)
    assert captured_pool["min_size"] == 2
    assert captured_pool["max_size"] == 3


async def test_connect_still_applies_schema(captured_pool):
    store = await PostgresStore.connect("postgresql://x/y")
    assert any("CREATE TABLE IF NOT EXISTS sessions" in s for s in store._pool.conn.executed)
