# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Common tool semantics must be described accurately by both native sandboxes."""

from __future__ import annotations

import pytest

from nooa.config import CodeActConfig
from nooa.runtime.sandbox._linux_session import _LinuxSandboxSession, _ManagedLinuxStrategy
from nooa.runtime.sandbox._windows_context import _render_windows_policy
from nooa.runtime.sandbox._windows_policy import _WindowsSandboxPolicy
from nooa.runtime.sandbox.config import SandboxConfig
from nooa.runtime.sandbox.context_block import (
    render_host_tools_block,
    render_sandbox_block,
    render_value_transfer_block,
)


@pytest.mark.parametrize("network", [False, True])
def test_native_contexts_share_live_tool_and_data_contracts(network):
    linux = render_sandbox_block(SandboxConfig(network=network), cell_timeout=30)
    windows = _render_windows_policy(_WindowsSandboxPolicy(host_tools=True, network=network))
    for text in (linux, windows):
        assert render_host_tools_block() in text
        assert render_value_transfer_block() in text
        assert "OUTSIDE the cell sandbox" in text
        assert "shell changes persist" in text
        assert "self.items.append(x), do not update the host" in text
        assert "picklable" not in text


@pytest.mark.parametrize("host_tools", [False, True])
def test_only_live_tool_modes_advertise_host_fields_and_mcp(host_tools):
    linux = render_sandbox_block(SandboxConfig(), cell_timeout=30, host_tools=host_tools)
    windows = _render_windows_policy(_WindowsSandboxPolicy(host_tools=host_tools))
    for text in (linux, windows):
        assert ("skills and MCP" in text) is host_tools
        assert ("self.<attr>" in text) is host_tools
        assert ("No other Agent methods or live self fields" in text) is not host_tools


@pytest.mark.parametrize("grace", [0, 2.5])
@pytest.mark.parametrize("broker_timeout", [0, 120])
def test_deadlines_distinguish_cell_grace_and_host_tool_budget(grace, broker_timeout):
    linux = render_sandbox_block(
        SandboxConfig(timeout_grace_s=grace, broker_timeout_s=broker_timeout), cell_timeout=30
    )
    windows = _render_windows_policy(
        _WindowsSandboxPolicy(
            cell_timeout_s=30, timeout_grace_s=grace, broker_timeout_s=broker_timeout
        )
    )
    for text in (linux, windows):
        assert "30s" in text
        assert f"{grace:g}s" in text
        assert "grace before hard-kill" in text
        assert "host tools is excluded from the cell deadline" in text
        assert ("120s" if broker_timeout else "deadline: disabled") in text


def test_disabled_cell_deadline_does_not_advertise_a_hard_kill():
    linux = render_sandbox_block(SandboxConfig(timeout_grace_s=2), cell_timeout=None)
    windows = _render_windows_policy(_WindowsSandboxPolicy(cell_timeout_s=None, timeout_grace_s=2))
    for text in (linux, windows):
        assert "hard-kill" not in text


@pytest.mark.parametrize("tools", [None, (), ("lookup",)])
async def test_managed_linux_context_uses_actual_tool_scope(monkeypatch, tools):
    owner = _LinuxSandboxSession(SandboxConfig(), tools=tools)
    monkeypatch.setattr(owner, "_require_ready", lambda: None)
    strategy = _ManagedLinuxStrategy(
        owner, config=CodeActConfig(execution_backend="sandbox", sandbox=owner.policy)
    )
    # Rendering depends only on session policy, without a running Agent.
    text = await strategy.sandbox_context(None)  # pyright: ignore[reportArgumentType]
    assert ("skills and MCP" in text) is (tools is None)
    assert ("No other Agent methods or live self fields" in text) is (tools is not None)
    assert "the worker cannot launch child processes" in text


def test_raw_linux_renderer_does_not_claim_managed_process_filter():
    text = render_sandbox_block(SandboxConfig(), cell_timeout=30)
    assert "cannot launch child processes" not in text


@pytest.mark.parametrize("recovery", ["restart_empty", "disabled"])
def test_native_contexts_explain_recovery_without_rollback(recovery):
    linux = render_sandbox_block(SandboxConfig(recovery=recovery), cell_timeout=30)
    windows = _render_windows_policy(_WindowsSandboxPolicy(recovery=recovery))
    for text in (linux, windows):
        assert ("replaced with empty globals" in text) is (recovery == "restart_empty")
        assert ("worker replacement is disabled" in text) is (recovery == "disabled")
        assert "Neither replacement nor failure rolls back files" in text


def test_linux_memory_description_uses_headroom_not_absolute_cap():
    text = render_sandbox_block(SandboxConfig(max_memory_mb=512), cell_timeout=30)
    assert "512 MiB of additional address-space headroom above the worker baseline" in text
    assert "not an absolute committed-memory limit" in text
