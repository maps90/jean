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
#  - node/npm: marketplace plugins bring `npx`-based MCP servers (e.g.
#          kubernetes-mcp-server, @elastic/mcp-server-elasticsearch) that the
#          agent SDK spawns on demand.
#
# The rest are the toolchain the anthropics/skills document skills shell out to
# (see jean.json). Their SKILL.md files all say the same thing -- "preinstalled,
# do NOT run npm/pip install first" -- and they mean it two ways here: an
# install at turn time is classified RISKY (approval/risk.py `_PROD_INFRA`
# matches `pip install` / `npm install`), so it costs a human click, and it
# lands in a pod filesystem that dies with the pod. Bake them in or the skills
# are decorative:
#  - libreoffice-{writer,calc,impress}: `soffice`, used to recalculate xlsx
#          formulas, convert to PDF, and render pptx slide thumbnails. Deliberately
#          no default-jre: the skills drive Basic macros, which need no Java.
#  - fonts-liberation: metric-compatible Arial/Times/Courier substitutes, so a
#          rendered document paginates like the one the requester will open.
#  - pandoc: docx <-> markdown conversion.
#  - poppler-utils: `pdftoppm`, how the skills rasterize a PDF to check their
#          own output before handing it over.
#
# `uv` (installed below) is a RUNTIME dependency too, not just the build tool
# that runs `uv sync`: it ships `uvx`, and the grafana plugin's MCP server is
# spawned as `uvx mcp-grafana`. Dropping uv from the final image -- e.g. when
# making this multi-stage -- would take grafana's tools down with it.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates curl git openssh-client nodejs npm \
        libreoffice-writer libreoffice-calc libreoffice-impress \
        fonts-liberation pandoc poppler-utils \
    && rm -rf /var/lib/apt/lists/*

# `az`, for the cloud-diagnostic skills: VPN gateway connection state, effective
# route tables, and Azure Monitor metrics. Same bake-it-in argument as the block
# above, and it costs ~800 MB installed, so it is worth being explicit about what
# is NOT here for the same skills: ping, iptables and conntrack stay out. A pod is
# the wrong vantage point to measure another host's network from, so those would
# only produce confident nonsense measured against the pod's own path. `jq` stays
# out too, since `az --query` is JMESPath already.
#
# NOT from PyPI. `pip install azure-cli` and `uv tool install azure-cli` both
# resolve to 2.0.67 (2019) and die at startup on `time.clock()`, removed in
# Python 3.8. They also need `setuptools<81` to keep `pkg_resources` around. The
# vendor apt repo is the only path that yields a current CLI.
#
# The suite is pinned to bookworm deliberately: this image is trixie, and the
# vendor publishes no trixie suite for azure-cli (404, checked 2026-08). That is
# also why the vendor's own install script cannot be used here -- it derives the
# suite from `lsb_release -cs` and would 404. `signed-by` points at the armored
# key directly, which apt has accepted since bookworm, so no gnupg is needed.
RUN install -d /etc/apt/keyrings \
    && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc \
         -o /etc/apt/keyrings/microsoft.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/microsoft.asc] \
https://packages.microsoft.com/repos/azure-cli/ bookworm main" \
         > /etc/apt/sources.list.d/azure-cli.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends azure-cli \
    && rm -rf /var/lib/apt/lists/*

# Node libraries the docx/pptx skills `require()` directly. Installed globally
# with an explicit prefix (rather than relying on npm's default) so NODE_PATH
# can name the resulting dir: the skills write throwaway scripts under the
# agent's workspace, which has no node_modules of its own and no package.json,
# so `require('docx')` resolves only via NODE_PATH.
ENV NPM_CONFIG_PREFIX=/usr/local
ENV NODE_PATH=/usr/local/lib/node_modules
RUN npm install -g --no-fund --no-audit \
        docx pptxgenjs sharp react react-dom react-icons \
    && npm cache clean --force

RUN pip install --no-cache-dir uv

WORKDIR /app

# Install deps first (better layer caching), then copy source and re-sync so
# the `jean` console script is installed too.
COPY pyproject.toml uv.lock ./
RUN uv sync --no-dev --no-install-project

COPY src ./src
COPY README.md ./
RUN uv sync --no-dev

# Python libraries the xlsx/pptx/docx skills import. These are NOT jean's own
# dependencies -- jean never imports them; the agent's scripts do -- so they
# stay out of pyproject.toml and land in the same venv only because that is
# what `python3` resolves to once PATH is set below.
#
# MUST stay after the last `uv sync`: sync makes the venv match the lockfile
# exactly and would prune every one of these back out again. Adding another
# sync below this line silently disarms the document skills.
#
# `python-docx` is NOT in any skill's documented dependency list and no skill
# script imports it -- the docx skill prescribes the npm `docx` library, which is
# installed above. It is here because the AGENT reaches for it anyway: asked to
# write a .docx, the model's prior is python-docx, and it ran `pip install
# python-docx Pillow` in production. That costs an approval click (pip install is
# RISKY by design) and then evaporates, because a pip install lands in the pod
# filesystem and dies with the pod -- so it re-installs and re-prompts on every
# restart, forever. Cheaper to satisfy the instinct than to keep refusing it.
#
# `pypdf` and `pdf2image` are the `pdf` skill's own imports. That skill is narrowed
# out of jean.json today (`skills: ["docx","xlsx","pptx"]`), so nothing needs them
# yet -- they are here so that enabling it later is a one-line config change and
# not a repeat of the python-docx loop. `pdfplumber` arrives transitively via
# markitdown[pdf] and is already satisfied.
#
# Deliberately NOT here: reportlab, fpdf, PyMuPDF. Nothing generates a PDF
# directly -- the skills convert through LibreOffice (`soffice --convert-to pdf`),
# which is installed above. Adding a PDF writer would invite the agent to bypass
# that path for worse-looking output.
RUN uv pip install --python /app/.venv/bin/python --no-cache-dir \
        openpyxl pandas 'markitdown[docx,pptx,xlsx,pdf]' Pillow defusedxml lxml \
        python-docx pypdf pdf2image

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
