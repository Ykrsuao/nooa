# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Summary ownership, optional runtime and middleware result contracts."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from nooa import Agent
from nooa.agents import MethodSummarizer, TokenBudgetSummarizer
from nooa.config.summarizer_config import TokenBudgetConfig
from nooa.events import Message
from nooa.runtime.middleware import ExecutePythonContext, LLMCallContext
from nooa.unifiedllm import FakeLLMClient, LLMResponse, LLMUsage


def _setup() -> tuple[Agent, TokenBudgetSummarizer, LLMCallContext]:
    agent = Agent(llm=FakeLLMClient())
    for i in range(3):
        agent.event_manager.add(Message(content=f"fact {i}"))
    summarizer = TokenBudgetSummarizer(
        agent, config=TokenBudgetConfig(max_tokens=10, preserve_recent=1)
    )
    ctx = LLMCallContext(agent=agent, runtime=agent.runtime, client=agent.llm, messages=[])
    return agent, summarizer, ctx


def test_detached_standalone_client_preserves_fixed_and_task_local_selection():
    parent = Agent(llm=FakeLLMClient())
    summarizer = MethodSummarizer(parent)
    original = summarizer.llm
    summarizer._target_agent = None
    assert summarizer._summary_llm() is original
    override = FakeLLMClient()
    token = summarizer._summary_llm_override.set(override)
    try:
        assert summarizer._summary_llm() is override
    finally:
        summarizer._summary_llm_override.reset(token)
        summarizer._uninstall()


def test_reinstall_without_parent_does_not_add_subscriptions(monkeypatch):
    agent, summarizer, _ = _setup()
    summarizer._uninstall()
    summarizer._target_agent = None
    subscribe = Mock()
    monkeypatch.setattr(agent.event_manager, "on", subscribe)
    monkeypatch.setattr(agent.event_manager, "intercept", subscribe)
    monkeypatch.setattr(agent.event_manager, "on_close", subscribe)
    with pytest.raises(ValueError, match="target agent is None"):
        summarizer._install()
    subscribe.assert_not_called()


@pytest.mark.parametrize("history", [None, "provided history"])
async def test_standalone_rendered_or_provided_history_is_text(monkeypatch, history):
    agent = Agent(llm=FakeLLMClient())
    summarizer = MethodSummarizer(agent)
    render = Mock(return_value="rendered history")
    summarize = AsyncMock(return_value="summary")
    monkeypatch.setattr(summarizer, "_render_range_to_markdown", render)
    summarizer.summarize = summarize
    try:
        await summarizer._run_summarization(history, "1", "2")
        summarize.assert_awaited_once_with(
            history or "rendered history", summarizer.config.target_chars
        )
        assert render.call_count == (1 if history is None else 0)
        assert summarizer._pending_summary == "summary"
        assert summarizer._summary_llm_override.get() is None
        assert not summarizer.event_manager.keys()
    finally:
        await summarizer.aclose()


@pytest.mark.parametrize("has_counter", [False, True])
def test_counter_retains_callable_or_approximate_fallback(monkeypatch, has_counter):
    from nooa.token_counter import char_approximate_token_counter

    summarizer = MethodSummarizer(Agent(llm=FakeLLMClient()))
    counter = Mock(return_value=7) if has_counter else None
    monkeypatch.setattr(summarizer, "_summary_llm", lambda: SimpleNamespace(count_tokens=counter))
    try:
        count = summarizer._input_token_counter()
        assert count("hello") == (7 if has_counter else char_approximate_token_counter("hello"))
    finally:
        summarizer._uninstall()


async def test_detached_fork_interceptor_passes_parent_through():
    agent, summarizer, ctx = _setup()
    summarizer.target_event_manager = None
    core = AsyncMock(return_value=ctx)
    try:
        assert await summarizer._fork_after_call(ctx, core) is ctx
        core.assert_awaited_once_with(ctx)
        assert summarizer._pending_task is None
        assert agent.event_manager.keys() == ["1", "2", "3"]
    finally:
        await summarizer.aclose()


async def test_automatic_budget_without_runtime_skips_fork(monkeypatch, caplog):
    agent, summarizer, ctx = _setup()
    summarizer._automatic_context_budget = True
    ctx.runtime = None
    ctx.response = LLMResponse.model_validate(
        {"content": "parent", "usage": LLMUsage(input_tokens=100)}
    )
    dispatch = AsyncMock()
    monkeypatch.setattr(agent.llm, "acall", dispatch)
    try:
        assert await summarizer._fork_after_call(ctx, AsyncMock(return_value=ctx)) is ctx
        assert summarizer._pending_task is None
        dispatch.assert_not_awaited()
        assert "no runtime" in caplog.text
        assert agent.event_manager.keys() == ["1", "2", "3"]
    finally:
        await summarizer.aclose()


@pytest.mark.parametrize("missing", ["manager", "client", "context"])
async def test_invalid_fork_state_is_contained_without_dispatch(monkeypatch, caplog, missing):
    agent, summarizer, ctx = _setup()
    dispatch = AsyncMock()
    monkeypatch.setattr(agent.llm, "acall", dispatch)
    if missing == "manager":
        summarizer.target_event_manager = None
    elif missing == "client":
        ctx = ctx.model_copy(update={"client": None})
    else:
        monkeypatch.setattr(
            agent.event_manager,
            "run_middleware",
            AsyncMock(return_value=ExecutePythonContext(code="not a summary")),
        )
    try:
        await summarizer._run_fork(ctx)
        dispatch.assert_not_awaited()
        assert summarizer._pending_summary is None
        assert summarizer._failed_forks == 1
        assert "Summary fork failed" in caplog.text
        assert agent.event_manager.keys() == ["1", "2", "3"]
    finally:
        await summarizer.aclose()
