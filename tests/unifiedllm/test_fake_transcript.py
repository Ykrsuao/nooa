# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Strict scripting and call-transcript contracts for FakeLLMClient."""

from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from typing import Any, cast

import pytest
from pydantic import BaseModel

from nooa.llm_types import AssistantText, CacheBoundary, LLMUsage, ToolCall
from nooa.unifiedllm import (
    FakeLLMClient,
    FakeLLMResponseExhaustedError,
    FakeLLMToolSnapshot,
    LLMResponse,
    Tool,
)


class StructuredAnswer(BaseModel):
    """Structured-output contract used by transcript assertions."""

    answer: int


def _response(content: str) -> LLMResponse:
    """Build a minimal scripted response."""
    return LLMResponse(
        raw_response=None, parts=(AssistantText(text=content),), finish_reason="stop"
    )


def _tool(value: str) -> str:
    """Return a value for tool-metadata capture tests."""
    return value


class _NonCopyable:
    """Opaque runtime value that intentionally rejects deep copying."""

    def __deepcopy__(self, memo):
        """Reject snapshot copying to exercise the opaque-leaf fallback."""
        raise TypeError("cannot copy")


@pytest.mark.asyncio
async def test_transcript_captures_detached_normalized_call() -> None:
    """Call records retain stable inputs, effective kwargs, and outcomes."""
    client = FakeLLMClient([_response("done")])
    message = {"role": "user", "content": "before"}
    messages: list[dict[str, Any] | LLMResponse | CacheBoundary] = [message]
    tools = [Tool(name="lookup", description="Look up a value", callable=_tool)]
    extra_body = {"metadata": {"labels": ["before"]}}

    returned = await client.acall(
        messages,
        tools=tools,
        output_model=StructuredAnswer,
        temperature=0.2,
        extra_body=extra_body,
    )

    message["content"] = "after"
    tools[0].name = "changed"
    extra_body["metadata"]["labels"].append("after")

    assert client.remaining_responses == 0
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call.index == 1
    assert call.messages[0]["content"] == "before"
    assert call.tools is not None
    assert isinstance(call.tools[0], FakeLLMToolSnapshot)
    assert call.tools[0].name == "lookup"
    assert call.output_model is StructuredAnswer
    assert call.kwargs["temperature"] == 0.2
    assert call.kwargs["extra_body"]["metadata"]["labels"] == ("before",)
    assert call.response is not returned
    assert call.response is not None
    assert call.response.content == "done"
    assert call.error is None

    # Exercise forbidden writes through a dynamically typed caller.
    with pytest.raises(TypeError):
        cast(Any, call.kwargs)["temperature"] = 0.9
    with pytest.raises(FrozenInstanceError):
        cast(Any, call).index = 2


@pytest.mark.asyncio
async def test_strict_async_exhaustion_is_recorded() -> None:
    """Strict async calls fail on the first unscripted request and retain it."""
    client = FakeLLMClient([_response("only")], strict_exhaustion=True)

    assert (await client.acall([{"role": "user", "content": "first"}])).content == "only"
    with pytest.raises(FakeLLMResponseExhaustedError, match=r"call 2") as raised:
        await client.acall([{"role": "user", "content": "unexpected"}])

    assert client.call_count == 2
    assert client.remaining_responses == 0
    assert len(client.calls) == 2
    failed = client.calls[1]
    assert failed.index == 2
    assert failed.messages[0]["content"] == "unexpected"
    assert failed.response is None
    assert failed.error is raised.value


def test_strict_sync_exhaustion_matches_async_contract() -> None:
    """The synchronous API exposes the same strict behavior and transcript."""
    client = FakeLLMClient(strict_exhaustion=True)

    with pytest.raises(FakeLLMResponseExhaustedError, match=r"call 1"):
        client.call([{"role": "user", "content": "unexpected"}])

    assert client.call_count == 1
    assert client.remaining_responses == 0
    assert len(client.calls) == 1
    assert isinstance(client.calls[0].error, FakeLLMResponseExhaustedError)


def test_convenience_constructor_forwards_strict_mode() -> None:
    """Convenience constructors expose the same strict scripting option."""
    client = FakeLLMClient.simple_message("only", strict_exhaustion=True)

    assert client.call([]).content == "only"
    with pytest.raises(FakeLLMResponseExhaustedError):
        client.call([])


@pytest.mark.parametrize(
    "factory", ["empty", "code", "message", "tool", "reasoning", "empty_reasoning"]
)
@pytest.mark.parametrize("async_call", [False, True])
async def test_factories_preserve_canonical_response_parts(factory: str, async_call: bool) -> None:
    content = "answer"
    reasoning = None
    calls = []
    finish = "stop"
    usage = LLMUsage(input_tokens=10, output_tokens=1, total_tokens=11)
    if factory == "empty":
        client = FakeLLMClient()
        content, usage = "", None
    elif factory == "code":
        client = FakeLLMClient.with_code_responses([content])
    elif factory == "message":
        client = FakeLLMClient.simple_message(content)
    elif factory == "tool":
        client = FakeLLMClient.with_tool_call("run", {"x": 1})
        content, finish = "", "tool_calls"
        calls = [ToolCall(id="call_fake_123", name="run", arguments='{"x": 1}')]
        usage = LLMUsage(input_tokens=10, output_tokens=5, total_tokens=15)
    else:
        reasoning = "think" if factory == "reasoning" else ""
        client = FakeLLMClient.with_reasoning(reasoning, content)
        usage = LLMUsage(input_tokens=15, output_tokens=10, total_tokens=25)

    expected = LLMResponse.model_validate(
        {"content": content, "tool_calls": calls, "reasoning": reasoning, "finish_reason": finish}
    )
    response = await client.acall([]) if async_call else client.call([])
    assert response.parts == expected.parts
    assert response.public_message() == expected.public_message()
    assert response.finish_reason == expected.finish_reason
    assert response.usage == usage
    assert response.replay_scope is None
    assert all(part.native is None for part in response.parts)


def test_convenience_constructor_preserves_legacy_subclass_default() -> None:
    """Default factory calls do not add a new keyword to legacy subclasses."""

    class LegacyFake(FakeLLMClient):
        """Subclass with the pre-feature constructor shape."""

        def __init__(self, scripted_responses=None):
            """Forward only the historically supported response argument."""
            super().__init__(scripted_responses=scripted_responses)

    client = LegacyFake.simple_message("compatible")

    assert client.call([]).content == "compatible"


@pytest.mark.asyncio
async def test_default_exhaustion_remains_compatible() -> None:
    """Non-strict clients keep returning empty successful responses."""
    client = FakeLLMClient()

    sync_response = client.call([])
    async_response = await client.acall([])

    assert sync_response.content == ""
    assert async_response.content == ""
    assert [call.response.content for call in client.calls if call.response] == ["", ""]
    assert all(call.error is None for call in client.calls)


@pytest.mark.asyncio
async def test_reset_clears_transcript_without_refilling_responses() -> None:
    """Reset clears inspection state while preserving queue consumption."""
    client = FakeLLMClient([_response("first"), _response("second")])
    await client.acall([])

    client.reset()

    assert client.call_count == 0
    assert client.calls == ()
    assert client.last_messages == []
    assert client.last_tools is None
    assert client.remaining_responses == 1
    assert (await client.acall([])).content == "second"
    assert client.calls[0].index == 1


@pytest.mark.asyncio
async def test_concurrent_async_calls_have_ordered_transcript() -> None:
    """The async lock assigns each response and transcript index atomically."""
    count = 50
    client = FakeLLMClient([_response(f"response-{index}") for index in range(count)])

    results = await asyncio.gather(
        *(client.acall([{"role": "user", "content": f"request-{index}"}]) for index in range(count))
    )

    assert [response.content for response in results] == [
        f"response-{index}" for index in range(count)
    ]
    assert [call.index for call in client.calls] == list(range(1, count + 1))
    assert [call.messages[0]["content"] for call in client.calls] == [
        f"request-{index}" for index in range(count)
    ]
    assert client.remaining_responses == 0


@pytest.mark.asyncio
async def test_concurrent_strict_exhaustion_records_every_attempt() -> None:
    """Queued successes and strict failures share one deterministic ordering."""
    client = FakeLLMClient(
        [_response(f"response-{index}") for index in range(10)],
        strict_exhaustion=True,
    )

    results = await asyncio.gather(
        *(client.acall([{"role": "user", "content": f"request-{index}"}]) for index in range(50)),
        return_exceptions=True,
    )

    for index, response in enumerate(results[:10]):
        assert isinstance(response, LLMResponse)
        assert response.content == f"response-{index}"
    assert all(isinstance(error, FakeLLMResponseExhaustedError) for error in results[10:])
    assert len(client.calls) == 50
    assert [call.index for call in client.calls] == list(range(1, 51))
    assert sum(call.response is not None for call in client.calls) == 10
    assert sum(call.error is not None for call in client.calls) == 40


def test_noncopyable_opaque_values_do_not_break_fake_calls() -> None:
    """Transcript capture remains best-effort for intentionally opaque leaves."""
    opaque = _NonCopyable()
    response = LLMResponse(
        raw_response=opaque, parts=(AssistantText(text="done"),), finish_reason="stop"
    )
    client = FakeLLMClient([response])

    returned = client.call([], opaque=opaque)

    assert returned is response
    assert client.calls[0].response is not None
    assert client.calls[0].response.raw_response is opaque
    assert client.calls[0].kwargs["opaque"] is opaque
