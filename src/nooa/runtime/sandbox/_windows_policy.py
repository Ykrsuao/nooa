# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Explicit internal Windows grants, not a translation of Linux SandboxConfig."""

from __future__ import annotations

import inspect
import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Literal

from nooa.runtime.sandbox._appcontainer import _input_name
from nooa.runtime.sandbox._lpac import _ToolPolicy
from nooa.runtime.sandbox._lpac_directories import _DirectoryGrant
from nooa.runtime.sandbox._lpac_files import _FileGrant
from nooa.runtime.sandbox._lpac_http import _HttpsEndpoint, _validate_endpoints

_BROKER_TOOLS = frozenset(
    {
        "read_file",
        "write_file",
        "list_directory",
        "read_directory",
        "write_directory",
        "fetch_https",
    }
)


@dataclass(frozen=True)
class _WindowsSandboxPolicy:
    """Creation-time authority and per-worker budgets for a managed LPAC runtime.

    Zero disables native job limits and the parent-tool deadline; None disables
    the cell deadline. Job limits include startup, exclude parent callbacks and
    reset on replacement/new calls. They are not cumulative session quotas.
    Files/directories are broker grants, never direct worker path permissions.
    """

    workspace_access: Literal["read", "read_write"] = "read"
    inputs: Mapping[str, bytes] = field(default_factory=dict)
    files: Mapping[str, _FileGrant] = field(default_factory=dict)
    directories: Mapping[str, _DirectoryGrant] = field(default_factory=dict)
    https: Mapping[str, _HttpsEndpoint] = field(default_factory=dict)
    tools: tuple[str, ...] = ()
    tool_policies: Mapping[str, _ToolPolicy] = field(default_factory=dict)
    cell_timeout_s: float | None = 10
    startup_timeout_s: float = 60
    broker_timeout_s: float = 30
    frame_timeout_s: float = 5
    https_timeout_s: float = 10
    memory_limit_bytes: int = 0
    cpu_time_limit_s: int = 0
    max_file_bytes: int = 1024 * 1024
    max_directory_entries: int = 512
    max_response_bytes: int = 1024 * 1024
    recovery: Literal["restart_empty", "disabled"] = "restart_empty"
    recovery_directory: Path | None = None

    def __post_init__(self):
        for name in ("inputs", "files", "directories", "https", "tool_policies"):
            value = getattr(self, name)
            if not isinstance(value, Mapping):
                raise TypeError(f"{name} must be a mapping")
            object.__setattr__(self, name, MappingProxyType(dict(value)))
        if type(self.workspace_access) is not str or self.workspace_access not in (
            "read",
            "read_write",
        ):
            raise ValueError("workspace_access must be 'read' or 'read_write'")
        if type(self.recovery) is not str or self.recovery not in ("restart_empty", "disabled"):
            raise ValueError("recovery must be 'restart_empty' or 'disabled'")
        if self.recovery_directory is not None:
            object.__setattr__(self, "recovery_directory", Path(self.recovery_directory).absolute())
        for name, data in self.inputs.items():
            if type(name) is not str or type(data) is not bytes:
                raise TypeError("inputs must map filenames to bytes")
            _input_name(name)
        if len({name.casefold() for name in self.inputs}) != len(self.inputs):
            raise ValueError("input snapshots contain case-insensitive filename aliases")
        for field_name, grant_type in (("files", _FileGrant), ("directories", _DirectoryGrant)):
            grants = getattr(self, field_name)
            normalized = {}
            for name, grant in grants.items():
                if type(name) is not str or not name.isidentifier():
                    raise ValueError("resource names must be identifiers")
                if type(grant) is not grant_type or type(grant.writable) is not bool:
                    raise TypeError("grants require a path and boolean writable access")
                normalized[name] = grant_type(Path(grant.path).absolute(), grant.writable)
            object.__setattr__(self, field_name, MappingProxyType(normalized))
        _validate_endpoints(self.https)
        if isinstance(self.tools, str):
            raise TypeError("tools must be an iterable of exact public method names")
        object.__setattr__(self, "tools", tuple(self.tools))
        if any(type(n) is not str or not n.isidentifier() or n.startswith("_") for n in self.tools):
            raise ValueError("tools must be exact public method names")
        if len(set(self.tools)) != len(self.tools) or _BROKER_TOOLS.intersection(self.tools):
            raise ValueError("tools must be unique and cannot shadow managed broker tools")
        if self.tool_policies.keys() - set(self.tools):
            raise ValueError("tool policies must name explicitly granted Agent tools")
        for policy in self.tool_policies.values():
            if not callable(policy):
                raise TypeError("tool policies must be synchronous predicates")
            target = policy if inspect.isroutine(policy) else policy.__call__
            if any(
                check(target)
                for check in (
                    inspect.iscoroutinefunction,
                    inspect.isasyncgenfunction,
                    inspect.isgeneratorfunction,
                )
            ):
                raise TypeError("tool policies must be synchronous predicates")
        for name in (
            "cell_timeout_s",
            "startup_timeout_s",
            "broker_timeout_s",
            "frame_timeout_s",
            "https_timeout_s",
        ):
            value = getattr(self, name)
            if name == "cell_timeout_s" and value is None:
                continue
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or value < 0
                or (value == 0 and name != "broker_timeout_s")
            ):
                raise ValueError(f"{name} must be finite and positive (broker zero is unbounded)")
        for name, minimum, maximum in (
            ("memory_limit_bytes", 0, 2**63 - 1),
            ("cpu_time_limit_s", 0, (2**63 - 1) // 10_000_000),
            ("max_file_bytes", 1, 4 * 1024 * 1024),
            ("max_directory_entries", 1, 4096),
            ("max_response_bytes", 1, 4 * 1024 * 1024),
        ):
            value = getattr(self, name)
            if type(value) is not int or not minimum <= value <= maximum:
                raise ValueError(f"{name} must be an integer between {minimum} and {maximum}")
