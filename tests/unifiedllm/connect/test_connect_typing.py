# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Typed Connect evidence, diagnostics and owned-client lifecycle contracts."""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import aclosing
from typing import Any, assert_type
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from litellm.exceptions import InternalServerError

from nooa.unifiedllm import FakeLLMClient, UnifiedLLM, connect, registry
from nooa.unifiedllm.connect._diagnostics import scrub_report
from nooa.unifiedllm.connect._records import ProbeRecord, public_record
from nooa.unifiedllm.connect._session import session_steps
from tests.unifiedllm.connect.connect_http import mock_http, response_body


def _plan() -> connect.ConnectPlan:
    return connect.plan("test", "gpt-5.1", "chat", "https://api.test/v1", "", reply_tokens=2048)


def test_record_preserves_unknown_evidence_and_filters_internal_fields() -> None:
    record: ProbeRecord = {
        "outcome": "not_confirmed",
        "state_retained": None,
        "settings_retained": None,
        "settings_sent": None,
        "source": "connect",
        "include_rejected": True,
        "request": {"private": "not-for-diagnostics"},
    }
    assert public_record(record) == {
        "outcome": "not_confirmed",
        "state_retained": None,
        "settings_retained": None,
        "settings_sent": None,
    }
    assert record.get("request") == {"private": "not-for-diagnostics"}


def test_scrubber_preserves_typed_container_shapes_and_detaches_values() -> None:
    secret = "test-only-secret"
    text = assert_type(scrub_report(f"value={secret}", api_key=secret), str)
    original: dict[str, Any] = {secret: (secret, {"value": secret}), "count": 2}
    report = assert_type(scrub_report(original, api_key=secret), dict[str, Any])
    array = assert_type(scrub_report((secret, None, True), api_key=secret), list[Any])

    assert text == "value=[redacted]"
    assert report == {"[redacted]": ["[redacted]", {"value": "[redacted]"}], "count": 2}
    assert array == ["[redacted]", None, True]
    assert original[secret][1] == {"value": secret}


async def test_progress_generators_are_closeable_without_dispatch() -> None:
    proposal = _plan()
    probes = assert_type(
        connect.run_steps(proposal, approved="none"),
        AsyncGenerator[connect.ProbeUpdate | connect.ConnectResult, None],
    )
    interfaces = assert_type(
        connect.check_interfaces("test", "model", "https://api.test/v1", ""),
        AsyncGenerator[connect.ProbeUpdate | connect.InterfaceResult, None],
    )
    sessions = assert_type(
        session_steps("test", proposal.entry, api_key=None, budget_tokens=0),
        AsyncGenerator[connect.ProbeUpdate, None],
    )
    async with aclosing(probes), aclosing(interfaces), aclosing(sessions):
        pass


@pytest.mark.parametrize("session", [False, True])
async def test_missing_http_transport_closes_client_without_dispatch(
    monkeypatch: pytest.MonkeyPatch, session: bool
) -> None:
    client = FakeLLMClient()
    close = AsyncMock(wraps=client.aclose)
    call = AsyncMock(side_effect=AssertionError("No uninstrumented call is approved"))
    monkeypatch.setattr(client, "aclose", close)
    monkeypatch.setattr(client, "acall", call)
    monkeypatch.setattr(registry, "client_from_config", lambda *args, **kwargs: client)
    proposal = _plan()

    if session:
        updates = [
            update
            async for update in session_steps(
                "test", proposal.entry, api_key=None, budget_tokens=100000
            )
        ]
        assert len(updates) == 1
        assert updates[0].name == "session"
        assert updates[0].outcome.get("outcome") == "not_confirmed"
    else:
        with pytest.raises(RuntimeError, match="HTTP"):
            await connect._run_probe("test", proposal.entry, proposal.probes[0], None)
    call.assert_not_awaited()
    close.assert_awaited_once()


@pytest.mark.parametrize("mode", ["success", "error", "cancel"])
async def test_probe_removes_owned_hooks_and_closes_real_client(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    created: list[UnifiedLLM] = []
    requests: list[httpx.Request] = []
    factory = registry.client_from_config
    # The real SDK validates credentials before dispatching to MockTransport.
    # Supply an inert key so this lifecycle test also works on clean CI hosts.
    api_key = "connect-test-key"
    network_sync = Mock(side_effect=AssertionError("Real HTTP is not approved"))
    network_async = AsyncMock(side_effect=AssertionError("Real HTTP is not approved"))
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", network_sync)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", network_async)

    def create(*args: Any, **kwargs: Any) -> UnifiedLLM:
        client = factory(*args, **kwargs)
        created.append(client)
        return client

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert str(request.url) == "https://api.test/v1/chat/completions"
        assert request.headers["authorization"] == f"Bearer {api_key}"
        requests.append(request)
        if mode == "cancel":
            raise asyncio.CancelledError
        if mode == "error":
            return httpx.Response(500, json={"error": {"message": "private-server-text"}})
        return httpx.Response(200, json=response_body("chat"))

    mock_http(monkeypatch, handle)
    monkeypatch.setattr(registry, "client_from_config", create)
    proposal = _plan()
    call = connect._run_probe("test", proposal.entry, proposal.probes[0], api_key)
    if mode == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await call
    elif mode == "error":
        with pytest.raises(InternalServerError) as raised:
            await call
        assert getattr(raised.value, "_connect_http_status", None) == 500
    else:
        response, observed, _ = await call
        assert response.content == "323"
        assert observed is True
    assert len(requests) == 1
    network_sync.assert_not_called()
    network_async.assert_not_called()
    assert len(created) == 1
    transport = created[0]._http
    assert transport is not None
    assert transport.httpx_async.is_closed
    assert transport.httpx_async.event_hooks["request"] == []
    assert transport.httpx_async.event_hooks["response"] == []
