# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Internal, explicitly provisioned CodeAct integration, not public backend selection."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Any, Literal

from pydantic import ValidationError

from nooa.config import CodeActConfig
from nooa.runtime.sandbox._appcontainer import _AppContainerPython
from nooa.runtime.sandbox._lpac import _LpacExecutor, _ToolPolicy
from nooa.runtime.sandbox.config import SandboxConfig
from nooa.runtime.sandbox.errors import SandboxUnavailable
from nooa.strategies.codeact import CodeActStrategy


class _LpacCodeActStrategy(CodeActStrategy):
    """Use a caller-owned staged runtime and exact tool/data grants for each call.

    The owner must await every Agent call before closing the runtime. Calls using
    this runtime share its filesystem identity/workspace, but not worker globals.
    Imported code and data-type validators are trusted application code. No public
    SandboxConfig policy is translated here. Native job limits include worker
    startup, exclude parent tools, and reset on worker replacement or a new Agent
    call. They are not session-wide quotas; recovery="disabled" refuses worker
    replacement after failure within a call.
    """

    def __init__(
        self,
        runtime: _AppContainerPython,
        *,
        tools: Iterable[str] = (),
        parent_tools: Mapping[str, Callable[..., Any]] | None = None,
        tool_policies: Mapping[str, _ToolPolicy] | None = None,
        module_globals: Mapping[str, Any] | None = None,
        data_types: Iterable[type] = (),
        config: CodeActConfig | None = None,
        startup_timeout_s: float = 60,
        broker_timeout_s: float = 30,
        frame_timeout_s: float = 5,
        memory_limit_bytes: int = 0,
        cpu_time_limit_s: int = 0,
        recovery: Literal["restart_empty", "disabled"] = "restart_empty",
    ):
        config = config or CodeActConfig(cell_timeout=10)
        try:
            CodeActConfig.model_validate(dict(config), strict=True)
        except ValidationError:
            raise ValueError(
                "internal LPAC strategy does not translate public sandbox policy "
                "or accept invalid CodeAct configuration"
            ) from None
        if config.sandbox != SandboxConfig():
            raise ValueError("internal LPAC strategy does not translate public sandbox policy")
        super().__init__(
            config.model_copy(
                update={
                    "execution_backend": "sandbox",
                    "sandbox": SandboxConfig(context_block=False),
                }
            )
        )
        self._runtime = runtime
        self._agent_tools = tuple(tools)
        self._parent_tools = dict(parent_tools or {})
        if set(self._agent_tools).intersection(self._parent_tools):
            raise ValueError("LPAC parent tools cannot replace Agent tools")
        self._tools = (*self._agent_tools, *self._parent_tools)
        if any(
            not isinstance(name, str) or not name.isidentifier() or name.startswith("_")
            for name in self._tools
        ):
            raise ValueError("LPAC tools must be exact public method names")
        self._module_globals = dict(module_globals or {})
        self._tool_policies = dict(tool_policies or {})
        if self._tool_policies.keys() - set(self._tools):
            raise ValueError("LPAC policies must name explicitly granted tools")
        self._data_types = tuple(data_types)
        self._startup_timeout_s = startup_timeout_s
        self._broker_timeout_s = broker_timeout_s
        self._frame_timeout_s = frame_timeout_s
        self._memory_limit_bytes = memory_limit_bytes
        self._cpu_time_limit_s = cpu_time_limit_s
        self._recovery: Literal["restart_empty", "disabled"] = recovery

    async def execute(self, runtime, call):
        if self.config.execution_backend != "sandbox" or self.config.sandbox != SandboxConfig(
            context_block=False
        ):
            raise SandboxUnavailable("internal LPAC configuration cannot select another backend")
        return await super().execute(runtime, call)

    def _extract_module_context(self, agent_module, agent=None):
        return dict(self._module_globals)

    def get_block_overrides(self):
        blocks = super().get_block_overrides()
        # Do not advertise the live agent's ungranted fields/methods as tools.
        blocks["self"] = "Available parent tools: " + ", ".join(
            f"self.{name}" for name in self._tools
        )
        return blocks

    def _create_sandbox_executor(self, runtime, call, builtins):
        if call.session_locals is not None:
            raise SandboxUnavailable("LPAC does not synchronize caller-owned session_locals")
        reserved = {"self", "_call", "return_result", "doc", "help", "methods", "variables"}
        if reserved.intersection(self._module_globals) or reserved.intersection(call.kwargs):
            raise SandboxUnavailable("LPAC application names overlap reserved framework names")
        if any(callable(value) for value in call.kwargs.values()):
            raise SandboxUnavailable("LPAC method arguments must be data snapshots, not callbacks")
        tools = {
            **self._parent_tools,
            **{name: getattr(runtime.agent, name) for name in self._agent_tools},
        }
        return _LpacExecutor(
            self._runtime,
            tools=tools,
            tool_policies=self._tool_policies,
            module_globals=self._module_globals,
            framework_builtins={
                **call.kwargs,
                "return_result": builtins["return_result"],
                "_call": call,
            },
            data_types=self._data_types,
            tool_introspection=True,
            restrictions=self.config.restrictions,
            cell_timeout=self.config.cell_timeout,
            startup_timeout_s=self._startup_timeout_s,
            broker_timeout_s=self._broker_timeout_s,
            frame_timeout_s=self._frame_timeout_s,
            memory_limit_bytes=self._memory_limit_bytes,
            cpu_time_limit_s=self._cpu_time_limit_s,
            recovery=self._recovery,
            max_error=runtime.truncation_config.capture.max_error,
            error_tail=runtime.truncation_config.capture.tail,
        )
