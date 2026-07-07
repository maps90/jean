from __future__ import annotations

import importlib


def test_server_module_imports_without_touching_infra():
    """server.py is the composition root: importing it must never construct a
    DB pool, a Slack client, or start anything -- only `run()`/`main()` do
    that. This is what lets the default `uv run pytest` stay DB-free."""
    module = importlib.import_module("jean.server")
    assert callable(module.main)
    assert callable(module.run)
