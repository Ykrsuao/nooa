# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""MCP result projection and nullable OAuth configuration contracts."""

from contextlib import asynccontextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from mcp.types import CallToolResult

from nooa.mcp import oauth
from nooa.mcp import tool as tool_module
from nooa.mcp.tool import MCPManager, MCPTool


def _client(result):
    session = SimpleNamespace(call_tool=AsyncMock(return_value=result), closed=False)

    @asynccontextmanager
    async def connect():
        try:
            yield session
        finally:
            session.closed = True

    return SimpleNamespace(connect_to_server=connect), session


@pytest.mark.parametrize("text", ["answer", ""])
async def test_mixed_sdk_content_returns_first_text_and_closes_session(text):
    result = CallToolResult.model_validate(
        {
            "content": [
                {"type": "image", "data": "AA==", "mimeType": "image/png"},
                {"type": "text", "text": text},
                {"type": "text", "text": "later"},
            ]
        }
    )
    client, session = _client(result)
    assert await MCPTool(client, "test")._call_tool("example", {"x": 0, "omit": None}) == text
    session.call_tool.assert_awaited_once_with("example", {"x": 0})
    assert session.closed


@pytest.mark.parametrize("kind", ["image", "audio", "resource_link", "resource", "empty"])
async def test_nontext_sdk_content_remains_structured(kind):
    content = {
        "image": {"type": "image", "data": "AA==", "mimeType": "image/png"},
        "audio": {"type": "audio", "data": "AA==", "mimeType": "audio/wav"},
        "resource_link": {
            "type": "resource_link",
            "uri": "https://example.test/data",
            "name": "data",
        },
        "resource": {
            "type": "resource",
            "resource": {"uri": "https://example.test/data", "text": "nested"},
        },
    }
    result = CallToolResult.model_validate({"content": [] if kind == "empty" else [content[kind]]})
    client, _ = _client(result)
    value = await MCPTool(client, "test")._invoke("example", {})
    assert value is (result if kind == "empty" else result.content)


async def test_legacy_duck_typed_text_none_is_not_treated_as_absent():
    result = SimpleNamespace(content=[SimpleNamespace(text=None), SimpleNamespace(text="later")])
    client, _ = _client(result)
    assert await MCPTool(client, "test")._invoke("example", {}) is None


@pytest.mark.parametrize("transport", [None, "", "invalid"])
async def test_invalid_refresh_transport_does_not_start_oauth(monkeypatch, transport):
    refresh = AsyncMock()
    factory = Mock()
    monkeypatch.setattr(oauth, "handle_mcp_oauth", refresh)
    monkeypatch.setattr(tool_module, "create_mcp_client", factory)
    client = object()
    tool = MCPTool(
        client, "test", {"server_url": "https://example.test/mcp", "transport": transport}
    )
    assert await tool._refresh_access_token() is False
    assert tool._client is client
    refresh.assert_not_awaited()
    factory.assert_not_called()


@pytest.mark.parametrize("transport", ["stdio", "sse", "streamable-http"])
async def test_refresh_preserves_transport_timeout_headers_and_unattended_mode(
    monkeypatch, transport
):
    refresh = AsyncMock(
        return_value=SimpleNamespace(token_type="Bearer", access_token="test-token")
    )
    factory = Mock(return_value=object())
    monkeypatch.setattr(oauth, "handle_mcp_oauth", refresh)
    monkeypatch.setattr(tool_module, "create_mcp_client", factory)
    headers = {"X-Test": "preserved", "Authorization": "old"}
    timeout = timedelta(seconds=125)
    tool = MCPTool(
        object(),
        "test",
        {
            "server_url": "https://example.test/mcp",
            "transport": transport,
            "redirect_uri": None,
            "headers": headers,
            "tool_call_timeout": timeout,
            "command": "test-server",
            "args": ["arg"],
            "env": {"TEST": "value"},
        },
    )
    assert await tool._refresh_access_token() is True
    assert tool._client is factory.return_value
    refresh.assert_awaited_once_with(
        server_url="https://example.test/mcp",
        redirect_uri="http://localhost:0/callback",
        client_id=None,
        scope=None,
        open_browser=False,
        manual=False,
        use_cache=True,
    )
    factory.assert_called_once_with(
        transport=transport,
        url="https://example.test/mcp",
        command="test-server",
        args=["arg"],
        env={"TEST": "value"},
        headers={"X-Test": "preserved", "Authorization": "Bearer test-token"},
        tool_call_timeout=timeout,
    )
    assert headers["Authorization"] == "old"


@pytest.mark.parametrize(
    "configured,explicit,expected",
    [
        (None, None, "http://127.0.0.1:0/callback"),
        ("", None, "http://127.0.0.1:0/callback"),
        ("http://localhost:1234/callback", None, "http://localhost:1234/callback"),
        (
            "http://localhost:1234/callback",
            "http://localhost:4321/callback",
            "http://localhost:4321/callback",
        ),
    ],
)
def test_factory_oauth_redirect_fallback_and_precedence(
    monkeypatch, tmp_path, configured, explicit, expected
):
    attempts = 0

    @asynccontextmanager
    async def connect():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            request = httpx.Request("GET", "https://example.test/mcp")
            response = httpx.Response(401, request=request)
            raise httpx.HTTPStatusError("expired", request=request, response=response)
        yield SimpleNamespace(list_tools=AsyncMock(return_value=SimpleNamespace(tools=[])))

    monkeypatch.setattr(
        tool_module,
        "create_mcp_client",
        Mock(return_value=SimpleNamespace(connect_to_server=connect)),
    )
    refresh = AsyncMock(
        return_value=SimpleNamespace(token_type="Bearer", access_token="test-token")
    )
    monkeypatch.setattr(tool_module, "handle_mcp_oauth", refresh)
    result = MCPManager.create_from_server(
        "test",
        transport="streamable-http",
        url="https://example.test/mcp",
        oauth_redirect_uri=explicit,
        mcp_file=tmp_path / "missing.json",
        servers={"test": {"oauth_redirect_uri": configured}},
    )
    assert attempts == 2
    assert refresh.await_args is not None
    assert refresh.await_args.kwargs["redirect_uri"] == expected
    assert result._refresh_ctx["redirect_uri"] == expected
