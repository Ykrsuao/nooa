# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""ACP native sandbox selection, safe startup and retryable resource ownership."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, Mock

import pytest
from acp import PROTOCOL_VERSION, RequestError, text_block
from nooa_acp import server

from nooa.interactive import Done, InteractiveAgent
from nooa.unifiedllm import FakeLLMClient


class _Client:
    def __init__(self):
        self.updates = []

    async def session_update(self, session_id, update, **kwargs):
        self.updates.append((session_id, update))


class _SandboxAgent(InteractiveAgent):
    fail_start = False
    failures_to_close = 0

    def __init__(self, *, llm, cwd, storage, backend):
        super().__init__(llm=llm, storage=storage)
        self.cwd = cwd
        self.backend = backend
        self.started = False
        self.close_attempts = 0
        self.closed = False
        self.queue_manager.queue("slash_commands")

    async def start(self):
        self.started = True
        if self.fail_start:
            raise OSError("provisioning failed")

    async def handle(self, notification):
        assert self.started
        return Done(message="sandbox response", explanation="mock native turn")

    async def close(self):
        self.close_attempts += 1
        if self.close_attempts <= self.failures_to_close:
            raise OSError("native cleanup failed")
        if not self.closed:
            await self.aclose()
            await self.queue_manager.shutdown()
            await self.llm.aclose()
            self.closed = True


@pytest.fixture
async def isolated_adapter(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("NEMO_OO_USER_DIR", str(tmp_path / "user-config"))
    monkeypatch.setattr(server, "SandboxCodingAgent", _SandboxAgent)
    monkeypatch.setattr(_SandboxAgent, "fail_start", False)
    monkeypatch.setattr(_SandboxAgent, "failures_to_close", 0)
    adapter = server.CodingACPAdapter(FakeLLMClient, sandbox="auto")
    adapter.on_connect(_Client())  # pyright: ignore[reportArgumentType] - only session_update is used
    yield adapter, workspace
    await adapter.close()


async def test_sandbox_start_skips_workspace_skills_full_agent_and_mcp(
    isolated_adapter, monkeypatch
):
    adapter, root = isolated_adapter
    full_agent = Mock(side_effect=AssertionError("unsafe CodingAgent startup"))
    skills = Mock(side_effect=AssertionError("workspace skills loaded"))
    mcp = AsyncMock(side_effect=AssertionError("MCP startup"))
    monkeypatch.setattr(server, "CodingAgent", full_agent)
    monkeypatch.setattr(server, "load_coding_skills_dirs", skills)
    monkeypatch.setattr(adapter, "_create_mcp_tools", mcp)
    response = await adapter.new_session(str(root))
    session = (await adapter._sessions.get(response.session_id)).value
    assert session.agent.started
    assert session.commands.commands() == ()
    assert session.handle.path.is_relative_to(root.parent / "user-config")
    assert not (root / ".nooa").exists()
    assert await adapter.prompt(response.session_id, [text_block("do work")])
    full_agent.assert_not_called()
    skills.assert_not_called()
    mcp.assert_not_called()


async def test_sandbox_handshake_does_not_advertise_mcp(isolated_adapter):
    adapter, _ = isolated_adapter
    response = await adapter.initialize(PROTOCOL_VERSION)
    caps = response.agent_capabilities.mcp_capabilities
    assert not caps.http and not caps.sse


async def test_sandbox_rejects_forwarded_mcp_before_start(isolated_adapter, monkeypatch):
    adapter, root = isolated_adapter
    factory = Mock(side_effect=AssertionError("should not construct a model"))
    adapter._llm_factory = factory
    with pytest.raises(RequestError) as caught:
        await adapter.new_session(str(root), mcp_servers=[object()])
    assert isinstance(caught.value.data, dict)
    assert "MCP" in caught.value.data["reason"]
    assert not adapter._pending
    factory.assert_not_called()


async def test_sandbox_storage_is_partitioned_by_canonical_workspace(isolated_adapter):
    adapter, root = isolated_adapter
    other = root.parent / "other"
    other.mkdir()
    assert adapter._store(root).root == adapter._store(root / ".").root
    assert adapter._store(root).root != adapter._store(other).root
    assert not adapter._store(root).root.is_relative_to(root)


async def test_sandbox_rejects_store_beneath_granted_root(isolated_adapter, monkeypatch):
    adapter, root = isolated_adapter
    monkeypatch.setenv("NEMO_OO_USER_DIR", str(root / "unsafe"))
    with pytest.raises(RequestError) as caught:
        await adapter.new_session(str(root))
    assert isinstance(caught.value.data, dict)
    assert "outside" in caught.value.data["reason"]
    assert not (root / "unsafe").exists()


async def test_failed_start_and_failed_cleanup_remain_owned(isolated_adapter, monkeypatch):
    adapter, root = isolated_adapter
    monkeypatch.setattr(_SandboxAgent, "fail_start", True)
    monkeypatch.setattr(_SandboxAgent, "failures_to_close", 1)
    with pytest.raises(OSError, match="native cleanup failed"):
        await adapter.new_session(str(root))
    assert len(adapter._pending) == 1
    pending = adapter._pending[0]
    assert pending.agent.started and pending.agent.close_attempts == 1
    pending.handle.record_user_message("handle remains usable for recovery")
    assert pending.handle.path.exists()
    with pytest.raises(RequestError) as caught:
        await adapter.load_session(str(root), pending.handle.id)
    assert isinstance(caught.value.data, dict)
    assert "cleanup is still pending" in caught.value.data["reason"]
    await adapter.close()
    assert pending.agent.closed and pending.agent.close_attempts == 2
    assert not adapter._pending


async def test_failed_session_close_keeps_agent_and_handle_for_retry(isolated_adapter, monkeypatch):
    adapter, root = isolated_adapter
    response = await adapter.new_session(str(root))
    session = (await adapter._sessions.get(response.session_id)).value
    monkeypatch.setattr(_SandboxAgent, "failures_to_close", 1)
    with pytest.raises(OSError, match="native cleanup failed"):
        await adapter.close_session(response.session_id)
    assert session.agent.close_attempts == 1
    session.handle.record_user_message("storage remains owned")
    await adapter.close_session(response.session_id)
    assert session.agent.closed


async def test_cancelled_failed_start_cleanup_is_drained_by_adapter(isolated_adapter, monkeypatch):
    adapter, root = isolated_adapter
    monkeypatch.setattr(_SandboxAgent, "fail_start", True)
    original_close = _SandboxAgent.close
    started, release = asyncio.Event(), asyncio.Event()
    attempts = 0

    class SlowSandboxAgent(_SandboxAgent):
        async def close(self):
            nonlocal attempts
            attempts += 1
            started.set()
            await release.wait()
            await original_close(self)

    monkeypatch.setattr(server, "SandboxCodingAgent", SlowSandboxAgent)
    create = asyncio.create_task(adapter.new_session(str(root)))
    await started.wait()
    create.cancel()
    with pytest.raises(asyncio.CancelledError):
        await create
    assert len(adapter._pending) == 1
    closing = asyncio.create_task(adapter.close())
    await asyncio.sleep(0)
    assert not closing.done()
    release.set()
    await closing
    assert attempts == 1 and not adapter._pending


async def test_sandbox_load_reopens_in_sandbox_without_workspace_store(isolated_adapter):
    adapter, root = isolated_adapter
    response = await adapter.new_session(str(root))
    await adapter.prompt(response.session_id, [text_block("first")])
    await adapter.close_session(response.session_id)
    await adapter.load_session(str(root), response.session_id)
    session = (await adapter._sessions.get(response.session_id)).value
    assert isinstance(session.agent, _SandboxAgent) and session.agent.started
    assert not (root / ".nooa").exists()
    assert len((await adapter.list_sessions(str(root))).sessions) == 1


@pytest.mark.parametrize(
    "platform,backend", [("darwin", "auto"), ("linux", "windows"), ("win32", "linux")]
)
def test_sandbox_rejects_unsupported_host_before_start(monkeypatch, platform, backend):
    monkeypatch.setattr(server.sys, "platform", platform)
    with pytest.raises(ValueError, match="native Linux or Windows"):
        server.CodingACPAdapter(FakeLLMClient, sandbox=backend)


def test_default_store_preserves_workspace_behavior(tmp_path):
    adapter = server.CodingACPAdapter(FakeLLMClient)
    assert adapter._store(tmp_path).root == Path(tmp_path / ".nooa" / "sessions")
