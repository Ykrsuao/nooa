# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Native managed workers keep even picklable helper methods on the host."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

from nooa.runtime.sandbox.config import SandboxConfig
from nooa.runtime.sandbox.serialization import is_picklable

pytestmark = [
    pytest.mark.skipif(sys.platform not in ("win32", "linux"), reason="native sandbox required"),
    pytest.mark.timeout(240),
]


class PicklableHostTool:
    def __init__(self):
        self.count = 0

    def increment(self, amount: int) -> tuple[int, int]:
        self.count += amount
        return self.count, os.getpid()


async def test_native_host_helpers_persist_while_data_reads_remain_snapshots():
    agent = SimpleNamespace(helper=PicklableHostTool(), items=[1])
    assert is_picklable(agent.helper)
    runtime = None
    if sys.platform == "win32":
        from nooa.runtime.sandbox._appcontainer import _AppContainerPython
        from nooa.runtime.sandbox._lpac import _LpacExecutor
        from nooa.runtime.sandbox._lpac_runtime import stage_framework

        runtime = _AppContainerPython()
        runtime.__enter__()
        try:
            stage_framework(runtime)
            executor = _LpacExecutor(
                runtime, host_tools=True, live_agent=agent, startup_timeout_s=90
            )
        except BaseException:
            runtime.close()
            raise
    else:
        from nooa.runtime.sandbox._linux_session import _ManagedLinuxExecutor

        executor = _ManagedLinuxExecutor(agent, SandboxConfig(), tools=None, cell_timeout=10)
    try:
        result = await executor.run_cell(
            "count, tool_pid = self.helper.increment(2)\n"
            f"assert tool_pid == {os.getpid()}, 'helper ran inside the worker'\n"
            "assert count == 2\n"
            "snapshot = self.items\n"
            "snapshot.append(99)\n"
            "assert self.items == [1], 'snapshot mutation reached host'\n"
            "self.items = self.items + [2]\n"
            "assert self.items == [1, 2]\n"
            "self.helper.increment(3)[0]"
        )
        assert result.success, result.error
        assert result.returned_value == 5
        assert agent.helper.count == 5
        assert agent.items == [1, 2]
        assert executor._proc is not None and executor._proc.pid != os.getpid()
    finally:
        await executor.aclose()
        if runtime is not None:
            runtime.close()
