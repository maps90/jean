from __future__ import annotations

from typing import Any

from jean.plugins.mcp_proxy import build_proxy_servers, build_proxy_tools, proxy_tool_patterns


class FakeClient:
    """Stands in for a started McpClient (its `tools` are already fetched)."""

    def __init__(self, name: str, tools: list[dict[str, Any]]) -> None:
        self.name = name
        self.tools = tools
        self.calls: list[tuple[str, dict]] = []

    async def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((tool, arguments))
        return {"content": [{"type": "text", "text": f"{tool} ok"}]}


def _client(name: str = "plugin_kubectl_kubernetes") -> FakeClient:
    return FakeClient(
        name,
        [
            {
                "name": "pods_list",
                "description": "List pods",
                "inputSchema": {"type": "object", "properties": {"ns": {"type": "string"}}},
            },
            {"name": "pods_log", "description": "Read a pod log"},
        ],
    )


def test_upstream_name_description_and_schema_are_forwarded_verbatim():
    tools = build_proxy_tools(_client())

    assert [t.name for t in tools] == ["pods_list", "pods_log"]
    assert tools[0].description == "List pods"
    assert tools[0].input_schema["properties"]["ns"]["type"] == "string"


def test_a_tool_without_a_schema_still_gets_a_valid_one():
    tools = build_proxy_tools(_client())
    assert tools[1].input_schema == {"type": "object", "properties": {}}


async def test_calling_a_proxied_tool_reaches_the_upstream_client():
    client = _client()
    tools = build_proxy_tools(client)

    result = await tools[0].handler({"ns": "devops"})

    assert client.calls == [("pods_list", {"ns": "devops"})]
    assert result["content"][0]["text"] == "pods_list ok"


async def test_each_tool_calls_its_own_upstream_tool_not_the_last_one_in_the_loop():
    """A closure over the loop variable would make every proxied tool invoke
    whichever tool the loop ended on -- 65 grafana tools all calling one."""
    client = _client()
    tools = build_proxy_tools(client)

    await tools[0].handler({})
    await tools[1].handler({})

    assert [name for name, _ in client.calls] == ["pods_list", "pods_log"]


def test_the_agent_facing_tool_ids_are_unchanged_by_proxying():
    """The CLI named a plugin's server `plugin:kubectl:kubernetes` and served
    `mcp__plugin_kubectl_kubernetes__pods_list`. Keeping the key identical means
    every skill and prompt that already names that tool keeps working."""
    servers = build_proxy_servers([_client(), _client("kubernetes")])

    assert set(servers) == {"plugin_kubectl_kubernetes", "kubernetes"}
    assert proxy_tool_patterns(servers) == [
        "mcp__plugin_kubectl_kubernetes__*",
        "mcp__kubernetes__*",
    ]
