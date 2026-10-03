# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Shared code-sandbox intent must be honored or rejected before provisioning."""

from __future__ import annotations

import pytest

from nooa.config import CodeActConfig
from nooa.runtime.sandbox import SandboxConfig, SandboxSession, SandboxUnavailable
from nooa.runtime.sandbox import session as facade


@pytest.fixture
def windows(monkeypatch):
    monkeypatch.setattr(facade.sys, "platform", "win32")


@pytest.mark.parametrize("network", [False, True])
def test_windows_accepts_shared_code_policy_without_starting_resources(windows, network):
    policy = SandboxConfig(network=network, broker_timeout_s=120)
    session = SandboxSession(policy, config=CodeActConfig(cell_timeout=30))
    assert session.policy == policy
    assert session.backend == "windows"
    assert session._session.policy.network is network
    assert session._session.policy.host_tools is True
    assert session._session.policy.cell_timeout_s == 30
    assert session._session.policy.timeout_grace_s == policy.timeout_grace_s
    assert session._session.policy.broker_timeout_s == 120
    assert session._session._runtime is None


def test_explicit_tool_allowlist_does_not_enable_host_agent_access(windows):
    session = SandboxSession(SandboxConfig(), tools=("lookup",))
    assert session._session.policy.host_tools is False
    assert session._session.policy.tools == ("lookup",)


@pytest.mark.parametrize(
    "override",
    [
        {"workspace": "C:/project"},
        {"allow": ({"path": "C:/project", "access": "read"},)},
        {"filesystem": False},
        {"system_paths": False},
        {"max_memory_mb": 128},
        {"max_cpu_seconds": 3},
        {"rss_poll_s": 0.5},
    ],
)
def test_windows_rejects_unavailable_linux_semantics_instead_of_ignoring(windows, override):
    with pytest.raises(SandboxUnavailable, match="Windows.*unsupported"):
        SandboxSession(SandboxConfig(**override))


def test_shared_policy_cannot_request_silent_degradation(windows):
    with pytest.raises(ValueError, match="require=True"):
        SandboxSession(SandboxConfig(require=False))


def test_shared_policy_is_revalidated(windows):
    with pytest.raises(ValueError):
        SandboxSession(SandboxConfig().model_copy(update={"network": "yes"}))


def test_shared_policy_preserves_recovery_and_context_choice(windows):
    session = SandboxSession(SandboxConfig(recovery="disabled", context_block=False))
    assert session._session.policy.recovery == "disabled"
    assert session._session.policy.context_block is False


def test_conflicting_generation_policy_is_rejected(windows):
    with pytest.raises(ValueError, match="session policy"):
        SandboxSession(SandboxConfig(), config=CodeActConfig(execution_backend="sandbox"))


@pytest.mark.parametrize("grace", [0, 0.5, 4])
def test_shared_timeout_grace_is_preserved_on_windows(windows, grace):
    session = SandboxSession(SandboxConfig(timeout_grace_s=grace))
    assert session._session.policy.timeout_grace_s == grace


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_shared_session_rejects_per_strategy_deadline_changes(monkeypatch, platform):
    monkeypatch.setattr(facade.sys, "platform", platform)
    session = SandboxSession(SandboxConfig(), config=CodeActConfig(cell_timeout=7))
    with pytest.raises(ValueError, match="set cell_timeout when creating"):
        session.strategy(config=CodeActConfig(cell_timeout=8))


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_shared_session_passes_matching_deadline_to_native_strategy(monkeypatch, platform):
    monkeypatch.setattr(facade.sys, "platform", platform)
    session = SandboxSession(SandboxConfig(), config=CodeActConfig(cell_timeout=7))
    received = {}

    def native_strategy(**kwargs):
        received.update(kwargs)

    monkeypatch.setattr(session._session, "strategy", native_strategy)
    session.strategy(config=CodeActConfig(cell_timeout=7))
    expected = 7 if platform == "linux" else CodeActConfig().cell_timeout
    assert received["config"].cell_timeout == expected


@pytest.mark.parametrize("platform", ["linux", "win32"])
@pytest.mark.parametrize("deadline", [0, -1, float("inf"), float("nan")])
def test_shared_session_rejects_invalid_deadlines_before_startup(monkeypatch, platform, deadline):
    monkeypatch.setattr(facade.sys, "platform", platform)
    with pytest.raises(ValueError, match="cell_timeout"):
        SandboxSession(SandboxConfig(), config=CodeActConfig(cell_timeout=deadline))


@pytest.mark.parametrize("name", ["broker_timeout_s", "timeout_grace_s", "rss_poll_s"])
@pytest.mark.parametrize("value", [float("inf"), float("nan")])
def test_shared_policy_rejects_nonfinite_timing(name, value):
    with pytest.raises(ValueError):
        SandboxConfig(**{name: value})
