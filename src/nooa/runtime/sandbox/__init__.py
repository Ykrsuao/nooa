# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Process-backed, OS-enforced sandbox for CodeAct cell execution.

Public surface:

* :class:`~nooa.runtime.sandbox.config.SandboxConfig` — declarative guardrails.
* :class:`~nooa.runtime.sandbox.executor.SandboxedExecutor` — the parent-side
  process backend that runs cells in a locked-down worker.
* guard errors (:class:`CellTimeoutError`, :class:`CellMemoryError`, ...).

The separate ``nooa.runtime.sandbox.windows`` module provides an explicit native
Windows policy/session interface. Enter ``WindowsSandboxSession`` and pass its
strategy to Agent generation methods; CodeAct's fork-based backend is separate.
"""

from __future__ import annotations

from nooa.runtime.sandbox.config import FileRule, SandboxConfig
from nooa.runtime.sandbox.errors import (
    CellMemoryError,
    CellSerializationError,
    CellTimeoutError,
    SandboxError,
    SandboxExecutionError,
    SandboxUnavailable,
    WorkerDiedError,
)

__all__ = [
    "SandboxConfig",
    "FileRule",
    "SandboxError",
    "SandboxExecutionError",
    "SandboxUnavailable",
    "CellTimeoutError",
    "CellMemoryError",
    "CellSerializationError",
    "WorkerDiedError",
]
