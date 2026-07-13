from __future__ import annotations

import os
import re
from typing import Any

# `${VAR}` and `${VAR:-default}` inside an MCP server config. The CLI expands both
# in a .mcp.json it reads itself, so jean must speak the same dialect for the
# configs it hands over -- and jean ships its servers to the CLI as an inline
# `--mcp-config` blob, not as a file on disk, so betting a bearer token on the CLI
# doing it for us is not a bet worth having.
#
# The `:-default` half is not a nicety: plugin authors write
# `${PORTICO_URL:-https://portico.int.okadoc.net}` so their server works unset.
# A reader that only understands `${VAR}` leaves that whole string untouched --
# no substitution, and no error either, because nothing matched -- and hands the
# CLI a URL with a literal `${...}` in it.
ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class MissingEnvVar(Exception):
    """A remote MCP config referenced an env var that is not set."""


def expand(value: str) -> str:
    """Lenient: an unset var falls back to its `:-default`, or to "". Used for a
    *stdio* server's env block.

    Survivable there because a stdio server starved of a variable dies at spawn,
    loudly, with its stderr captured (mcp_stdio.stderr_tail). A remote server has
    no such moment -- see expand_config.
    """
    return ENV_REF.sub(lambda m: os.environ.get(m.group(1)) or (m.group(2) or ""), value)


def expand_config(config: dict[str, Any], *, server: str) -> dict[str, Any]:
    """Strict: every `${VAR}` in a remote server's config, or refuse to boot.

    Blanking an unset var here is the worst option available. `Authorization:
    Bearer ${PORTICO_ACCESS_TOKEN}` with the var unset becomes `Bearer `, jean
    boots clean, and every call to the server 401s with nothing in the logs
    saying why -- at which point the agent, finding its tools broken, falls back
    to curl-ing the endpoint through Bash, which is one approval click per call.
    That is the exact failure this whole change exists to remove. A credential
    misconfiguration is a deploy-time error; raise it on the deploy that caused
    it, the way a malformed JEAN_APPROVERS already does (config.py).

    Returns a new config; the caller's dict is never mutated.
    """
    return {key: _expand_value(value, server=server, path=key) for key, value in config.items()}


def _expand_value(value: Any, *, server: str, path: str) -> Any:
    if isinstance(value, str):
        return _expand_strict(value, server=server, path=path)
    if isinstance(value, dict):
        return {k: _expand_value(v, server=server, path=f"{path}.{k}") for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_value(v, server=server, path=f"{path}[{i}]") for i, v in enumerate(value)]
    return value


def _expand_strict(value: str, *, server: str, path: str) -> str:
    def replace(match: re.Match[str]) -> str:
        name, default = match.group(1), match.group(2)
        # POSIX `:-` semantics, which is what the CLI implements: an empty value
        # counts as unset, so a var exported blank still falls back to the default.
        supplied = os.environ.get(name)
        if supplied:
            return supplied
        if default is not None:
            # `${VAR:-}` is an author saying "empty is fine here", out loud. Only a
            # bare `${VAR}` -- which says nothing -- is worth refusing to boot over.
            return default
        raise MissingEnvVar(
            f"MCP server {server!r}: {path} references ${{{name}}}, which is unset or empty. "
            f"Set {name} in the environment, give it a default (${{{name}:-…}}), "
            f"or drop the reference."
        )

    return ENV_REF.sub(replace, value)
