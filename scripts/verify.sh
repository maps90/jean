#!/usr/bin/env bash
# Single quality gate for jean: lint, format-check, tests. Run before every commit.
# Usage: ./scripts/verify.sh   (or: uv run ./scripts/verify.sh)
set -euo pipefail
cd "$(dirname "$0")/.."

echo "== ruff check =="
uv run ruff check src tests

echo "== ruff format --check =="
uv run ruff format --check src tests

echo "== pytest =="
uv run pytest -q

echo "== verify OK =="
