# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Configuration refusal contract; no native runtime or worker is provisioned."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from nooa.config import CodeActConfig
from nooa.runtime.sandbox._lpac_codeact import _LpacCodeActStrategy
from nooa.runtime.sandbox.config import FileRule, SandboxConfig
from nooa.runtime.sandbox.errors import SandboxUnavailable

_POLICY_CHANGES = {
    "filesystem": False,
    "workspace": "unmapped-workspace",
    "allow": (
        FileRule(path="unmapped-read"),
        FileRule(path="unmapped-write", access="read_write"),
    ),
    "system_paths": False,
    "network": True,
    "max_memory_mb": 1,
    "max_cpu_seconds": 1,
    "rss_poll_s": 0.5,
    "timeout_grace_s": 0,
    "broker_timeout_s": 0,
    "start_method": "spawn",
    "recovery": "disabled",
    "require": False,
    "context_block": False,
}


def test_every_public_policy_field_has_an_explicit_refusal_case():
    assert set(_POLICY_CHANGES) == set(SandboxConfig.model_fields)


@pytest.mark.parametrize("field,value", _POLICY_CHANGES.items(), ids=_POLICY_CHANGES)
@pytest.mark.parametrize("native_limits", [False, True], ids=["no-limits", "native-limits"])
def test_internal_strategy_refuses_public_policy_before_using_runtime(field, value, native_limits):
    policy = SandboxConfig().model_copy(update={field: value})
    assert policy != SandboxConfig()
    # Bypass nested validation too, to exercise the internal strategy's own gate.
    config = CodeActConfig().model_copy(update={"sandbox": policy})
    limits = {"memory_limit_bytes": 1024 * 1024, "cpu_time_limit_s": 1} if native_limits else {}

    # None intentionally proves refusal precedes any access to a caller-owned runtime.
    with pytest.raises(ValueError, match="does not translate public sandbox policy"):
        _LpacCodeActStrategy(None, config=config, **limits)
    assert config.sandbox == policy


@pytest.mark.parametrize("field,value", _POLICY_CHANGES.items(), ids=_POLICY_CHANGES)
async def test_policy_reconfiguration_is_refused_before_codeact_runs(field, value):
    backend = _LpacCodeActStrategy(None)
    if field == "context_block":
        value = True
    policy = backend.config.sandbox.model_copy(update={field: value})
    backend.config = backend.config.model_copy(update={"sandbox": policy})
    with pytest.raises(SandboxUnavailable, match="cannot select another backend"):
        await backend.execute(None, None)


@pytest.mark.parametrize("backend_name", ["inprocess", "spawn", "lpac"])
async def test_backend_reconfiguration_never_falls_back_to_host(backend_name):
    backend = _LpacCodeActStrategy(None)
    backend.config = backend.config.model_copy(update={"execution_backend": backend_name})
    with pytest.raises(SandboxUnavailable, match="cannot select another backend"):
        await backend.execute(None, None)


def test_internal_strategy_does_not_advertise_public_sandbox_constraints():
    backend = _LpacCodeActStrategy(None)
    assert backend.config.execution_backend == "sandbox"
    assert backend.config.sandbox.context_block is False
    assert "sandbox" not in backend.get_block_overrides()


@pytest.mark.parametrize("start_method", ["spawn", "forkserver", "lpac"])
def test_public_schema_does_not_select_internal_start_methods(start_method):
    with pytest.raises(ValidationError):
        SandboxConfig.model_validate({"start_method": start_method})


@pytest.mark.parametrize("backend_name", ["spawn", "lpac"])
def test_public_schema_does_not_register_internal_backends(backend_name):
    with pytest.raises(ValidationError):
        CodeActConfig.model_validate({"execution_backend": backend_name})


@pytest.mark.parametrize(
    "model,settings",
    [
        (CodeActConfig, {"windows_sandbox": {"memory_limit_bytes": 1024}}),
        (SandboxConfig, {"memory_limit_bytes": 1024}),
        (SandboxConfig, {"workspace_access": "read"}),
        (FileRule, {"path": "source", "writable": True}),
        (CodeActConfig, {"sandbox": {"cpu_time_limit_s": 1}}),
    ],
    ids=["windows-policy", "memory-units", "workspace-mode", "file-access", "nested-policy"],
)
def test_public_schema_refuses_unknown_policy_fields(model, settings):
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        model.model_validate(settings)


@pytest.mark.parametrize(
    "config",
    [
        CodeActConfig().model_copy(update={"windows_sandbox": {"memory_limit_bytes": 1024}}),
        CodeActConfig().model_copy(
            update={"sandbox": SandboxConfig().model_copy(update={"memory_limit_bytes": 1024})}
        ),
    ],
    ids=["unknown-config", "unknown-policy"],
)
def test_internal_strategy_refuses_copied_unknown_fields_before_using_runtime(config):
    with pytest.raises(ValueError, match="does not translate public sandbox policy"):
        _LpacCodeActStrategy(None, config=config)
