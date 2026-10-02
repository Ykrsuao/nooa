# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Managed lifecycle shared by the explicit public Windows sandbox session."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from nooa.config import CodeActConfig
from nooa.runtime.sandbox._appcontainer import _AppContainerPython
from nooa.runtime.sandbox._lpac_codeact import _LpacCodeActStrategy
from nooa.runtime.sandbox._lpac_directories import _DirectoryBroker
from nooa.runtime.sandbox._lpac_files import _FileBroker, _finish_file_io
from nooa.runtime.sandbox._lpac_http import _HttpsBroker
from nooa.runtime.sandbox._lpac_runtime import stage_framework
from nooa.runtime.sandbox._windows_context import _render_windows_policy
from nooa.runtime.sandbox._windows_policy import _WindowsSandboxPolicy
from nooa.runtime.sandbox.config import SandboxConfig
from nooa.runtime.sandbox.errors import SandboxUnavailable


def _validate_generation_config(config: CodeActConfig) -> None:
    if not isinstance(config, CodeActConfig):
        raise TypeError("CodeActConfig is required for generation options")
    CodeActConfig.model_validate(dict(config), strict=True)
    if config.sandbox != SandboxConfig() or config.execution_backend != "inprocess":
        raise ValueError("managed LPAC requires its Windows policy, not public backend settings")
    if config.cell_timeout != CodeActConfig().cell_timeout:
        raise ValueError("set cell_timeout_s in the Windows policy")


async def _drain(awaitable):
    """Propagate cancellation only after owned cleanup finishes, including retries."""
    task = asyncio.create_task(awaitable)
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError
    return result


class _WindowsSandboxSession:
    """Own one staged runtime, brokers and sequential Agent calls on one loop.

    Enter before constructing a strategy; await all calls before exiting. Calls
    get separate worker namespaces but share workspace files. Concurrent/nested
    calls and closing during a call fail explicitly, without cancelling callers.
    A failed close retains ownership for a later aclose(); reopening is forbidden.
    Recovery enrollment never automatically adopts or deletes existing resources.
    Supply generation config at construction to check it before provisioning;
    permissions and deadlines come exclusively from the Windows policy.
    """

    def __init__(
        self,
        policy: _WindowsSandboxPolicy,
        *,
        config: CodeActConfig | None = None,
        application_modules: Mapping[str, Path] | None = None,
        application_requirements: Iterable[str] = (),
    ):
        if type(policy) is not _WindowsSandboxPolicy:
            raise TypeError("an explicit Windows sandbox policy is required")
        self._config = CodeActConfig() if config is None else config
        _validate_generation_config(self._config)
        if isinstance(application_requirements, str):
            raise TypeError("application_requirements must be an iterable of requirements")
        self._policy = policy
        self._modules = {
            name: Path(path).absolute() for name, path in (application_modules or {}).items()
        }
        self._requirements = tuple(application_requirements)
        self._runtime: _AppContainerPython | None = None
        self._files: _FileBroker | None = None
        self._directories: _DirectoryBroker | None = None
        self._https: _HttpsBroker | None = None
        self._executors: list[Any] = []
        self._state = "new"
        self._loop = None
        self._active = False
        self._opening = False
        self._closing = False

    @property
    def policy(self):
        return self._policy

    def _check_loop(self):
        if self._loop is not None and asyncio.get_running_loop() is not self._loop:
            raise RuntimeError("Windows sandbox session belongs to another event loop")

    def _retain_runtime(self, runtime):
        self._runtime = runtime

    def _provision(self):
        policy = self.policy
        # Assign ownership in this thread, before returning across a cancellable
        # await. A cancelled to_thread result must never orphan native resources.
        if policy.files:
            self._files = _FileBroker(policy.files, max_file_bytes=policy.max_file_bytes)
        if policy.directories:
            self._directories = _DirectoryBroker(
                policy.directories,
                max_file_bytes=policy.max_file_bytes,
                max_entries=policy.max_directory_entries,
            )
        if policy.https:
            self._https = _HttpsBroker(
                policy.https,
                max_response_bytes=policy.max_response_bytes,
                timeout_s=policy.https_timeout_s,
            )
        runtime = _AppContainerPython(
            inputs=policy.inputs,
            workspace_access=policy.workspace_access,
            recovery_directory=policy.recovery_directory,
            _retain=self._retain_runtime,
        )
        stage_framework(
            runtime,
            application_modules=self._modules,
            application_requirements=self._requirements,
        )

    async def __aenter__(self):
        if self._state != "new":
            raise RuntimeError("Windows sandbox sessions cannot be reopened")
        _validate_generation_config(self._config)
        if sys.platform != "win32":
            raise SandboxUnavailable("managed LPAC requires native Windows")
        self._loop = asyncio.get_running_loop()
        self._state = "opening"
        self._opening = True
        try:
            await _finish_file_io(self._provision)
        except BaseException:
            self._opening = False
            await self.aclose()
            raise
        self._opening = False
        self._state = "ready"
        return self

    async def __aexit__(self, *_):
        await self.aclose()

    def _require_ready(self):
        self._check_loop()
        if self._state != "ready":
            raise SandboxUnavailable("Windows sandbox session is not ready")

    def strategy(self, *, config=None, module_globals=None, data_types=()):
        self._require_ready()
        config = self._config if config is None else config
        _validate_generation_config(config)
        tools = {}
        if self._files is not None:
            tools["read_file"] = self._files.read
            if any(grant.writable for grant in self.policy.files.values()):
                tools["write_file"] = self._files.write
        if self._directories is not None:
            tools["list_directory"] = self._directories.list
            tools["read_directory"] = self._directories.read
            if any(grant.writable for grant in self.policy.directories.values()):
                tools["write_directory"] = self._directories.write
        if self._https is not None:
            tools["fetch_https"] = self._https.fetch
        return _ManagedLpacStrategy(
            self,
            parent_tools=tools,
            config=config.model_copy(update={"cell_timeout": self.policy.cell_timeout_s}),
            module_globals=module_globals,
            data_types=data_types,
        )

    def _begin_call(self):
        self._require_ready()
        if self._active:
            raise SandboxUnavailable("concurrent or nested managed LPAC calls are not supported")
        self._active = True

    async def _close_executor(self, executor):
        await executor.aclose()
        self._executors.remove(executor)

    async def _cleanup(self):
        # Do not release brokers or delete the runtime until every worker and
        # callback has retired. Keep failed resources reachable for retry.
        for executor in tuple(self._executors):
            await self._close_executor(executor)
        if self._directories is not None:
            await self._directories.aclose()
            self._directories = None
        if self._files is not None:
            await self._files.aclose()
            self._files = None
        self._https = None  # Each fetch owns and closes its pool.
        if self._runtime is not None:
            await _finish_file_io(self._runtime.close)
            self._runtime = None
        self._state = "closed"

    async def aclose(self):
        self._check_loop()
        if self._state == "closed":
            return
        if self._opening or self._closing:
            raise RuntimeError("Windows sandbox lifecycle operation already in progress")
        self._state = "closing"
        if self._active:
            raise RuntimeError("await active Agent calls before closing the Windows sandbox")
        self._closing = True
        try:
            await _drain(self._cleanup())
        finally:
            self._closing = False


class _ManagedLpacStrategy(_LpacCodeActStrategy):
    def __init__(self, owner, **kwargs):
        self._owner = owner
        policy = owner.policy
        super().__init__(
            owner._runtime,
            tools=policy.tools,
            tool_policies=policy.tool_policies,
            startup_timeout_s=policy.startup_timeout_s,
            broker_timeout_s=policy.broker_timeout_s,
            frame_timeout_s=policy.frame_timeout_s,
            memory_limit_bytes=policy.memory_limit_bytes,
            cpu_time_limit_s=policy.cpu_time_limit_s,
            recovery=policy.recovery,
            **kwargs,
        )

    def get_block_overrides(self):
        self._owner._require_ready()
        blocks = super().get_block_overrides()
        blocks["sandbox"] = _render_windows_policy(self._owner.policy)
        return blocks

    async def sandbox_context(self, runtime):
        self._owner._require_ready()
        return _render_windows_policy(self._owner.policy)

    async def execute(self, runtime, call):
        self._owner._begin_call()
        try:
            if self.config.cell_timeout != self._owner.policy.cell_timeout_s:
                raise SandboxUnavailable("managed LPAC deadlines cannot be reconfigured")
            return await super().execute(runtime, call)
        finally:
            self._owner._active = False

    def _create_sandbox_executor(self, runtime, call, builtins):
        executor = super()._create_sandbox_executor(runtime, call, builtins)
        self._owner._executors.append(executor)
        return executor

    async def _close_sandbox(self, session):
        executor = session.sandbox_executor
        if executor is not None:
            try:
                await _drain(self._owner._close_executor(executor))
            except BaseException:
                if executor in self._owner._executors:
                    self._owner._state = "closing"
                raise
            finally:
                session.sandbox_executor = None
