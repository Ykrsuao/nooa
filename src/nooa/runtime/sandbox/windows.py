# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Staged Windows interface. Public launch is disabled pending release acceptance.

These grants retain their native Windows meanings; they do not translate
Linux SandboxConfig. Importing or constructing a policy does not enable a
backend, provision native resources, or change CodeAct/doctor availability.
"""

from __future__ import annotations

from typing import Self

from nooa.runtime.sandbox._lpac_directories import _DirectoryGrant as DirectoryGrant
from nooa.runtime.sandbox._lpac_files import _FileGrant as FileGrant
from nooa.runtime.sandbox._lpac_http import _HttpsEndpoint as HttpsEndpoint
from nooa.runtime.sandbox._windows_policy import _WindowsSandboxPolicy as WindowsSandboxPolicy
from nooa.runtime.sandbox._windows_session import _WindowsSandboxSession
from nooa.runtime.sandbox.errors import SandboxUnavailable

__all__ = [
    "WindowsSandboxPolicy",
    "WindowsSandboxSession",
    "FileGrant",
    "DirectoryGrant",
    "HttpsEndpoint",
]


def _require_public_launch() -> None:
    raise SandboxUnavailable(
        "The public Windows sandbox launch is not enabled; release acceptance is incomplete. "
        "There is no caller opt-in or unisolated fallback."
    )


class WindowsSandboxSession(_WindowsSandboxSession):
    """Own a Windows policy's runtime, brokers and sequential Agent calls.

    Supply policy and generation config at construction, enter before obtaining
    a strategy, and await all calls before exit. Lifecycle and cleanup are shared
    with the managed implementation. Entry currently always raises
    SandboxUnavailable before provisioning; no environment or constructor option
    enables it. Policy construction remains available for validation.
    """

    async def __aenter__(self) -> Self:
        _require_public_launch()
        return await super().__aenter__()
