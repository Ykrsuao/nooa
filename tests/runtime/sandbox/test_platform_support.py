# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Sandbox behavior on hosts that cannot run it, e.g. Windows (no forking)."""

from __future__ import annotations

import sys
from unittest.mock import Mock

import pytest

from nooa import Agent, strategy
from nooa.config import CodeActConfig
from nooa.runtime.sandbox import executor as executor_mod
from nooa.runtime.sandbox.config import FileRule, SandboxConfig
from nooa.runtime.sandbox.errors import SandboxUnavailable
from nooa.runtime.sandbox.executor import SandboxedExecutor
from nooa.runtime.sandbox.guards import Capabilities, probe_capabilities
from nooa.strategies.codeact import CodeActStrategy
from nooa.unifiedllm import FakeLLMClient


class _Agent(Agent, llm=FakeLLMClient()):
    pass


@pytest.mark.parametrize("require", [True, False])
def test_missing_fork_is_a_clear_sandbox_unavailable(monkeypatch, require):
    monkeypatch.setattr(executor_mod.mp, "get_all_start_methods", lambda: ["spawn"])

    with pytest.raises(SandboxUnavailable, match="'fork' multiprocessing start method"):
        SandboxedExecutor(_Agent(), SandboxConfig(require=require), cell_timeout=1.0)


@pytest.mark.parametrize("require", [True, False])
@pytest.mark.parametrize("start_method", ["spawn", "forkserver"])
def test_unvalidated_nonfork_method_cannot_select_a_public_worker(
    monkeypatch, tmp_path, require, start_method
):
    monkeypatch.setattr(executor_mod.mp, "get_all_start_methods", lambda: ["spawn", "forkserver"])
    monkeypatch.setattr(executor_mod, "_capabilities", lambda: Capabilities(False, 0, False, False))
    context = Mock(return_value=object())
    monkeypatch.setattr(executor_mod.mp, "get_context", context)
    workspace = tmp_path / "must-not-be-created"
    config = SandboxConfig(require=require, workspace=str(workspace)).model_copy(
        update={"start_method": start_method}
    )

    with pytest.raises(SandboxUnavailable, match="only the 'fork' multiprocessing start method"):
        SandboxedExecutor(_Agent(), config, cell_timeout=1.0)

    context.assert_not_called()
    assert not workspace.exists()


@pytest.mark.parametrize(
    "policy",
    [
        SandboxConfig().model_copy(update={"memory_limit_bytes": 1024}),
        SandboxConfig().model_copy(update={"max_memory_mb": -1}),
        SandboxConfig().model_copy(update={"network": "false"}),
        SandboxConfig().model_copy(
            update={"allow": (FileRule(path="source").model_copy(update={"access": "write"}),)}
        ),
    ],
    ids=["unknown-units", "negative-limit", "unvalidated-bool", "nested-access"],
)
@pytest.mark.parametrize("require", [True, False])
def test_executor_revalidates_copied_policy_before_resolution(monkeypatch, policy, require):
    resolve = Mock(side_effect=AssertionError("invalid policy reached resolution"))
    context = Mock(side_effect=AssertionError("invalid policy selected a worker"))
    monkeypatch.setattr(executor_mod, "resolve_spec", resolve)
    monkeypatch.setattr(executor_mod.mp, "get_context", context)

    with pytest.raises(SandboxUnavailable, match="Invalid sandbox configuration"):
        SandboxedExecutor(_Agent(), policy.model_copy(update={"require": require}), cell_timeout=1)

    resolve.assert_not_called()
    context.assert_not_called()


@pytest.mark.parametrize(
    "config",
    [
        CodeActConfig().model_copy(update={"windows_sandbox": {"memory_limit_bytes": 1024}}),
        CodeActConfig().model_copy(
            update={"sandbox": SandboxConfig().model_copy(update={"memory_limit_bytes": 1024})}
        ),
        CodeActConfig().model_copy(
            update={"sandbox": SandboxConfig().model_copy(update={"network": "false"})}
        ),
        CodeActConfig().model_copy(
            update={
                "sandbox": SandboxConfig().model_copy(
                    update={
                        "allow": (FileRule(path="source").model_copy(update={"access": "write"}),)
                    }
                )
            }
        ),
    ],
    ids=["unknown-backend-policy", "unknown-units", "unvalidated-bool", "nested-access"],
)
async def test_public_agent_revalidates_copies_before_inprocess_setup(monkeypatch, config):
    backend = CodeActStrategy(config=config)
    setup = Mock(side_effect=AssertionError("invalid configuration entered host execution"))
    monkeypatch.setattr(backend, "_build_builtins", setup)
    touched = []

    class Demo(Agent, llm=FakeLLMClient()):
        def touch(self) -> None:
            touched.append("host tool")

        @strategy(backend)
        async def compute(self) -> str:
            """Return after the pre-ellipsis cell."""
            self.touch()
            ...

    llm = FakeLLMClient()
    with pytest.raises(SandboxUnavailable, match="Invalid CodeAct configuration"):
        await Demo(llm=llm).compute()

    setup.assert_not_called()
    assert not touched
    assert not llm.calls


def test_probe_capabilities_without_resource_module(monkeypatch):
    monkeypatch.setitem(sys.modules, "resource", None)  # import now raises ImportError

    assert probe_capabilities().rlimit is False


def test_supported_fork_policy_still_selects_its_context_without_starting_a_worker(monkeypatch):
    monkeypatch.setattr(executor_mod.mp, "get_all_start_methods", lambda: ["fork", "spawn"])
    monkeypatch.setattr(executor_mod, "_capabilities", lambda: Capabilities(True, 1, True, True))
    context = Mock(return_value=object())
    monkeypatch.setattr(executor_mod.mp, "get_context", context)
    executor = SandboxedExecutor(_Agent(), SandboxConfig(), cell_timeout=1.0)
    try:
        context.assert_called_once_with("fork")
        assert executor.degraded_guards == []
    finally:
        executor.close_sync()


@pytest.mark.parametrize("require", [True, False])
@pytest.mark.parametrize("guards", [True, False], ids=["guarded", "no-guards"])
@pytest.mark.parametrize(
    "backend_name,message",
    [
        ("sandbox", "'fork' multiprocessing start method"),
        ("spawn", "Unsupported CodeAct execution backend"),
        ("lpac", "Unsupported CodeAct execution backend"),
    ],
)
async def test_public_agent_refuses_unavailable_backends_before_cells_tools_or_model(
    monkeypatch, tmp_path, require, guards, backend_name, message
):
    monkeypatch.setattr(executor_mod.mp, "get_all_start_methods", lambda: ["spawn"])
    context = Mock(side_effect=AssertionError("a public worker must not be selected"))
    monkeypatch.setattr(executor_mod.mp, "get_context", context)
    workspace = tmp_path / "must-not-be-created"
    policy = SandboxConfig(
        require=require,
        filesystem=guards,
        network=not guards,
        workspace=str(workspace),
    )
    backend = CodeActStrategy(
        config=CodeActConfig(sandbox=policy).model_copy(update={"execution_backend": backend_name})
    )
    touched = []

    class Demo(Agent, llm=FakeLLMClient()):
        def touch(self) -> None:
            touched.append("host tool")

        @strategy(backend)
        async def compute(self) -> str:
            """Return a value after the pre-ellipsis cell."""
            self.touch()
            ...

    llm = FakeLLMClient()
    agent = Demo(llm=llm)
    with pytest.raises(SandboxUnavailable, match=message):
        await agent.compute()

    assert not touched
    assert not llm.calls
    context.assert_not_called()
    assert not workspace.exists()
