# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Explicit native Windows sandbox sessions for sequential Agent calls.

These grants retain their native Windows meanings; they do not translate
Linux SandboxConfig. Importing or constructing a policy does not enable a
backend or provision native resources. Enter a session to provision its LPAC
runtime, then obtain the strategy used by the Agent's generation methods.
"""

from __future__ import annotations

from nooa.runtime.sandbox._lpac_directories import _DirectoryGrant as DirectoryGrant
from nooa.runtime.sandbox._lpac_files import _FileGrant as FileGrant
from nooa.runtime.sandbox._lpac_http import _HttpsEndpoint as HttpsEndpoint
from nooa.runtime.sandbox._windows_capabilities import (
    WindowsSandboxCapabilities,
    probe_windows_sandbox,
)
from nooa.runtime.sandbox._windows_policy import _WindowsSandboxPolicy as WindowsSandboxPolicy
from nooa.runtime.sandbox._windows_session import _WindowsSandboxSession

__all__ = [
    "WindowsSandboxPolicy",
    "WindowsSandboxSession",
    "FileGrant",
    "DirectoryGrant",
    "HttpsEndpoint",
    "WindowsSandboxCapabilities",
    "probe_windows_sandbox",
]


class WindowsSandboxSession(_WindowsSandboxSession):
    """Own a Windows policy's runtime, brokers and sequential Agent calls.

    Supply policy and generation config at construction, enter before obtaining
    a strategy, and await all calls before exit. Entry requires native Windows;
    provisioning failures propagate after owned resources are cleaned up. There
    is no unisolated fallback. Calls use fresh workers and share the disposable
    workspace. Failed cleanup retains ownership for an explicit aclose() retry.
    Policy construction remains available for validation on any platform.
    """
