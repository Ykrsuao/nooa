# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Process-backed, OS-enforced sandbox for CodeAct cell execution.

Public surface:

* :class:`~nooa.runtime.sandbox.config.SandboxConfig` — declarative guardrails.
* :class:`~nooa.runtime.sandbox.executor.SandboxedExecutor` — the parent-side
  process backend that runs cells in a locked-down worker.
* guard errors (:class:`CellTimeoutError`, :class:`CellMemoryError`, ...).

``SandboxSession`` selects the native Linux or Windows backend. Both accept
``SandboxConfig`` for code isolation with trusted host tools; unsupported
Linux-specific path/resource policies fail explicitly on Windows. The
``nooa.runtime.sandbox.windows`` module also provides the
Windows-specific policy/session interface. Enter a session and pass its strategy
to Agent generation methods; host-side tools keep their native trust semantics.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

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

if TYPE_CHECKING:
    from nooa.runtime.sandbox.session import SandboxSession


def __getattr__(name: str):
    # CodeActConfig imports sandbox.config while strategies are still loading.
    if name == "SandboxSession":
        from nooa.runtime.sandbox.session import SandboxSession

        return SandboxSession
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "SandboxSession",
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
