# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Public Windows contract: configuration is available, launch remains gated."""

from __future__ import annotations

import dataclasses
from unittest.mock import Mock

import pytest

from nooa.config import CodeActConfig
from nooa.runtime.sandbox.config import SandboxConfig
from nooa.runtime.sandbox.errors import SandboxUnavailable


def test_public_policy_is_explicit_and_immutable(tmp_path):
    from nooa.runtime.sandbox.windows import (
        DirectoryGrant,
        FileGrant,
        HttpsEndpoint,
        WindowsSandboxPolicy,
    )

    inputs = {"seed.txt": b"snapshot"}
    policy = WindowsSandboxPolicy(
        inputs=inputs,
        files={"output": FileGrant(tmp_path / "output.txt", writable=True)},
        directories={"source": DirectoryGrant(tmp_path / "source")},
        https={"docs": HttpsEndpoint("https://example.com/", "93.184.216.34")},
        memory_limit_bytes=1024**3,
        cpu_time_limit_s=10,
    )
    inputs["seed.txt"] = b"changed"
    assert policy.workspace_access == "read"
    assert policy.inputs["seed.txt"] == b"snapshot"
    assert policy.files["output"].writable is True
    assert policy.directories["source"].writable is False
    assert policy.memory_limit_bytes == 1024**3
    assert policy.cpu_time_limit_s == 10
    with pytest.raises(dataclasses.FrozenInstanceError):
        policy.workspace_access = "read_write"
    with pytest.raises(TypeError):
        policy.inputs["other.txt"] = b"data"


@pytest.mark.parametrize("workspace_access", ["read", "read_write"])
@pytest.mark.parametrize("memory_limit_bytes", [0, 1024**3])
async def test_public_launch_gate_precedes_all_provisioning(
    monkeypatch, tmp_path, workspace_access, memory_limit_bytes
):
    from nooa.runtime.sandbox._windows_session import _WindowsSandboxSession
    from nooa.runtime.sandbox.windows import (
        DirectoryGrant,
        WindowsSandboxPolicy,
        WindowsSandboxSession,
    )

    provision = Mock(side_effect=AssertionError("the release gate must precede provisioning"))
    monkeypatch.setattr(_WindowsSandboxSession, "_provision", provision)
    ledger = tmp_path / "ledger"
    source = tmp_path / "missing-source"
    owner = WindowsSandboxSession(
        WindowsSandboxPolicy(
            workspace_access=workspace_access,
            memory_limit_bytes=memory_limit_bytes,
            directories={"source": DirectoryGrant(source)},
            recovery_directory=ledger,
        ),
        config=CodeActConfig(max_iterations=2),
        application_modules={"missing_app": tmp_path / "missing.py"},
    )

    with pytest.raises(SandboxUnavailable, match="public Windows sandbox launch is not enabled"):
        async with owner:
            pytest.fail("the public release gate was bypassed")

    provision.assert_not_called()
    assert not ledger.exists() and not source.exists()
    with pytest.raises(SandboxUnavailable, match="not ready"):
        owner.strategy()
    await owner.aclose()


@pytest.mark.parametrize("settings", [{"require": False}, {"network": True}, {"max_memory_mb": 1}])
def test_public_windows_policy_does_not_translate_linux_fields(settings):
    from nooa.runtime.sandbox.windows import WindowsSandboxPolicy

    with pytest.raises(TypeError):
        WindowsSandboxPolicy(**settings)


@pytest.mark.parametrize("flag", ["enabled", "unsafe", "require"])
def test_public_session_has_no_caller_release_switch(flag):
    from nooa.runtime.sandbox.windows import WindowsSandboxPolicy, WindowsSandboxSession

    with pytest.raises(TypeError):
        WindowsSandboxSession(WindowsSandboxPolicy(), **{flag: True})


def test_public_module_does_not_register_a_codeact_backend():
    from pydantic import ValidationError

    from nooa.runtime.sandbox.windows import WindowsSandboxPolicy  # noqa: F401

    with pytest.raises(ValidationError):
        CodeActConfig(execution_backend="windows")
    with pytest.raises(ValidationError):
        SandboxConfig(start_method="lpac")


def test_public_session_requires_windows_policy_and_valid_generation_config():
    from nooa.runtime.sandbox.windows import WindowsSandboxPolicy, WindowsSandboxSession

    with pytest.raises(TypeError, match="explicit Windows sandbox policy"):
        WindowsSandboxSession(SandboxConfig())
    with pytest.raises(ValueError, match="cell_timeout_s"):
        WindowsSandboxSession(WindowsSandboxPolicy(), config=CodeActConfig(cell_timeout=1))
    with pytest.raises(ValueError):
        WindowsSandboxSession(
            WindowsSandboxPolicy(),
            config=CodeActConfig().model_copy(update={"windows_sandbox": {"enabled": True}}),
        )


async def test_test_admission_does_not_bypass_native_platform_requirement(monkeypatch):
    from nooa.runtime.sandbox import windows
    from nooa.runtime.sandbox._windows_session import _WindowsSandboxSession

    provision = Mock(side_effect=AssertionError("non-Windows must not provision"))
    monkeypatch.setattr(windows, "_require_public_launch", lambda: None)
    monkeypatch.setattr(_WindowsSandboxSession, "_provision", provision)
    monkeypatch.setattr("nooa.runtime.sandbox._windows_session.sys.platform", "linux")

    with pytest.raises(SandboxUnavailable, match="native Windows"):
        async with windows.WindowsSandboxSession(windows.WindowsSandboxPolicy()):
            pytest.fail("platform check was bypassed")
    provision.assert_not_called()
