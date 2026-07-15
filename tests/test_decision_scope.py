from __future__ import annotations

from jean.db.memory import MemoryStore


async def test_resolve_defaults_to_once():
    store = MemoryStore()
    await store.create("a1", "C1", "1.0", "deploy")
    await store.resolve("a1", True, "U1")
    decision = await store.wait("a1", 0.05)
    assert decision.approved is True
    assert decision.by == "U1"
    assert decision.scope == "once"


async def test_resolve_carries_always_scope():
    store = MemoryStore()
    await store.create("a2", "C1", "1.0", "delete a pod")
    await store.resolve("a2", True, "U2", scope="always")
    decision = await store.wait("a2", 0.05)
    assert decision.approved is True
    assert decision.scope == "always"


async def test_timeout_decision_is_once_scoped_system_deny():
    store = MemoryStore()
    await store.create("a3", "C1", "1.0", "deploy")
    decision = await store.wait("a3", 0.01)
    assert decision.approved is False
    assert decision.by == "system"
    assert decision.scope == "once"
