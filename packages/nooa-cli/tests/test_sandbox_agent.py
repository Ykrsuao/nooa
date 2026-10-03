# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Exercise coding callbacks through real native workers without a paid LLM."""

import sys
from unittest.mock import AsyncMock

import pytest
from nooa_cli.coding.sandbox_agent import SandboxCodingAgent

from nooa.interactive import Done
from nooa.runtime.sandbox.errors import SandboxUnavailable
from nooa.unifiedllm import FakeLLMClient


def _llm(code):
    return FakeLLMClient.with_tool_call("execute_python", {"code": code})


@pytest.mark.skipif(sys.platform not in ("win32", "linux"), reason="native backend required")
async def test_native_coding_turn_edits_and_tests_disposable_snapshot(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    (root / "original.txt").write_bytes(b"before\r\n")
    (root / ".nooa").mkdir()
    (root / ".nooa" / "untrusted.py").write_text("raise RuntimeError('must not import')")
    outside = tmp_path / "outside.txt"
    outside.write_text("private")
    code = (
        "assert (await self.workspace_read('original.txt')) == 'before\\r\\n'\n"
        "await self.workspace_replace('original.txt', 'before', 'after')\n"
        "await self.workspace_create('new.txt', 'created')\n"
        "await self.workspace_write('new.txt', 'updated')\n"
        "assert (await self.workspace_read('new.txt')) == 'updated'\n"
        "names = await self.workspace_list()\n"
        "assert any(item['name'] == 'new.txt' for item in names)\n"
        "for path in ['../outside.txt', 'new.txt:stream']:\n"
        "    try:\n"
        "        await self.workspace_read(path)\n"
        "    except Exception:\n"
        "        pass\n"
        "    else:\n"
        "        raise AssertionError('unsafe path accepted')\n"
        f"try:\n    open({str(outside)!r}).read()\n"
        "except PermissionError:\n    pass\n"
        "else:\n    raise AssertionError('outside file accessible')\n"
        "r = await self.run_command('echo snapshot > generated.txt')\n"
        "assert r['returncode'] == 0, r\n"
        "assert r['changes_discarded'] is True\n"
        "assert '.nooa' in r['snapshot']['excluded'], r\n"
        "return_result(Done(message='Verified', explanation='native tools passed'))"
    )
    agent = SandboxCodingAgent(llm=_llm(code), cwd=root)
    try:
        await agent.start()
        try:
            result = await agent.handle({"user_messages": ["edit and test"]})
        except Exception:
            for event in agent.event_manager.values():
                print(event)
            raise
        assert isinstance(result, Done)
        assert result.message == "Verified"
        assert (root / "original.txt").read_bytes() == b"after\r\n"
        assert (root / "new.txt").read_text() == "updated"
        assert not (root / "generated.txt").exists()
        assert not agent._commands
    finally:
        await agent.close()


async def test_unsupported_platform_fails_before_allocating_files(tmp_path, monkeypatch):
    import nooa_cli.coding.sandbox_agent as module

    monkeypatch.setattr(module.sys, "platform", "darwin")
    agent = SandboxCodingAgent(llm=_llm(""), cwd=tmp_path)
    with pytest.raises(SandboxUnavailable, match="Windows or Linux"):
        await agent.start()
    assert agent._files is None
    await agent.close()


async def test_failed_cleanup_keeps_owners_for_retry(tmp_path):
    agent = SandboxCodingAgent(llm=_llm(""), cwd=tmp_path)
    owner = AsyncMock()
    owner.aclose.side_effect = [OSError("busy"), None]
    agent._commands.append(owner)
    with pytest.raises(OSError, match="busy"):
        await agent.close()
    assert agent._commands == [owner]
    assert not agent._host_closed
    await agent.close()
    assert agent._commands == []
    assert agent._host_closed
