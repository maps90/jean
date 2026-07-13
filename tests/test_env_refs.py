from __future__ import annotations

import pytest

from jean.plugins.env_refs import MissingEnvVar, expand, expand_config


def test_lenient_expand_blanks_an_unset_var(monkeypatch):
    """stdio's behaviour, preserved verbatim: a stdio server that loses a var
    dies visibly at spawn, so blanking it there is survivable."""
    monkeypatch.delenv("JEAN_TEST_ABSENT", raising=False)
    monkeypatch.setenv("JEAN_TEST_URL", "https://es.internal")

    assert expand("${JEAN_TEST_URL}/health") == "https://es.internal/health"
    assert expand("x${JEAN_TEST_ABSENT}y") == "xy"


def test_a_remote_servers_credential_comes_from_the_environment(monkeypatch):
    """The whole point: the token lives in the env (Vault -> env, like every
    other jean secret), not as a second copy inside the mounted mcp.json."""
    monkeypatch.setenv("PORTICO_ACCESS_TOKEN", "sekrit")

    expanded = expand_config(
        {
            "type": "http",
            "url": "https://portico.int.okadoc.net/mcp",
            "headers": {"Authorization": "Bearer ${PORTICO_ACCESS_TOKEN}"},
        },
        server="portico",
    )

    assert expanded["headers"]["Authorization"] == "Bearer sekrit"
    assert expanded["url"] == "https://portico.int.okadoc.net/mcp"


def test_expansion_reaches_every_nested_string(monkeypatch):
    monkeypatch.setenv("HOST", "portico.int")
    monkeypatch.setenv("TOK", "t0k")

    expanded = expand_config(
        {
            "url": "https://${HOST}/mcp",
            "headers": {"Authorization": "Bearer ${TOK}"},
            "args": ["--host", "${HOST}"],
            "timeout": 30,
            "insecure": False,
        },
        server="portico",
    )

    assert expanded == {
        "url": "https://portico.int/mcp",
        "headers": {"Authorization": "Bearer t0k"},
        "args": ["--host", "portico.int"],
        "timeout": 30,
        "insecure": False,
    }


def test_a_config_without_references_is_returned_unchanged():
    config = {"type": "http", "url": "https://x", "headers": {"A": "b"}}

    assert expand_config(config, server="remote") == config


def test_an_unset_var_in_a_remote_config_fails_loudly(monkeypatch):
    """Blanking this one would send `Authorization: Bearer ` -- jean boots clean,
    every call 401s, nothing in the logs says why, and the agent falls back to
    curl (i.e. straight back to the click storm). Refuse to boot instead."""
    monkeypatch.delenv("PORTICO_ACCESS_TOKEN", raising=False)

    with pytest.raises(MissingEnvVar) as exc:
        expand_config(
            {"headers": {"Authorization": "Bearer ${PORTICO_ACCESS_TOKEN}"}},
            server="portico",
        )

    # The message has to name both, or the operator is left grepping.
    assert "PORTICO_ACCESS_TOKEN" in str(exc.value)
    assert "portico" in str(exc.value)


def test_a_default_fills_in_for_an_unset_var(monkeypatch):
    """`${VAR:-fallback}` is the syntax the Claude CLI expands in a .mcp.json, so
    plugin authors write it. jean must read the same dialect or it hands the CLI a
    URL with a literal `${...}` in it."""
    monkeypatch.delenv("PORTICO_URL", raising=False)

    expanded = expand_config(
        {"url": "${PORTICO_URL:-https://portico.int.okadoc.net}/mcp"}, server="portico"
    )

    assert expanded["url"] == "https://portico.int.okadoc.net/mcp"


def test_a_set_var_beats_its_default(monkeypatch):
    monkeypatch.setenv("PORTICO_URL", "https://portico.staging")

    expanded = expand_config(
        {"url": "${PORTICO_URL:-https://portico.int.okadoc.net}/mcp"}, server="portico"
    )

    assert expanded["url"] == "https://portico.staging/mcp"


def test_a_default_means_there_is_nothing_to_fail_about(monkeypatch):
    """Strictness is about a credential nobody supplied. A var *with* a default was
    supplied -- by the author -- so it must not raise."""
    monkeypatch.delenv("PORTICO_URL", raising=False)

    assert expand_config({"url": "${PORTICO_URL:-https://x}"}, server="p") == {"url": "https://x"}


def test_an_empty_default_is_still_a_default(monkeypatch):
    """`${VAR:-}` says "empty is fine here" out loud. Distinct from a bare `${VAR}`,
    which says nothing and must therefore raise."""
    monkeypatch.delenv("OPTIONAL_THING", raising=False)

    assert expand_config({"x": "a${OPTIONAL_THING:-}b"}, server="p") == {"x": "ab"}


def test_lenient_expand_honours_defaults_too(monkeypatch):
    monkeypatch.delenv("ES_URL", raising=False)

    assert expand("${ES_URL:-http://localhost:9200}") == "http://localhost:9200"


def test_a_var_exported_empty_is_treated_as_unset(monkeypatch):
    """`PORTICO_ACCESS_TOKEN=""` is the silent-401 trap wearing a disguise: the var
    exists, so a naive reader substitutes it and sends `Bearer `. POSIX `:-` counts
    empty as unset and so do we -- refuse, rather than ship an empty credential."""
    monkeypatch.setenv("PORTICO_ACCESS_TOKEN", "")

    with pytest.raises(MissingEnvVar, match="unset or empty"):
        expand_config(
            {"headers": {"Authorization": "Bearer ${PORTICO_ACCESS_TOKEN}"}}, server="portico"
        )


def test_an_empty_var_falls_back_to_its_default(monkeypatch):
    monkeypatch.setenv("PORTICO_URL", "")

    assert expand_config({"url": "${PORTICO_URL:-https://fallback}"}, server="p") == {
        "url": "https://fallback"
    }


def test_expansion_does_not_mutate_the_config_it_was_given(monkeypatch):
    monkeypatch.setenv("TOK", "t0k")
    config = {"headers": {"Authorization": "Bearer ${TOK}"}}

    expand_config(config, server="portico")

    assert config["headers"]["Authorization"] == "Bearer ${TOK}"
