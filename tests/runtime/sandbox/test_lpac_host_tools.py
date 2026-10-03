# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Native LPAC cells with explicitly retained ordinary CodingAgent host tools."""

from __future__ import annotations

import asyncio
import json
import os
import pickle
import sys
from types import SimpleNamespace

import pytest

from nooa.events import PythonOutput
from nooa.interactive import Done, NeedInput, Waiting
from nooa.runtime.sandbox._appcontainer import _AppContainerPython
from nooa.runtime.sandbox._lpac import _LpacExecutor
from nooa.runtime.sandbox._lpac_codeact import _LpacCodeActStrategy
from nooa.runtime.sandbox._lpac_runtime import stage_framework
from nooa.runtime.sandbox.errors import WorkerDiedError
from nooa.unifiedllm import FakeLLMClient, LLMResponse, ToolCall

pytestmark = [
    pytest.mark.skipif(sys.platform != "win32", reason="native Windows LPAC"),
    pytest.mark.timeout(240),
]


@pytest.fixture(scope="module")
def framework():
    with _AppContainerPython() as runtime:
        stage_framework(runtime, application_requirements=("nooa-cli",))
        yield runtime


@pytest.fixture(autouse=True)
def isolated_configuration(monkeypatch, tmp_path):
    monkeypatch.setenv("PYTHON_DOTENV_DISABLED", "1")
    monkeypatch.setenv("LITELLM_LOCAL_MODEL_COST_MAP", "True")
    monkeypatch.setenv("NEMO_OO_USER_DIR", str(tmp_path / "user-config"))
    monkeypatch.setenv("NEMO_OO_PROJECT_DIR", str(tmp_path / "project-config"))


def _responses(*cells):
    return FakeLLMClient(
        [
            LLMResponse(
                parts=(
                    ToolCall(
                        id=f"host-tools-{i}",
                        name="execute_python",
                        arguments=json.dumps({"code": code}),
                    ),
                ),
                finish_reason="tool_calls",
            )
            for i, code in enumerate(cells)
        ],
        strict_exhaustion=True,
    )


def _backend(runtime):
    return _LpacCodeActStrategy(
        runtime,
        host_tools=True,
        module_globals={"Done": Done, "NeedInput": NeedInput, "Waiting": Waiting},
        data_types=(Done, NeedInput, Waiting),
        startup_timeout_s=90,
    )


async def test_normal_coding_tools_persist_and_waiting_roundtrips(framework, tmp_path):
    from nooa_cli.coding.agent import CodingAgent

    root = tmp_path / "workspace"
    root.mkdir()
    (root / "example.py").write_text("def parity_symbol():\n    return 42\n", encoding="utf-8")
    backend = _backend(framework)
    received = []

    class ForwardedMCPTools:
        async def lookup(self, query: str) -> str:
            """Look up a value through a client-forwarded MCP tool."""
            received.append(query)
            return "fixture result"

    agent = CodingAgent(
        llm=_responses(
            "assert 'run' in doc(self.shell)\n"
            "r = await self.shell.run(\"printf 'original' > command.txt\")\n"
            "assert r.returncode == 0, r\n"
            "assert r.success\n"
            "assert (await self.shell.read('command.txt')).text == 'original'\n"
            "await self.shell.replace('command.txt', 'original', 'updated')\n"
            "assert (await self.shell.read('command.txt')).text == 'updated'\n"
            "symbols = await self.repo.symbols('example.py', query='parity_symbol')\n"
            "assert symbols.total_matches == 1, symbols\n"
            "assert 'parity_symbol' in symbols.text\n"
            "assert 'lookup' in doc(self.search)\n"
            "assert await self.search.lookup('fixture query') == 'fixture result'\n"
            "assert 'mcp.search' in self.skills.activated()\n"
            "self.message('Host tools verified')\n"
            "return_result(Waiting(message='Waiting for follow-up.', "
            "explanation='checked tools', on=['system_messages']))",
            "assert (await self.shell.read('command.txt')).text == 'updated'\n"
            "return_result(Done(message='Resumed.', explanation='checked persistence'))",
        ),
        cwd=root,
        libs_dir=root / ".nooa" / "libs",
    )
    agent.skills.register("mcp.search", ForwardedMCPTools())
    agent.skills.activate(["mcp.search"])
    try:
        waiting = await agent.handle({"user_messages": ["verify"]}, _strategy=backend)
        assert isinstance(waiting, Waiting)
        assert waiting.on == ["system_messages"]
        assert (root / "command.txt").read_text() == "updated"
        result = await agent.handle({"system_messages": ["continue"]}, _strategy=backend)
        assert isinstance(result, Done)
        assert result.message == "Resumed."
        assert received == ["fixture query"]
        outputs = [e for e in agent.event_manager.values() if isinstance(e, PythonOutput)]
        assert outputs and all(not event.error for event in outputs), outputs
    finally:
        await agent.close()


async def test_live_proxy_keeps_native_cells_and_safe_inbound_protocol(framework, tmp_path):
    marker = tmp_path / "pickle-must-not-run.txt"

    class Payload:
        def __reduce__(self):
            return eval, (f"__import__('pathlib').Path({str(marker)!r}).write_text('bad')",)

    raw = pickle.dumps(Payload())
    executor = _LpacExecutor(
        framework,
        host_tools=True,
        live_agent=SimpleNamespace(value=1, nested=SimpleNamespace(echo=lambda value: value)),
        startup_timeout_s=90,
    )
    try:
        result = await executor.run_cell("self.value = 2\nself.nested.echo(self.value)")
        assert result.success, result.error
        assert result.returned_value == 2 and executor._agent.value == 2
        assert executor._proc is not None and executor._proc.pid != os.getpid()
        assert type(executor._proc).__name__ == "LpacProcess"
        assert not (await executor.run_cell(f"open({str(marker)!r}, 'w')")).success
        assert not (await executor.run_cell("__import__('socket').socket()")).success
        worker_pid = executor._proc.pid
        refused = await executor.run_cell(
            "from nooa.runtime.sandbox.worker import _PROXY_STATE\n"
            "_PROXY_STATE[self][0]._rpc({'type': 'tool_call', 'kind': 'call', "
            f"'path': ['nested', 'echo'], 'payload': {raw!r}}})"
        )
        assert not refused.success
        assert "malformed sandbox message" in str(refused.error)
        assert not marker.exists()
        assert executor._proc.pid == worker_pid
        result = await executor.run_cell(
            "from nooa.runtime.sandbox.worker import _PROXY_STATE\n"
            f"_PROXY_STATE[self][0]._conn.send_bytes({raw!r})\n"
            "while True:\n    pass"
        )
        assert isinstance(result.error, WorkerDiedError)
        assert not marker.exists()
        recovered = await executor.run_cell("self.value")
        assert recovered.success and recovered.returned_value == 2
    finally:
        await executor.aclose()


def test_host_mode_is_explicit_and_cannot_mix_exact_grants(framework):
    with pytest.raises(ValueError, match="live_agent"):
        _LpacExecutor(framework, host_tools=True)
    with pytest.raises(ValueError, match="host_tools"):
        _LpacExecutor(framework, live_agent=object())
    with pytest.raises(ValueError, match="exact tool"):
        _LpacExecutor(framework, host_tools=True, live_agent=object(), tools={"echo": str})
    with pytest.raises(ValueError, match="exact tool"):
        _LpacCodeActStrategy(framework, host_tools=True, tools=("echo",))


async def test_cancelling_live_nested_host_tool_reaps_worker(framework):
    started, cancelled = asyncio.Event(), asyncio.Event()

    async def slow():
        started.set()
        try:
            await asyncio.sleep(60)
        finally:
            cancelled.set()

    executor = _LpacExecutor(
        framework,
        host_tools=True,
        live_agent=SimpleNamespace(nested=SimpleNamespace(slow=slow)),
        startup_timeout_s=90,
    )
    task = asyncio.create_task(executor.run_cell("await self.nested.slow()"))
    try:
        await asyncio.wait_for(started.wait(), 120)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert cancelled.is_set()
        assert executor._proc is None and executor._io_task is None
        assert not executor._tool_tasks and not executor._processes
    finally:
        await executor.aclose()
        await asyncio.gather(task, return_exceptions=True)
