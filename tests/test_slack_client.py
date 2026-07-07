from __future__ import annotations

from jean.slack.client import SlackSurface


class FakeWebClient:
    def __init__(self, *, raise_on_status=False):
        self.posted: list[dict] = []
        self.updated: list[dict] = []
        self.uploaded: list[dict] = []
        self.reacted: list[dict] = []
        self.unreacted: list[dict] = []
        self.statuses: list[dict] = []
        self._raise_on_status = raise_on_status
        self._ts_counter = 0

    async def chat_postMessage(self, **kwargs):
        self._ts_counter += 1
        self.posted.append(kwargs)
        return {"ts": f"100.{self._ts_counter}"}

    async def chat_update(self, **kwargs):
        self.updated.append(kwargs)
        return {"ok": True}

    async def files_upload_v2(self, **kwargs):
        self.uploaded.append(kwargs)
        return {"ok": True}

    async def reactions_add(self, **kwargs):
        self.reacted.append(kwargs)
        return {"ok": True}

    async def reactions_remove(self, **kwargs):
        self.unreacted.append(kwargs)
        return {"ok": True}

    async def assistant_threads_setStatus(self, **kwargs):
        self.statuses.append(kwargs)
        if self._raise_on_status:
            raise RuntimeError("missing assistant:write scope")
        return {"ok": True}


async def test_reply_converts_markdown_and_returns_first_ts():
    client = FakeWebClient()
    surface = SlackSurface(client)
    ts = await surface.reply("C1", "111.0", "**bold** text")
    assert ts == "100.1"
    assert len(client.posted) == 1
    assert client.posted[0]["channel"] == "C1"
    assert client.posted[0]["thread_ts"] == "111.0"
    assert client.posted[0]["text"] == "*bold* text"


async def test_reply_chunks_long_text_into_multiple_messages():
    client = FakeWebClient()
    surface = SlackSurface(client)
    long_text = "x" * 39100  # over the default 39000-char chunk_text limit
    ts = await surface.reply("C1", "111.0", long_text)
    assert len(client.posted) > 1
    assert ts == "100.1"


async def test_reply_short_text_is_a_single_message():
    client = FakeWebClient()
    surface = SlackSurface(client)
    ts = await surface.reply("C1", "111.0", "x" * 100)
    assert len(client.posted) == 1
    assert ts == "100.1"


async def test_edit_converts_markdown():
    client = FakeWebClient()
    surface = SlackSurface(client)
    await surface.edit("C1", "111.0", "*already mrkdwn* and **bold**")
    assert client.updated[0]["channel"] == "C1"
    assert client.updated[0]["ts"] == "111.0"
    assert client.updated[0]["text"] == "_already mrkdwn_ and *bold*"


async def test_upload_with_path():
    client = FakeWebClient()
    surface = SlackSurface(client)
    await surface.upload("C1", "111.0", path="/tmp/x.txt", filename="x.txt", title="X")
    call = client.uploaded[0]
    assert call["channel"] == "C1"
    assert call["thread_ts"] == "111.0"
    assert call["file"] == "/tmp/x.txt"
    assert call["filename"] == "x.txt"
    assert call["title"] == "X"


async def test_upload_with_content():
    client = FakeWebClient()
    surface = SlackSurface(client)
    await surface.upload("C1", "111.0", content="hello", filename="x.txt", comment="here")
    call = client.uploaded[0]
    assert call["content"] == "hello"
    assert call["initial_comment"] == "here"


async def test_react_uses_name_and_timestamp():
    client = FakeWebClient()
    surface = SlackSurface(client)
    await surface.react("C1", "111.0", ":thumbsup:")
    assert client.reacted[0] == {"channel": "C1", "name": "thumbsup", "timestamp": "111.0"}


async def test_unreact_uses_name_and_timestamp():
    client = FakeWebClient()
    surface = SlackSurface(client)
    await surface.unreact("C1", "111.0", "thumbsup")
    assert client.unreacted[0] == {"channel": "C1", "name": "thumbsup", "timestamp": "111.0"}


async def test_set_status_calls_channel_id_and_thread_ts():
    client = FakeWebClient()
    surface = SlackSurface(client)
    await surface.set_status("C1", "111.0", "is thinking...")
    assert client.statuses[0] == {
        "channel_id": "C1",
        "thread_ts": "111.0",
        "status": "is thinking...",
    }


async def test_set_status_swallows_errors():
    client = FakeWebClient(raise_on_status=True)
    surface = SlackSurface(client)
    await surface.set_status("C1", "111.0", "is thinking...")  # must not raise
    assert len(client.statuses) == 1
