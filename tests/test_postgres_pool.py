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


async def test_connect_defaults_search_path_to_public(captured_pool):
    await PostgresStore.connect("postgresql://x/y")
    assert captured_pool["server_settings"] == {"search_path": "public"}


async def test_named_schema_sets_search_path_and_creates_schema_first(captured_pool):
    # A named schema is applied to every pooled connection via search_path, and
    # the schema is created BEFORE the tables -- else CREATE TABLE has nowhere to
    # land under that search_path.
    store = await PostgresStore.connect("postgresql://x/y", schema="anya")
    assert captured_pool["server_settings"] == {"search_path": "anya"}
    executed = store._pool.conn.executed
    schema_idx = next(
        i for i, s in enumerate(executed) if 'CREATE SCHEMA IF NOT EXISTS "anya"' in s
    )
    table_idx = next(
        i for i, s in enumerate(executed) if "CREATE TABLE IF NOT EXISTS sessions" in s
    )
    assert schema_idx < table_idx


def test_default_schema_keeps_bare_global_names():
    # "public" (the single-agent default) must keep the historical NOTIFY channel
    # and advisory-lock keys, so an in-place upgrade never renames them mid-rollout.
    store = PostgresStore(pool=None, dsn="postgresql://x/y")  # type: ignore[arg-type]
    assert store._notify_channel == "jean_approvals"
    assert store._cleanup_key == "jean_cleanup"
    assert store._lock_prefix == ""


def test_named_schema_namespaces_global_primitives():
    # A per-agent schema namespaces the NOTIFY channel, the cleanup advisory-lock
    # key, and the per-thread lock key -- these are per-DATABASE, not per-schema,
    # so without this two agents sharing one DB would cross-signal each other.
    store = PostgresStore(pool=None, dsn="postgresql://x/y", schema="damian")  # type: ignore[arg-type]
    assert store._notify_channel == "jean_approvals_damian"
    assert store._cleanup_key == "jean_cleanup_damian"
    assert store._lock_prefix == "damian:"
