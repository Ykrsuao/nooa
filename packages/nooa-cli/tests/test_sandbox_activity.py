# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Committed edits and command cleanup remain independent of activity sinks."""

import inspect
import sys
from types import MethodType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from nooa_cli.coding.activity import TerminalCommandFinished
from nooa_cli.coding.sandbox_agent import SandboxCodingAgent
from nooa_cli.coding.sandbox_files import FileChange


def _agent(tmp_path, files, add):
    agent = SimpleNamespace(
        _root=tmp_path,
        _commands=[],
        _file_tools=lambda: files,
        event_manager=SimpleNamespace(add=add),
    )
    for name in ("_emit_activity", "_record_edit"):
        setattr(agent, name, MethodType(inspect.unwrap(getattr(SandboxCodingAgent, name)), agent))
    return agent


async def test_file_activity_failure_does_not_fail_successful_checked_edit(tmp_path):
    files = SimpleNamespace(create_edit=AsyncMock(return_value=FileChange(7, None, "after\r\n")))
    agent = _agent(tmp_path, files, Mock(side_effect=OSError("activity sink failed")))
    assert (
        await inspect.unwrap(SandboxCodingAgent.workspace_create)(agent, "file", "after\r\n") == 7
    )
    files.create_edit.assert_awaited_once_with("file", "after\r\n")


@pytest.mark.parametrize("cleanup_fails", [False, True])
async def test_command_records_one_finish_only_after_cleanup(tmp_path, monkeypatch, cleanup_fails):
    from nooa.runtime.sandbox import _linux_commands, _windows_command

    events = []
    owner = SimpleNamespace(
        workspace=tmp_path / "private",
        __aenter__=AsyncMock(),
        run=AsyncMock(
            return_value={
                "stdout": "ok",
                "stderr": "",
                "returncode": 0,
                "timed_out": False,
                "output_truncated": False,
            }
        ),
        aclose=AsyncMock(side_effect=OSError("cleanup failed") if cleanup_fails else None),
    )
    module, name = (
        (_windows_command, "WindowsCommandSession")
        if sys.platform == "win32"
        else (_linux_commands, "LinuxCommandSession")
    )
    monkeypatch.setattr(module, name, lambda: owner)

    def record(event):
        if isinstance(event, TerminalCommandFinished):
            assert owner.aclose.await_count == 1
        events.append(event)

    files = SimpleNamespace(copy_snapshot=AsyncMock(return_value={"files": 1}))
    agent = _agent(tmp_path, files, record)
    if cleanup_fails:
        with pytest.raises(OSError, match="cleanup failed"):
            await inspect.unwrap(SandboxCodingAgent.run_command)(agent, "echo ok")
        assert agent._commands == [owner]
    else:
        result = await inspect.unwrap(SandboxCodingAgent.run_command)(agent, "echo ok")
        assert result["stdout"] == "ok"
        assert agent._commands == []
    finished = [event for event in events if isinstance(event, TerminalCommandFinished)]
    assert len(finished) == 1
    assert finished[0].error == ("cleanup failed" if cleanup_fails else "")


async def test_command_activity_sink_failure_cannot_skip_runtime_cleanup(tmp_path, monkeypatch):
    from nooa.runtime.sandbox import _linux_commands, _windows_command

    owner = SimpleNamespace(
        workspace=tmp_path / "private",
        __aenter__=AsyncMock(),
        run=AsyncMock(
            return_value={
                "stdout": "",
                "stderr": "",
                "returncode": 0,
                "timed_out": False,
                "output_truncated": False,
            }
        ),
        aclose=AsyncMock(),
    )
    module, name = (
        (_windows_command, "WindowsCommandSession")
        if sys.platform == "win32"
        else (_linux_commands, "LinuxCommandSession")
    )
    monkeypatch.setattr(module, name, lambda: owner)
    files = SimpleNamespace(copy_snapshot=AsyncMock(return_value={}))
    agent = _agent(tmp_path, files, Mock(side_effect=OSError("activity sink failed")))
    result = await inspect.unwrap(SandboxCodingAgent.run_command)(agent, "echo ok")
    assert result["returncode"] == 0
    owner.aclose.assert_awaited_once()
    assert agent._commands == []
