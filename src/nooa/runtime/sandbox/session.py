# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Managed native sessions with a shared code-sandbox configuration."""

from __future__ import annotations

import math
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from nooa.runtime.sandbox.config import SandboxConfig
from nooa.runtime.sandbox.errors import SandboxUnavailable

if TYPE_CHECKING:
    from nooa.config import CodeActConfig
    from nooa.runtime.sandbox._linux_session import _LinuxSandboxSession
    from nooa.runtime.sandbox.windows import WindowsSandboxPolicy, WindowsSandboxSession
    from nooa.strategies.codeact import CodeActStrategy


class SandboxSession:
    """Own sequential Agent calls through this host's native sandbox backend.

    Both hosts accept ``SandboxConfig(require=True)`` for code isolation with
    trusted host tools. Windows additionally accepts ``WindowsSandboxPolicy``
    for native grants and resource units. Linux-only direct path grants and
    resource semantics are rejected on Windows instead of silently translated.
    This interface does not isolate host-side tools or turn the Linux fork
    backend into a credentials boundary.

    Enter before obtaining a strategy, then await calls before closing. Each
    call has fresh worker globals; workspace files persist between calls.
    Unsupported hosts, mismatched policies and unavailable guards fail closed.
    Windows-only staging and strategy options are rejected on Linux.
    With a shared policy, set the cell deadline when constructing the session;
    per-strategy configuration cannot change that session budget on either host.

    With ``SandboxConfig``, ``tools=None`` keeps the existing live Agent proxy;
    an explicit tuple grants only those exact public parent methods (``()``
    denies all). Explicit Windows-native policies carry their own tool grants.
    """

    def __init__(
        self,
        policy: SandboxConfig | WindowsSandboxPolicy,
        *,
        backend: Literal["auto", "linux", "windows"] = "auto",
        config: CodeActConfig | None = None,
        application_modules: Mapping[str, Path] | None = None,
        application_requirements: Iterable[str] = (),
        tools: Iterable[str] | None = None,
    ):
        if backend not in ("auto", "linux", "windows"):
            raise ValueError("sandbox backend must be 'auto', 'linux', or 'windows'")
        native = {"linux": "linux", "win32": "windows"}.get(sys.platform)
        if native is None:
            raise SandboxUnavailable("managed sandboxes support native Linux and Windows only")
        if backend != "auto" and backend != native:
            raise SandboxUnavailable(f"the {backend} sandbox cannot run on {sys.platform}")
        self._backend: Literal["linux", "windows"] = "linux" if native == "linux" else "windows"
        self._policy = policy
        self._shared_policy = type(policy) is SandboxConfig
        self._generation_config = config
        if self._shared_policy and config is not None:
            _validate_shared_generation(config)
        self._session: _LinuxSandboxSession | WindowsSandboxSession
        if self._backend == "linux":
            from nooa.runtime.sandbox._linux_session import _LinuxSandboxSession

            if application_modules is not None or tuple(application_requirements):
                raise ValueError("application staging is a Windows sandbox option")
            if type(policy) is not SandboxConfig:
                raise TypeError("the Linux backend requires SandboxConfig")
            self._session = _LinuxSandboxSession(policy, config=config, tools=tools)
        else:
            from nooa.runtime.sandbox.windows import WindowsSandboxPolicy, WindowsSandboxSession

            if type(policy) is SandboxConfig:
                policy, config = _windows_code_policy(policy, config, tools)
            elif type(policy) is not WindowsSandboxPolicy:
                raise TypeError(
                    "the Windows backend requires SandboxConfig or WindowsSandboxPolicy"
                )
            elif tools is not None:
                raise ValueError("Windows tool grants belong in WindowsSandboxPolicy.tools")
            self._session = WindowsSandboxSession(
                policy,
                config=config,
                application_modules=application_modules,
                application_requirements=application_requirements,
            )

    @property
    def backend(self) -> Literal["linux", "windows"]:
        return self._backend

    @property
    def policy(self) -> SandboxConfig | WindowsSandboxPolicy:
        return self._policy

    async def __aenter__(self) -> SandboxSession:
        await self._session.__aenter__()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Release owned resources; failed cleanup can be retried."""
        await self._session.aclose()

    def strategy(
        self,
        *,
        config: CodeActConfig | None = None,
        module_globals: Mapping[str, Any] | None = None,
        data_types: Iterable[type] = (),
    ) -> CodeActStrategy:
        """Create a strategy bound to this open session and its native policy."""
        if self._shared_policy and config is not None:
            from nooa.config import CodeActConfig

            _validate_shared_generation(config)
            expected = self._generation_config or CodeActConfig()
            if config.cell_timeout != expected.cell_timeout:
                raise ValueError("set cell_timeout when creating the sandbox session")
            if self._backend == "windows":
                config = config.model_copy(update={"cell_timeout": CodeActConfig().cell_timeout})
        return self._session.strategy(
            config=config, module_globals=module_globals, data_types=data_types
        )


def _validate_shared_generation(config: CodeActConfig) -> None:
    from nooa.config import CodeActConfig

    if not isinstance(config, CodeActConfig):
        raise TypeError("CodeActConfig is required for generation options")
    CodeActConfig.model_validate(dict(config), strict=True)
    if config.cell_timeout is not None and (
        not math.isfinite(config.cell_timeout) or config.cell_timeout <= 0
    ):
        raise ValueError("cell_timeout must be None or finite and positive")
    if config.execution_backend != "inprocess" or config.sandbox != SandboxConfig():
        raise ValueError("supply sandbox permissions as the session policy, not generation config")


def _windows_code_policy(policy: SandboxConfig, config, tools):
    """Resolve only shared semantics; never fabricate Linux filesystem/rlimit parity."""
    from nooa.config import CodeActConfig
    from nooa.runtime.sandbox.windows import WindowsSandboxPolicy

    policy = SandboxConfig.model_validate(dict(policy), strict=True)
    if not policy.require:
        raise ValueError("managed sandbox sessions require require=True")
    defaults = SandboxConfig()
    native_only = (
        "workspace",
        "allow",
        "filesystem",
        "system_paths",
        "max_memory_mb",
        "max_cpu_seconds",
        "rss_poll_s",
    )
    unsupported = [name for name in native_only if getattr(policy, name) != getattr(defaults, name)]
    if unsupported:
        raise SandboxUnavailable(
            "Windows code sandbox: unsupported Linux policy fields: "
            + ", ".join(unsupported)
            + ". Use host tools for project access; WindowsSandboxPolicy exposes native limits."
        )
    config = CodeActConfig() if config is None else config
    _validate_shared_generation(config)
    if isinstance(tools, str):
        raise TypeError("tools must be an iterable of public method names")
    native_policy = WindowsSandboxPolicy(
        network=policy.network,
        host_tools=tools is None,
        tools=() if tools is None else tuple(tools),
        cell_timeout_s=config.cell_timeout,
        timeout_grace_s=policy.timeout_grace_s,
        broker_timeout_s=policy.broker_timeout_s,
        recovery=policy.recovery,
        context_block=policy.context_block,
    )
    return native_policy, config.model_copy(update={"cell_timeout": CodeActConfig().cell_timeout})
