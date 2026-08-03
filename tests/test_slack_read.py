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


def _raw(ts, text, user="U1", **extra):
    return {"ts": ts, "text": text, "user": user, **extra}


async def test_history_returns_oldest_first():
    """Slack hands back newest-first. The renderer reads top-to-bottom as a
    conversation, so the adapter is where the order gets fixed."""
    web = FakeWeb(
        history={
            "messages": [_raw("1754213640.0", "second"), _raw("1754210040.0", "first")],
            "has_more": False,
        }
    )
    messages, has_more = await SlackSurface(web).history("C1")
    assert [m.text for m in messages] == ["first", "second"]
    assert has_more is False


async def test_history_passes_window_and_limit_as_slack_expects():
    web = FakeWeb()
    await SlackSurface(web).history("C1", oldest=1754210040.0, latest=1754296440.0, limit=25)
    call = web.history_calls[0]
    assert call["channel"] == "C1"
    assert call["oldest"] == "1754210040.000000"
    assert call["latest"] == "1754296440.000000"
    assert call["limit"] == 25


async def test_history_omits_bounds_that_were_not_given():
    web = FakeWeb()
    await SlackSurface(web).history("C1")
    assert "oldest" not in web.history_calls[0]
    assert "latest" not in web.history_calls[0]


async def test_history_clamps_limit_to_the_cap():
    web = FakeWeb()
    await SlackSurface(web).history("C1", limit=9999)
    assert web.history_calls[0]["limit"] == 200


async def test_history_carries_reply_count_and_thread_ts():
    web = FakeWeb(
        history={
            "messages": [_raw("1754210040.0", "parent", thread_ts="1754210040.0", reply_count=4)],
            "has_more": False,
        }
    )
    messages, _ = await SlackSurface(web).history("C1")
    assert messages[0].reply_count == 4
    assert messages[0].thread_ts == "1754210040.0"


async def test_history_reports_has_more():
    web = FakeWeb(history={"messages": [_raw("1754210040.0", "x")], "has_more": True})
    _messages, has_more = await SlackSurface(web).history("C1")
    assert has_more is True


async def test_bot_message_falls_back_to_bot_id_for_the_author():
    web = FakeWeb(
        history={
            "messages": [{"ts": "1754210040.0", "text": "alert", "bot_id": "B7"}],
            "has_more": False,
        }
    )
    messages, _ = await SlackSurface(web).history("C1")
    assert messages[0].user == "B7"


async def test_replies_keep_slack_order_parent_first():
    """conversations.replies is ALREADY oldest-first with the parent leading --
    reversing it here would put the parent last and misread the thread."""
    web = FakeWeb(
        replies={
            "messages": [_raw("1754210040.0", "parent"), _raw("1754210100.0", "answer")],
            "has_more": False,
        }
    )
    messages, _ = await SlackSurface(web).replies("C1", "1754210040.0")
    assert [m.text for m in messages] == ["parent", "answer"]


async def test_replies_passes_ts_and_limit():
    web = FakeWeb()
    await SlackSurface(web).replies("C1", "1754210040.0", limit=10)
    call = web.replies_calls[0]
    assert call["channel"] == "C1"
    assert call["ts"] == "1754210040.0"
    assert call["limit"] == 10


async def test_slack_error_becomes_a_chat_read_error_carrying_the_code():
    from slack_sdk.errors import SlackApiError

    class Refusing(FakeWeb):
        async def conversations_history(self, **kwargs):
            raise SlackApiError("nope", {"ok": False, "error": "not_in_channel"})

    with pytest.raises(ChatReadError) as exc:
        await SlackSurface(Refusing()).history("C1")
    assert exc.value.code == "not_in_channel"
