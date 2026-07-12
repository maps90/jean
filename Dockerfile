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

# Runtime deps for plugin loading (python:3.11-slim ships none of these):
#  - ca-certificates: TLS trust store for HTTPS — token clones over
#          https://github.com, npx package downloads, and the Anthropic API.
#  - curl: general fetch utility (health checks, ad-hoc debugging).
#  - git:  GitMarketplaceResolver (jean.plugins) clones the marketplace repos
#          named in jean.json at boot.
#  - openssh-client: git shells out to `ssh` for a git@/ssh:// marketplace url
#          (GitMarketplaceResolver clones those verbatim over SSH). Without it
#          git dies with `cannot run ssh: No such file or directory`.
#  - node/npm: oka-skills plugins bring `npx`-based MCP servers (e.g.
#          kubernetes-mcp-server, @elastic/mcp-server-elasticsearch) that the
#          agent SDK spawns on demand.
#
# `uv` (installed below) is a RUNTIME dependency too, not just the build tool
# that runs `uv sync`: it ships `uvx`, and the grafana plugin's MCP server is
# spawned as `uvx mcp-grafana`. Dropping uv from the final image -- e.g. when
# making this multi-stage -- would take grafana's tools down with it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git openssh-client nodejs npm \
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

# --- Run as non-root ---------------------------------------------------------
# Not just hardening: the CLI *refuses to start* as root when jean runs with
# permission_mode=bypassPermissions (the SDK passes --dangerously-skip-permissions,
# and the CLI rejects that under root/sudo "for security reasons", exit 1). So a
# root container breaks every turn, not merely a risky one.
#
# HOME must be a writable dir owned by this uid: the CLI writes its config and
# per-conversation transcripts under $HOME/.claude. Deployments that mount a
# volume (k8s emptyDir) should point HOME at it and set fsGroup=10001; the
# in-image /home/jean is the standalone-docker fallback.
RUN useradd --create-home --uid 10001 --user-group jean
USER 10001
ENV HOME=/home/jean

# Informational only -- see docker-compose.yaml, which intentionally does not
# map this to a host port so `docker compose up --scale jean=N` works without
# port collisions between replicas.
EXPOSE 8080

# Exec the console script from the venv on PATH rather than `uv run`, which
# re-resolves the project at startup and wants to write to /app -- root-owned
# from the build, and no longer writable now that we drop to uid 10001.
CMD ["jean"]
