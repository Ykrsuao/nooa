# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Public Windows policy, native admission and fail-closed session contract."""

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
async def test_public_entry_owns_lifecycle_without_a_release_switch(
    monkeypatch, tmp_path, workspace_access, memory_limit_bytes
):
    from nooa.runtime.sandbox._windows_session import _WindowsSandboxSession
    from nooa.runtime.sandbox.windows import (
        DirectoryGrant,
        WindowsSandboxPolicy,
        WindowsSandboxSession,
    )

    provision = Mock()
    monkeypatch.setattr("nooa.runtime.sandbox._windows_session.sys.platform", "win32")
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

    with pytest.raises(SandboxUnavailable, match="not ready"):
        owner.strategy()
    async with owner as entered:
        assert entered is owner
        assert owner._state == "ready"
        provision.assert_called_once_with()

    assert owner._state == "closed"
    assert not ledger.exists() and not source.exists()
    with pytest.raises(SandboxUnavailable, match="not ready"):
        owner.strategy()
    await owner.aclose()
    with pytest.raises(RuntimeError, match="cannot be reopened"):
        await owner.__aenter__()


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


async def test_public_entry_requires_native_windows_before_provisioning(monkeypatch):
    from nooa.runtime.sandbox import windows
    from nooa.runtime.sandbox._windows_session import _WindowsSandboxSession

    provision = Mock(side_effect=AssertionError("non-Windows must not provision"))
    monkeypatch.setattr(_WindowsSandboxSession, "_provision", provision)
    monkeypatch.setattr("nooa.runtime.sandbox._windows_session.sys.platform", "linux")

    with pytest.raises(SandboxUnavailable, match="native Windows"):
        async with windows.WindowsSandboxSession(windows.WindowsSandboxPolicy()):
            pytest.fail("platform check was bypassed")
    provision.assert_not_called()


async def test_public_entry_revalidates_generation_config_before_provisioning(monkeypatch):
    from nooa.runtime.sandbox._windows_session import _WindowsSandboxSession
    from nooa.runtime.sandbox.windows import WindowsSandboxPolicy, WindowsSandboxSession

    provision = Mock(side_effect=AssertionError("invalid config must not provision"))
    monkeypatch.setattr(_WindowsSandboxSession, "_provision", provision)
    config = CodeActConfig()
    owner = WindowsSandboxSession(WindowsSandboxPolicy(), config=config)
    # Simulate mutation bypassing the frozen model's normal assignment guard.
    object.__setattr__(config, "execution_backend", "sandbox")
    with pytest.raises(ValueError, match="Windows policy"):
        await owner.__aenter__()
    provision.assert_not_called()
    await owner.aclose()


@pytest.mark.parametrize("error", [SandboxUnavailable("native setup denied"), OSError("disk full")])
async def test_public_provisioning_failure_propagates_without_fallback(monkeypatch, error):
    from nooa.runtime.sandbox._windows_session import _WindowsSandboxSession
    from nooa.runtime.sandbox.windows import WindowsSandboxPolicy, WindowsSandboxSession

    monkeypatch.setattr("nooa.runtime.sandbox._windows_session.sys.platform", "win32")
    close = Mock()

    def provision(owner):
        owner._runtime = Mock(close=close)
        raise error

    monkeypatch.setattr(_WindowsSandboxSession, "_provision", provision)
    owner = WindowsSandboxSession(WindowsSandboxPolicy())
    with pytest.raises(type(error), match=str(error)) as caught:
        await owner.__aenter__()
    assert caught.value is error
    close.assert_called_once_with()
    assert owner._state == "closed" and owner._runtime is None
    with pytest.raises(SandboxUnavailable, match="not ready"):
        owner.strategy()
