# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Interactive budget and host-renderer typing contracts."""

from types import SimpleNamespace
from typing import assert_type

import pytest

from nooa.agents.summarization import context_budget
from nooa.interactive import AgentMessage, InteractiveAgent, _summarizer_budget
from nooa.unifiedllm import FakeLLMClient


def test_budget_return_types_follow_fallback():
    unknown = SimpleNamespace()
    assert assert_type(context_budget(unknown), int) == 100_000
    assert assert_type(context_budget(unknown, fallback=0), int) == 0
    assert assert_type(context_budget(unknown, fallback=None), int | None) is None
    known = SimpleNamespace(context_window=1000)
    assert context_budget(known, fallback=None) == 800
    assert _summarizer_budget(FakeLLMClient()) == context_budget(FakeLLMClient(), 0.75)


@pytest.mark.parametrize("legacy", [False, True])
async def test_host_renderer_signatures(legacy: bool):
    agent = InteractiveAgent(llm=FakeLLMClient())
    rendered: list[str] = []
    metadata: list[tuple[str, set[str]]] = []

    def modern(text: str, *, event_id: str, tags: set[str]) -> None:
        rendered.append(text)
        metadata.append((event_id, tags))

    def text_only(text: str) -> None:
        rendered.append(text)

    agent._render_message = text_only if legacy else modern
    try:
        agent.message("hello")
        assert rendered == ["hello"]
        events = [e for e in agent.event_manager.values() if isinstance(e, AgentMessage)]
        assert len(events) == 1
        assert events[0].content == "hello"
        if legacy:
            assert not metadata
        else:
            assert metadata[0][0] == str(events[0].id)
            assert metadata[0][1]
    finally:
        await agent.aclose()
