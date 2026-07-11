# Design — External config & plugin loading (Vault-ready jean)

Date: 2026-07-11
Status: draft for review

## Goal

Make jean deployable to the `admin-v2` Flux cluster with its persona, config,
and plugins sourced externally (Vault via VSO) instead of hard-wired to a local
`~/.jean/IDENTITY.md` and a single in-process MCP server. Concretely, jean must
be able to:

1. Read its **soul** (persona / `IDENTITY.md`) from a configurable path so it can
   be a mounted Secret file.
2. Load **extra MCP servers** from an `mcp.json` file, merged with the existing
   in-process `jean_slack` server.
3. Install **plugins** from a `jean.json` manifest that names a marketplace git
   repo, a plugin, and a ref — resolved at boot by cloning the marketplace over
   HTTPS with a token, then handed to the SDK as local plugins.

This is **sub-project A**. The Flux/VSO deployment is **sub-project B** (outlined
at the end); B depends on A and on a published image.

## Scope decisions (settled in brainstorming)

| Area | Decision |
|---|---|
| Cluster / path | `admin-v2`, `apps/admin-v2/jean` (flat, no base/overlay) |
| Packaging | minimal manifests: `VaultStaticSecret` + `Deployment` + `kustomization` + a Flux `Kustomization` in `clusters/admin-v2/.../custom-components.yaml` |
| Postgres | managed Azure; `JEAN_DATABASE_URL` from Vault |
| Secrets/config | one VSO-synced Secret → env vars + mounted `soul.md` / `jean.json` / `mcp.json` |
| `jean.json` schema | `{"plugins":[{"marketplace","plugin","ref"}, …]}` |
| Plugin fetch | jean clones each `marketplace@ref` at boot via HTTPS token; pin `ref` to a SHA/tag in prod |

## Non-goals

- No change to the trust boundary, gateway, approval, or session logic.
- No runtime "hot reload" of plugins/soul — loaded once at session construction
  (same seam as the existing `_SoulCell`). A restart picks up new config.
- No support for non-git marketplaces or non-`local` SDK plugin types (the SDK,
  `claude-agent-sdk 0.2.110`, only supports `type: "local"`).

## SDK facts this relies on (verified against 0.2.110)

- `ClaudeAgentOptions.plugins: list[SdkPluginConfig]` where `SdkPluginConfig =
  {"type": "local", "path": str}`. Only local plugins are supported.
- `ClaudeAgentOptions.mcp_servers: dict[str, McpServerConfig] | str | Path` — a
  dict of name→config **or** a path to an MCP JSON file (not both). To keep the
  in-process `jean_slack` server *and* add external ones, we merge into one dict.
- `strict_mcp_config: bool = False` — left False so plugin-provided `.mcp.json`
  servers still load. Setting it True would suppress them.
- `skills: list[str] | "all" | None` — the single switch to enable skills; the
  SDK adds `"Skill"` to `allowed_tools` and sets `setting_sources` itself.

## Architecture (ports & adapters, per CLAUDE.md)

Domain stays free of `git`/subprocess/network. New boundary:

### Port — `MarketplaceResolver` (`src/jean/ports.py`)

```python
@dataclass
class ResolvedPlugin:
    name: str          # plugin name, e.g. "grafana"
    path: str          # local absolute path to the plugin dir

@runtime_checkable
class MarketplaceResolver(Protocol):
    async def resolve(self, entries: list[PluginRef]) -> list[ResolvedPlugin]: ...
```

`PluginRef` is the parsed `jean.json` entry `(marketplace, plugin, ref)`.

### Adapter — `GitMarketplaceResolver` (`src/jean/plugins/git_resolver.py`)

- Dedupes entries by `(marketplace, ref)`, shallow-clones each unique repo once
  into a cache dir (`JEAN_MARKETPLACE_CACHE_DIR`, default `<home>/marketplaces`),
  keyed by a hash of `(marketplace, ref)`. A resolved `(marketplace, ref)` clone
  is cached on disk and is **not** refreshed on later boots, so operators should
  pin `ref` to an immutable tag or commit SHA for deterministic rollouts.
- Rewrites `git@github.com:ORG/repo.git` (and `https://github.com/ORG/repo.git`)
  to `https://x-access-token:${JEAN_MARKETPLACE_TOKEN}@github.com/ORG/repo.git`.
  The token is **never** logged and never written into the cache path.
- For each entry: `path = <clone>/plugins/<plugin>`. Validates the dir exists and
  the plugin is listed in the repo's `.claude-plugin/marketplace.json`.
- **Fails loudly** (raises) on clone/auth/missing-plugin errors — a jailbroken or
  typo'd manifest must not silently degrade jean's toolset. Startup aborts.

### Domain — `PluginManifest` + `McpConfig` loaders (`src/jean/plugins/manifest.py`)

- `load_plugin_manifest(path) -> list[PluginRef]` — parse/validate `jean.json`.
  Missing file → empty list (plugins optional). Malformed → raise.
- `load_mcp_config(path) -> dict[str, McpServerConfig]` — parse `mcp.json`'s
  `mcpServers` map. Missing file → empty dict.

### Composition root — `server.py`

The only place that constructs the git adapter and wires results into
`options_factory`:

```python
resolver = GitMarketplaceResolver(token=settings.marketplace_token, cache_dir=…)
plugins  = await resolver.resolve(load_plugin_manifest(settings.plugins_path))
extra_mcp = load_mcp_config(settings.mcp_config_path)

def options_factory(resume):
    return ClaudeAgentOptions(
        system_prompt=compose_system_prompt(persona_text),
        mcp_servers={"jean_slack": server_mcp, **extra_mcp},
        plugins=[{"type": "local", "path": p.path} for p in plugins],
        skills="all",                    # enable plugin-provided skills
        allowed_tools=tool_names + external_tool_patterns,
        strict_mcp_config=False,
        …
    )
```

## Config additions (`JEAN_*`, pydantic-settings)

| Setting | Default | Purpose |
|---|---|---|
| `JEAN_IDENTITY_PATH` | `<home>/IDENTITY.md` | soul file path (mount `soul.md` here) |
| `JEAN_MCP_CONFIG_PATH` | `<home>/mcp.json` | extra MCP servers (optional) |
| `JEAN_PLUGINS_PATH` | `<home>/jean.json` | plugin manifest (optional) |
| `JEAN_MARKETPLACE_CACHE_DIR` | `<home>/marketplaces` | clone cache (writable volume) |
| `JEAN_MARKETPLACE_TOKEN` | `None` | GitHub HTTPS token from Vault; required only if a manifest names a private marketplace |

`JEAN_HOME` stays a **writable** dir (emptyDir in k8s) for cache/workspaces; the
soul/config files are mounted read-only from the Secret at their own paths, so we
keep them out of `JEAN_HOME` via the explicit path settings above.

## Open risk to resolve during implementation (spike)

`allowed_tools` interplay: plugin/external MCP tools must be reachable. jean runs
`permission_mode=bypassPermissions` with a `can_use_tool` that allows all, but
`allowed_tools` is an exposure list. The spike: confirm whether enabling
`skills="all"` + `plugins=[…]` surfaces the plugin MCP tools automatically, or
whether we must add `mcp__<server>__*` patterns to `allowed_tools`. Resolve with
a throwaway harness against a real plugin before finalizing the wiring.

## Testing (TDD, fakes at the port — no live git/network)

- `GitMarketplaceResolver`: inject a fake "git" runner (a callable) so tests
  assert clone/dedupe/URL-rewrite/validation without touching the network. Assert
  the token never appears in the cache path and a missing plugin raises.
- `load_plugin_manifest` / `load_mcp_config`: parse valid + malformed + missing
  files (tmp_path).
- `options_factory` wiring: a unit test asserting the merged `mcp_servers` keeps
  `jean_slack` and adds externals, and `plugins` reflects resolved paths — using
  fakes, no SDK client.
- Domain tests use a fake `MarketplaceResolver`; the git adapter is exercised in
  isolation.

## Sub-project B (outline — separate spec, flux-infra repo)

`apps/admin-v2/jean/`:
- `vaultstaticsecret.yaml` — `secrets.hashicorp.com/v1beta1`, `mount: infra`,
  `type: kv-v2`, `path: admin-v2/jean`, `destination.create: true`. Vault KV keys:
  `slack_bot_token`, `slack_app_token`, `anthropic_api_key`|`claude_code_oauth_token`,
  `database_url`, `marketplace_token`, and file blobs `soul.md`, `jean.json`,
  `mcp.json`.
- `deployment.yaml` — no ingress (Socket Mode is outbound). `envFrom` the Secret;
  mount the file keys at `JEAN_IDENTITY_PATH`/`JEAN_PLUGINS_PATH`/`JEAN_MCP_CONFIG_PATH`;
  emptyDir for `JEAN_HOME`. Health probe on `JEAN_HEALTH_PORT`.
- `kustomization.yaml` (jean) and `apps/admin-v2/kustomization.yaml` (aggregate).
- `clusters/admin-v2/flux-system/custom-components.yaml` — add a Flux
  `Kustomization` `apps-admin-v2` (`path: ./apps/admin-v2`, `prune: true`,
  `dependsOn` the existing `apps-tools-admin-v2` so VSO is ready first).

## Sequencing

A (jean code) → build & push image → seed Vault KV → B (flux manifests). Building
B first would scaffold against an image that can't consume any of it.
