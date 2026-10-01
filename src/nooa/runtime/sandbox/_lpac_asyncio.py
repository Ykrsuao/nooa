# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Private-runtime replacement for asyncio.windows_events, NOT a host patch.

CPython's _overlapped initializes Winsock extensions at import time, which LPAC
denies. Retain CPython's selector event loop for tasks/timers/thread wakeups but
use a Windows Event instead of sockets. Async descriptor IO and subprocesses are
intentionally unsupported in this internal worker, never silently unrestricted.
"""

from __future__ import annotations

import ctypes
import math
import selectors
from asyncio import events, selector_events
from ctypes import wintypes

__all__ = (
    "SelectorEventLoop",
    "ProactorEventLoop",
    "DefaultEventLoopPolicy",
    "WindowsSelectorEventLoopPolicy",
    "WindowsProactorEventLoopPolicy",
)


_kernel = ctypes.WinDLL("kernel32", use_last_error=True)
_CreateEvent = _kernel.CreateEventW
_CreateEvent.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
_CreateEvent.restype = wintypes.HANDLE
_SetEvent = _kernel.SetEvent
_SetEvent.argtypes = [wintypes.HANDLE]
_SetEvent.restype = wintypes.BOOL
_Wait = _kernel.WaitForSingleObject
_Wait.argtypes = [wintypes.HANDLE, wintypes.DWORD]
_Wait.restype = wintypes.DWORD
_CloseHandle = _kernel.CloseHandle
_CloseHandle.argtypes = [wintypes.HANDLE]
_CloseHandle.restype = wintypes.BOOL


class _EventSelector(selectors.BaseSelector):
    def __init__(self):
        self.handle = _CreateEvent(None, False, False, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())

    def select(self, timeout=None):
        milliseconds = (
            0xFFFFFFFF if timeout is None else min(0xFFFFFFFE, max(0, math.ceil(timeout * 1000)))
        )
        if _Wait(self.handle, milliseconds) not in (0, 258):
            raise ctypes.WinError(ctypes.get_last_error())
        return []

    def get_map(self):
        return {}

    def get_key(self, fileobj):
        raise KeyError(fileobj)

    def register(self, *args, **kwargs):
        raise NotImplementedError("LPAC asyncio descriptor IO is not supported")

    unregister = register
    modify = register

    def close(self):
        if self.handle is not None:
            _CloseHandle(self.handle)
            self.handle = None


class SelectorEventLoop(selector_events.BaseSelectorEventLoop):
    def __init__(self):
        self._wakeup_selector = _EventSelector()
        super().__init__(self._wakeup_selector)

    def _make_self_pipe(self):
        pass  # _EventSelector already owns the wakeup event.

    def _close_self_pipe(self):
        pass  # BaseSelectorEventLoop closes its selector deterministically.

    def _write_to_self(self):
        selector = self._wakeup_selector
        if selector.handle is not None:
            _SetEvent(selector.handle)  # Concurrent close needs no further wakeup.


class ProactorEventLoop:
    def __init__(self, *args, **kwargs):
        raise NotImplementedError("LPAC does not support CPython's socket-initializing IOCP loop")


class WindowsSelectorEventLoopPolicy(events.BaseDefaultEventLoopPolicy):
    _loop_factory = SelectorEventLoop


class WindowsProactorEventLoopPolicy(events.BaseDefaultEventLoopPolicy):
    _loop_factory = ProactorEventLoop


DefaultEventLoopPolicy = WindowsSelectorEventLoopPolicy
