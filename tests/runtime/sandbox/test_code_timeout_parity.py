# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""The same native-worker deadline/recovery contract on Windows and Linux."""

from __future__ import annotations

import asyncio
import sys
import time
from typing import Literal

import pytest

from nooa import Agent
from nooa.runtime.sandbox.config import SandboxConfig
from nooa.runtime.sandbox.errors import CellTimeoutError, WorkerDiedError
from nooa.runtime.sandbox.executor import SandboxedExecutor
from nooa.unifiedllm import FakeLLMClient

pytestmark = [
    pytest.mark.skipif(sys.platform not in ("win32", "linux"), reason="native code sandbox"),
    pytest.mark.timeout(180),
]


class _HostTools(Agent, llm=FakeLLMClient()):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()
        self.calls = 0

    async def slow(self, seconds: float) -> int:
        self.calls += 1
        self.started.set()
        try:
            await asyncio.sleep(seconds)
        except asyncio.CancelledError:
            # Real host tools release subprocesses or sockets asynchronously.
            # Worker retirement must await cleanup without cancelling it again.
            await asyncio.sleep(0.1)
            self.cancelled.set()
            raise
        return self.calls


@pytest.fixture(scope="module")
def native_runtime():
    if sys.platform == "win32":
        from nooa.runtime.sandbox._appcontainer import _AppContainerPython
        from nooa.runtime.sandbox._lpac_runtime import stage_framework

        with _AppContainerPython() as runtime:
            stage_framework(runtime)
            yield runtime
    else:
        yield None


@pytest.fixture
async def make_executor(native_runtime):
    executors = []

    def make(
        *,
        cell_timeout=0.2,
        grace=0.6,
        broker=0,
        recovery: Literal["restart_empty", "disabled"] = "restart_empty",
    ):
        agent = _HostTools()
        if sys.platform == "win32":
            from nooa.runtime.sandbox._lpac import _LpacExecutor

            executor = _LpacExecutor(
                native_runtime,
                host_tools=True,
                live_agent=agent,
                cell_timeout=cell_timeout,
                timeout_grace_s=grace,
                broker_timeout_s=broker,
                recovery=recovery,
                startup_timeout_s=60,
            )
        else:
            # The facade uses the scoped lifecycle so cancellation drains host
            # tasks before returning. Keep that same native executor here.
            from nooa.runtime.sandbox._linux_session import _ManagedLinuxExecutor

            executor = _ManagedLinuxExecutor(
                agent,
                SandboxConfig(
                    timeout_grace_s=grace,
                    broker_timeout_s=broker,
                    recovery=recovery,
                ),
                cell_timeout=cell_timeout,
                tools=None,
            )
        executors.append(executor)
        return agent, executor

    try:
        yield make
    finally:
        for executor in executors:
            await executor.aclose()


async def _value(executor: SandboxedExecutor, code: str):
    result = await executor.run_cell(code)
    assert result.success, result.error
    return result.returned_value


async def test_grace_and_unbounded_host_time_then_hard_kill_recovery(make_executor):
    agent, executor = make_executor()
    # 0.4 seconds exceeds the nominal cell budget but fits its explicit grace.
    # Host time exceeds the entire budget and must not consume either part.
    assert (
        await _value(
            executor,
            "import time\nmarker = 42\nawait self.slow(1.5)\ntime.sleep(0.4)\nmarker",
        )
        == 42
    )
    assert agent.calls == 1
    started = time.monotonic()
    result = await executor.run_cell("while True: pass")
    assert not result.success and isinstance(result.error, CellTimeoutError)
    assert time.monotonic() - started >= 0.65
    assert await _value(executor, "'marker' in globals()") is False
    assert await _value(executor, "await self.slow(0)") == 2


async def test_broker_deadline_cancels_host_call_and_resets_worker(make_executor):
    agent, executor = make_executor(cell_timeout=None, grace=1, broker=0.2)
    result = await executor.run_cell("marker = 42\nawait self.slow(60)")
    assert not result.success and isinstance(result.error, CellTimeoutError)
    assert "broker_timeout" in str(result.error)
    assert agent.cancelled.is_set()
    assert await _value(executor, "'marker' in globals()") is False


@pytest.mark.parametrize("recovery", ["restart_empty", "disabled"])
async def test_cancelled_unbounded_host_call_drains_and_obeys_recovery(make_executor, recovery):
    agent, executor = make_executor(recovery=recovery)
    task = asyncio.create_task(executor.run_cell("await self.slow(60)"))
    try:
        await asyncio.wait_for(agent.started.wait(), 60)
        task.cancel()
        await asyncio.sleep(0.02)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert agent.cancelled.is_set()
        if recovery == "disabled":
            result = await executor.run_cell("42")
            assert not result.success and isinstance(result.error, WorkerDiedError)
            assert "disabled" in str(result.error)
        else:
            assert await _value(executor, "42") == 42
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
