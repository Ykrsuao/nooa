# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Code sandbox mode keeps the coding agent's host capabilities and lifecycle."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from acp import PROTOCOL_VERSION, text_block
from acp.schema import McpServerStdio
from nooa_acp import server
from nooa_acp._sandbox_namespace import coding_sandbox_globals
from nooa_cli.coding import CodingAgent

from nooa.config import CodeActConfig
from nooa.interactive import Done, NeedInput, Waiting
from nooa.runtime.sandbox import wire
from nooa.runtime.sandbox._spawn_bootstrap import describe, restore
from nooa.strategies import CodeActStrategy
from nooa.unifiedllm import FakeLLMClient, LLMResponse, ToolCall


class Client:
    def __init__(self):
        self.updates = []

    async def session_update(self, session_id, update, **kwargs):
        self.updates.append(update)


def response(code):
    return LLMResponse(
        parts=(ToolCall(id="code", name="execute_python", arguments=json.dumps({"code": code})),),
        finish_reason="tool_calls",
    )


class StubSandbox:
    """Keep production Agent/dispatcher paths real while avoiding native provisioning."""

    instances = []

    def __init__(self, policy, *, backend, config, **kwargs):
        self.policy = policy
        self.config = config
        self.backend = "windows" if sys.platform == "win32" else "linux"
        self.options = kwargs
        self.strategy_options = None
        self.entered = False
        self.closed = False
        self.fail_close = False
        self.close_attempts = 0
        self.instances.append(self)

    async def __aenter__(self):
        self.entered = True
        return self

    def strategy(self, **kwargs):
        self.strategy_options = kwargs
        return CodeActStrategy(config=CodeActConfig(cell_timeout=30))

    async def aclose(self):
        self.close_attempts += 1
        if self.fail_close:
            self.fail_close = False
            raise OSError("native close failed")
        self.closed = True


@pytest.fixture
async def code_adapter(tmp_path, monkeypatch):
    root = tmp_path / "workspace"
    root.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("NEMO_OO_USER_DIR", str(home / "user-config"))
    monkeypatch.delenv("NEMO_OO_SETTINGS", raising=False)
    monkeypatch.setattr(server, "SandboxSession", StubSandbox)
    StubSandbox.instances = []
    adapter = server.CodingACPAdapter(FakeLLMClient, sandbox="auto", sandbox_mode="code")
    client = Client()
    adapter.on_connect(client)  # pyright: ignore[reportArgumentType]
    yield adapter, root, client
    await adapter.close()


async def test_code_mode_registers_mcp_loads_workspace_skills_and_keeps_host_file_tools(
    code_adapter, monkeypatch
):
    adapter, root, _ = code_adapter

    class Lookup:
        async def lookup(self, query: str) -> str:
            return query

    connect = AsyncMock(return_value=Lookup())
    monkeypatch.setattr(server.MCPManager, "create_stdio_server", connect)
    skill = root / ".agents" / "skills" / "workspace.py"
    skill.parent.mkdir(parents=True)
    marker = root / "skill-loaded"
    skill.write_text(
        f"from pathlib import Path\nfrom nooa.skill import Skill\n"
        f"Path({str(marker)!r}).write_text('loaded')\n"
        "class Workspace(Skill):\n    def hello(self):\n        return 'host skill'\n",
        encoding="utf-8",
    )
    adapter._llm_factory = lambda: FakeLLMClient(
        [
            response(
                "await self.shell.write_file('host-edit.txt', 'saved')\nreturn_result(Done(explanation='done'))"
            )
        ]
    )
    initialized = await adapter.initialize(PROTOCOL_VERSION)
    assert initialized.agent_capabilities.mcp_capabilities.http
    created = await adapter.new_session(
        str(root), mcp_servers=[McpServerStdio(name="lookup", command="lookup", args=[], env=[])]
    )
    session = (await adapter._sessions.get(created.session_id)).value
    assert type(session.agent) is CodingAgent
    assert session.handle.info.agent == "CodingAgent"
    assert "mcp.lookup" in session.agent.skills.activated()
    assert marker.read_text() == "loaded"
    result = await adapter.prompt(created.session_id, [text_block("edit")])
    assert result.stop_reason == "end_turn" and (root / "host-edit.txt").read_text() == "saved"
    assert session.sandbox is StubSandbox.instances[0]
    native = StubSandbox.instances[0]
    assert native.policy.network is False and native.policy.broker_timeout_s == 120
    assert native.config.cell_timeout == 30
    if native.backend == "windows":
        assert native.options["application_requirements"] == ("nooa-cli",)
        assert native.strategy_options == {
            "module_globals": coding_sandbox_globals(),
            "data_types": (Done, NeedInput, Waiting),
        }
    else:
        assert native.options == {} and native.strategy_options == {}


def test_coding_sandbox_namespace_types_resolve_without_copying_host_globals():
    from nooa import Context

    namespace = coding_sandbox_globals()
    assert namespace["Path"] is Path and namespace["Context"] is Context
    assert namespace["Done"] is Done
    for name, value in namespace.items():
        assert isinstance(value, type), name
        assert restore(describe(value, wire.Codec(), name), wire.Codec()) is value
    assert {"doc", "self", "hidden", "spec", "nosnapshot", "get_project_dir"}.isdisjoint(namespace)
    namespace["unexpected_host_state"] = object()
    assert "unexpected_host_state" not in coding_sandbox_globals()


async def test_code_mode_waiting_dispatches_followup_with_same_strategy(code_adapter):
    adapter, root, _ = code_adapter
    adapter._llm_factory = lambda: FakeLLMClient(
        [
            response(
                "self.queue_manager.get_channel('system_messages').put('ready')\nreturn_result(Waiting(message='working', explanation='waiting', on=['system_messages']))"
            ),
            response("return_result(Done(message='complete', explanation='ready'))"),
        ]
    )
    created = await adapter.new_session(str(root))
    result = await adapter.prompt(created.session_id, [text_block("wait")])
    assert result.stop_reason == "end_turn"
    assert len(StubSandbox.instances) == 1


async def test_code_mode_native_cleanup_failure_keeps_agent_and_storage_for_retry(
    code_adapter, monkeypatch
):
    adapter, root, _ = code_adapter
    created = await adapter.new_session(str(root))
    session = (await adapter._sessions.get(created.session_id)).value
    native = session.sandbox
    assert native is not None
    native.fail_close = True
    close_agent = AsyncMock(wraps=session.agent.close)
    monkeypatch.setitem(vars(session.agent), "close", close_agent)
    with pytest.raises(OSError, match="native close failed"):
        await adapter.close_session(created.session_id)
    close_agent.assert_not_called()
    session.handle.record_user_message("storage retained")
    await adapter.close_session(created.session_id)
    assert native.closed and native.close_attempts == 2
    close_agent.assert_awaited_once()


async def test_code_mode_load_constructs_new_native_owner(code_adapter):
    adapter, root, _ = code_adapter
    created = await adapter.new_session(str(root))
    await adapter.close_session(created.session_id)
    await adapter.load_session(str(root), created.session_id)
    current = (await adapter._sessions.get(created.session_id)).value
    assert type(current.agent) is CodingAgent
    assert StubSandbox.instances[0].closed
    assert current.sandbox is StubSandbox.instances[1] and current.sandbox.entered


async def test_code_and_strict_modes_keep_separate_session_histories(code_adapter):
    adapter, root, _ = code_adapter
    created = await adapter.new_session(str(root))
    strict = server.CodingACPAdapter(FakeLLMClient, sandbox="auto", sandbox_mode="strict")
    strict.on_connect(Client())  # pyright: ignore[reportArgumentType]
    try:
        code_sessions = await adapter.list_sessions(cwd=str(root))
        strict_sessions = await strict.list_sessions(cwd=str(root))
        assert [session.session_id for session in code_sessions.sessions] == [created.session_id]
        assert strict_sessions.sessions == []
    finally:
        await strict.close()


async def test_code_mode_failed_start_retains_native_owner_until_cleanup_succeeds(
    code_adapter, monkeypatch
):
    adapter, root, _ = code_adapter

    class FailedStart(StubSandbox):
        async def __aenter__(self):
            self.entered = True
            self.fail_close = True
            raise RuntimeError("native provisioning failed")

    monkeypatch.setattr(server, "SandboxSession", FailedStart)
    with pytest.raises(OSError, match="native close failed"):
        await adapter.new_session(str(root))
    assert len(adapter._pending) == 1
    pending = adapter._pending[0]
    native = pending.sandbox
    assert native is not None and not native.closed
    pending.handle.record_user_message("startup failure retained storage")
    await adapter.close()
    assert native.closed and native.close_attempts == 2
    assert adapter._pending == []


async def test_code_mode_cancel_closes_active_call_before_native_session(code_adapter, monkeypatch):
    adapter, root, _ = code_adapter
    created = await adapter.new_session(str(root))
    session = (await adapter._sessions.get(created.session_id)).value
    entered = asyncio.Event()
    stopped = asyncio.Event()

    async def handle(notification, *, _strategy):
        assert _strategy is session.dispatcher._strategy
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    monkeypatch.setitem(vars(session.agent), "handle", handle)
    prompt = asyncio.create_task(adapter.prompt(created.session_id, [text_block("block")]))
    await asyncio.wait_for(entered.wait(), 5)
    await adapter.cancel(created.session_id)
    assert (await asyncio.wait_for(prompt, 5)).stop_reason == "cancelled"
    assert stopped.is_set()
    assert not session.sandbox.closed
    await adapter.close_session(created.session_id)


@pytest.mark.parametrize("sandbox,mode", [("off", "code"), ("auto", "strict")])
def test_adapter_rejects_network_on_without_code_sandbox(sandbox, mode):
    with pytest.raises(ValueError, match="enabled sandbox in code mode"):
        server.CodingACPAdapter(
            FakeLLMClient, sandbox=sandbox, sandbox_mode=mode, sandbox_network="on"
        )
