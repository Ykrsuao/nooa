# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Managed live Linux cells cannot create processes; host tools still can."""

from __future__ import annotations

import sys

import pytest

from nooa import Agent
from nooa.runtime.restrictions import RestrictionsConfig
from nooa.runtime.sandbox import _linux_session as linux
from nooa.runtime.sandbox import guards
from nooa.runtime.sandbox._linux_session import _ManagedLinuxExecutor
from nooa.runtime.sandbox.config import SandboxConfig
from nooa.runtime.sandbox.executor import SandboxedExecutor
from nooa.tools.shell_tools import ShellTools
from nooa.unifiedllm import FakeLLMClient

pytestmark = [pytest.mark.sandbox, pytest.mark.skipif(sys.platform != "linux", reason="Linux fork")]


class _Tools(Agent, llm=FakeLLMClient()):
    shell: ShellTools


def _executor(policy=None, *, agent=None, managed=True):
    kwargs = {"tools": None} if managed else {}
    cls = _ManagedLinuxExecutor if managed else SandboxedExecutor
    return cls(
        agent or _Tools(),
        policy or SandboxConfig(),
        cell_timeout=10,
        restrictions=RestrictionsConfig(blocked_modules=frozenset(), blocked_calls={}),
        **kwargs,
    )


@pytest.mark.parametrize("other_guards", [True, False])
async def test_managed_live_worker_denies_child_processes_independent_of_other_guards(other_guards):
    executor = _executor(SandboxConfig(filesystem=other_guards, network=not other_guards))
    try:
        result = await executor.run_cell(
            "import os, subprocess\n"
            "denied = {}\n"
            "actions = {\n"
            "    'fork': os.fork,\n"
            "    'spawn': lambda: os.posix_spawn('/bin/true', ['/bin/true'], {}),\n"
            "    'subprocess': lambda: subprocess.run(['/bin/true']),\n"
            "}\n"
            "for name, action in actions.items():\n"
            "    try:\n"
            "        value = action()\n"
            "        if name in ('fork', 'spawn'):\n"
            "            if value == 0:\n"
            "                os._exit(0)\n"
            "            os.waitpid(value, 0)\n"
            "    except OSError as exc:\n"
            "        denied[name] = exc.errno\n"
            "denied\n"
        )
        assert result.success, result.error
        assert result.returned_value == {"fork": 13, "spawn": 13, "subprocess": 13}
    finally:
        await executor.aclose()


async def test_managed_live_worker_cannot_replace_itself_with_exec():
    executor = _executor()
    try:
        result = await executor.run_cell(
            "import os\n"
            "denied = None\n"
            "try:\n"
            "    os.execve('/bin/true', ['/bin/true'], {})\n"
            "except OSError as exc:\n"
            "    denied = exc.errno\n"
            "denied\n"
        )
        assert result.success, result.error
        assert result.returned_value == 13
        continued = await executor.run_cell("21 * 2")
        assert continued.success and continued.returned_value == 42, continued.error
    finally:
        await executor.aclose()


async def test_managed_live_worker_threads_and_nested_host_shell_still_work(tmp_path):
    shell = ShellTools(cwd=str(tmp_path))
    agent = _Tools()
    agent.shell = shell
    executor = _executor(agent=agent)
    try:
        result = await executor.run_cell(
            "thread_value = await asyncio.to_thread(lambda: 7)\n"
            "print('thread completed')\n"
            "command = await self.shell.run(\"/bin/sh -c 'printf host-process > persisted.txt'\")\n"
            "print('host command completed')\n"
            "content = await self.shell.read('persisted.txt')\n"
            "{'thread': thread_value, 'exit': command.returncode, 'content': content.text}\n"
        )
        assert result.success, (result.error, result.stdout, result.stderr)
        assert result.returned_value == {"thread": 7, "exit": 0, "content": "host-process"}
        assert (tmp_path / "persisted.txt").read_text() == "host-process"
    finally:
        await executor.aclose()
        await shell.close()


@pytest.mark.parametrize("failure", ["seccomp", "architecture"])
async def test_process_filter_failure_cannot_run_cell(monkeypatch, failure):
    executor = _executor(SandboxConfig(filesystem=False, network=True, recovery="disabled"))

    def unavailable(_program):
        raise OSError("process filter unavailable")

    try:
        with monkeypatch.context() as patched:
            if failure == "seccomp":
                patched.setattr(guards, "_seccomp_install", unavailable)
                expected = "process filter unavailable"
            else:
                patched.setattr(linux.platform, "machine", lambda: "unsupported")
                expected = "managed Linux workers require x86_64 or aarch64"
            result = await executor.run_cell("'unguarded cell ran'")
        assert not result.success
        assert expected in str(result.error)
        disabled = await executor.run_cell("6 * 7")
        assert not disabled.success
        assert "disabled" in str(disabled.error)
    finally:
        await executor.aclose()


async def test_raw_linux_executor_preserves_original_process_policy():
    executor = _executor(managed=False)
    try:
        result = await executor.run_cell(
            "import os\n"
            "child = os.fork()\n"
            "if child == 0:\n"
            "    os._exit(0)\n"
            "_, status = os.waitpid(child, 0)\n"
            "status\n"
        )
        assert result.success and result.returned_value == 0, result.error
    finally:
        await executor.aclose()
