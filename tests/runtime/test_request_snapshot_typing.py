# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Request snapshots accept mixed provider messages without changing replay state."""

from typing import Any
from unittest.mock import Mock

import pytest

from nooa.llm_types import CacheBoundary, LLMResponse
from nooa.runtime.actor import _extract_trailing_context_envelope, _snapshot_llm_request


def test_snapshot_preserves_mixed_messages():
    native = object()
    response = LLMResponse(raw_response=native)
    boundary = CacheBoundary()
    messages: list[dict[str, Any] | LLMResponse | CacheBoundary] = [
        {"role": "system", "content": "system prompt"},
        response,
        boundary,
        {"role": "user", "content": "prefix<context>current</context>\n"},
    ]
    originals = tuple(messages)
    events = Mock()

    assert _snapshot_llm_request(events, messages, "generation") == ("<context>current</context>")
    events.add.assert_called_once()
    event = events.add.call_args.args[0]
    assert event.content == "system prompt"
    assert event.generation_id == "generation"
    assert events.add.call_args.kwargs == {"record": False}
    assert all(current is original for current, original in zip(messages, originals, strict=True))
    assert response.raw_response is native


@pytest.mark.parametrize("message", [LLMResponse(), CacheBoundary()])
def test_snapshot_ignores_non_dictionary_edges(message: LLMResponse | CacheBoundary):
    events = Mock()
    assert _snapshot_llm_request(events, (message,), "generation") == ""
    events.add.assert_not_called()


def test_snapshot_accepts_dictionary_only_sequence():
    messages = [{"role": "user", "content": "<context>current</context>"}]
    assert _extract_trailing_context_envelope(messages) == "<context>current</context>"
    assert _extract_trailing_context_envelope([]) == ""
