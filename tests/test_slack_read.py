from __future__ import annotations

import pytest

from jean.ports import ChatReadError
from jean.slack.client import SlackSurface


class FakeWeb:
    """Stands in for AsyncWebClient. Records the kwargs each call received so
    tests assert what actually reaches Slack, not that a mock was called."""

    def __init__(self, *, pages=None, history=None, replies=None):
        self._pages = pages or [{"channels": [], "response_metadata": {}}]
        self._history = history or {"messages": [], "has_more": False}
        self._replies = replies or {"messages": [], "has_more": False}
        self.list_calls: list[dict] = []
        self.history_calls: list[dict] = []
        self.replies_calls: list[dict] = []

    async def conversations_list(self, **kwargs):
        self.list_calls.append(kwargs)
        cursor = kwargs.get("cursor") or ""
        index = 0 if not cursor else int(cursor)
        return self._pages[index]

    async def conversations_history(self, **kwargs):
        self.history_calls.append(kwargs)
        return self._history

    async def conversations_replies(self, **kwargs):
        self.replies_calls.append(kwargs)
        return self._replies


def _pages(*groups):
    """Build conversations_list pages; every page but the last hands on a cursor."""
    out = []
    for i, group in enumerate(groups):
        last = i == len(groups) - 1
        out.append(
            {
                "channels": group,
                "response_metadata": {} if last else {"next_cursor": str(i + 1)},
            }
        )
    return out


async def test_channel_id_passes_through_without_an_api_call():
    web = FakeWeb()
    surface = SlackSurface(web)
    assert await surface.resolve_channel("C0123ABC") == "C0123ABC"
    assert web.list_calls == []


async def test_hash_prefixed_name_resolves_to_id():
    web = FakeWeb(pages=_pages([{"id": "C999", "name": "sre-support"}]))
    surface = SlackSurface(web)
    assert await surface.resolve_channel("#sre-support") == "C999"


async def test_bare_name_resolves_to_id():
    web = FakeWeb(pages=_pages([{"id": "C999", "name": "sre-support"}]))
    surface = SlackSurface(web)
    assert await surface.resolve_channel("sre-support") == "C999"


async def test_resolution_pages_until_it_finds_the_channel():
    web = FakeWeb(
        pages=_pages(
            [{"id": "C1", "name": "general"}],
            [{"id": "C999", "name": "sre-support"}],
        )
    )
    surface = SlackSurface(web)
    assert await surface.resolve_channel("sre-support") == "C999"
    assert len(web.list_calls) == 2
    assert web.list_calls[0]["types"] == "public_channel"


async def test_resolution_is_cached_so_a_second_read_does_not_rescan():
    web = FakeWeb(pages=_pages([{"id": "C999", "name": "sre-support"}]))
    surface = SlackSurface(web)
    await surface.resolve_channel("sre-support")
    await surface.resolve_channel("sre-support")
    assert len(web.list_calls) == 1


async def test_unknown_channel_raises_rather_than_returning_empty():
    """'no such channel' and 'the channel was empty' are different answers and
    the agent must never conflate them."""
    web = FakeWeb(pages=_pages([{"id": "C1", "name": "general"}]))
    surface = SlackSurface(web)
    with pytest.raises(ChatReadError) as exc:
        await surface.resolve_channel("sre-support")
    assert exc.value.code == "channel_not_found"
    assert "sre-support" in str(exc.value)
