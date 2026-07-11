FROM python:3.11-slim

# --- Where the `claude` CLI comes from --------------------------------------
# The SDK (claude-agent-sdk, pinned in pyproject.toml) shells out to the
# Claude Code CLI at runtime. Verified against the installed 0.2.110 package:
# it bundles a platform-specific `claude` binary inside its own wheel (see
# `claude_agent_sdk/_bundled/claude` and `_find_bundled_cli()` in
# `claude_agent_sdk/_internal/transport/subprocess_cli.py`, which is tried
# BEFORE falling back to a system-installed `claude` on PATH). The wheel's
# tag is platform-specific (e.g. `py3-none-manylinux_..._x86_64`) precisely
# because of this bundled binary, so `uv sync` below -- run inside this
# image -- pulls the correct one for the image's architecture automatically.
# No separate `npm install -g @anthropic-ai/claude-code` step is required.
# -----------------------------------------------------------------------------

# Runtime deps for plugin loading (python:3.11-slim ships neither):
#  - git:  GitMarketplaceResolver (jean.plugins) clones the marketplace repos
#          named in jean.json at boot.
#  - node/npm: oka-skills plugins bring `npx`-based MCP servers (e.g.
#          mcp-server-kubernetes, @elastic/mcp-server-elasticsearch) that the
#          agent SDK spawns on demand.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git nodejs npm \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv

WORKDIR /app

# Install deps first (better layer caching), then copy source and re-sync so
# the `jean` console script is installed too.
COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --no-install-project

COPY src ./src
COPY README.md ./
RUN uv sync --no-dev

ENV PATH="/app/.venv/bin:$PATH"

# Informational only -- see docker-compose.yaml, which intentionally does not
# map this to a host port so `docker compose up --scale jean=N` works without
# port collisions between replicas.
EXPOSE 8080

CMD ["uv", "run", "jean"]
