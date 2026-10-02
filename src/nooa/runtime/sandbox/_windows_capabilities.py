# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Read-only native prerequisites, separate from Linux guard capabilities."""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from importlib import import_module
from typing import Literal


@dataclass(frozen=True)
class WindowsSandboxCapabilities:
    """Native binding availability, never evidence of successful containment.

    ``native_api_available`` means the AppContainer and Job Object bindings
    loaded. It does not establish that profiles, launch, ACLs, or token policy
    work on this machine. Only an actual session can exercise those paths.
    """

    native_windows: bool
    native_api_available: bool
    detail: str
    containment_verified: Literal[False] = field(default=False, init=False)


def probe_windows_sandbox() -> WindowsSandboxCapabilities:
    """Inspect platform and native bindings without provisioning a sandbox.

    On Windows, importing the bindings loads DLLs and resolves API symbols;
    no native functions are invoked. No profile, job, process, or sandbox
    files are created. Non-Windows platforms do not import native bindings.
    A successful result is a prerequisite check, not a launch guarantee.
    """
    scope = "No session was started; containment is not verified."
    if sys.platform != "win32":
        return WindowsSandboxCapabilities(
            native_windows=False,
            native_api_available=False,
            detail=f"WindowsSandboxSession requires native Windows. {scope}",
        )
    try:
        # These modules only bind native APIs at import time. In particular,
        # do not instantiate Profile, ProcessJob, or a runtime as a probe.
        import_module("nooa.runtime.sandbox._win_appcontainer")
        import_module("nooa._win_job")
    except Exception as exc:
        return WindowsSandboxCapabilities(
            native_windows=True,
            native_api_available=False,
            detail=f"Native API prerequisites failed to load: {type(exc).__name__}: {exc}. {scope}",
        )
    return WindowsSandboxCapabilities(
        native_windows=True,
        native_api_available=True,
        detail=f"AppContainer and Job Object API bindings loaded. {scope}",
    )
