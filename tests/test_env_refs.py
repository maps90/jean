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


def test_expansion_does_not_mutate_the_config_it_was_given(monkeypatch):
    monkeypatch.setenv("TOK", "t0k")
    config = {"headers": {"Authorization": "Bearer ${TOK}"}}

    expand_config(config, server="portico")

    assert config["headers"]["Authorization"] == "Bearer ${TOK}"
