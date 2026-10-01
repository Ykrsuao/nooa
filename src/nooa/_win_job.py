# SPDX-FileCopyrightText: Copyright (c) 2026, NVIDIA CORPORATION & AFFILIATES. All rights reserved.
# SPDX-License-Identifier: Apache-2.0
"""Parent-owned Windows Job Objects shared by shell and worker launchers.

Resource limits apply to the whole job, including inherited descendants.
They are NOT filesystem, network, token, or handle-access isolation. A launcher
must assign its worker before allowing untrusted code to execute.
"""

from __future__ import annotations

import ctypes
import os
import sys
from ctypes import wintypes

if sys.platform != "win32":
    raise ImportError("Windows Job Objects are only available on Windows")

_JobObjectBasicProcessIdList = 3
_JobObjectExtendedLimitInformation = 9
_JOB_OBJECT_LIMIT_JOB_TIME = 0x0004
_JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x0008
_JOB_OBJECT_LIMIT_PROCESS_MEMORY = 0x0100
_JOB_OBJECT_LIMIT_JOB_MEMORY = 0x0200
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_ERROR_MORE_DATA = 234
_TICKS_PER_SECOND = 10_000_000

_kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)


def _fn(name: str, restype, *argtypes):
    fn = getattr(_kernel32, name)
    fn.restype = restype
    fn.argtypes = argtypes
    return fn


_CreateJobObjectW = _fn("CreateJobObjectW", wintypes.HANDLE, wintypes.LPVOID, wintypes.LPCWSTR)
_SetInformationJobObject = _fn(
    "SetInformationJobObject",
    wintypes.BOOL,
    wintypes.HANDLE,
    ctypes.c_int,
    wintypes.LPVOID,
    wintypes.DWORD,
)
_QueryInformationJobObject = _fn(
    "QueryInformationJobObject",
    wintypes.BOOL,
    wintypes.HANDLE,
    ctypes.c_int,
    wintypes.LPVOID,
    wintypes.DWORD,
    wintypes.LPDWORD,
)
_AssignProcessToJobObject = _fn(
    "AssignProcessToJobObject", wintypes.BOOL, wintypes.HANDLE, wintypes.HANDLE
)
_TerminateJobObject = _fn("TerminateJobObject", wintypes.BOOL, wintypes.HANDLE, wintypes.UINT)
_OpenProcess = _fn("OpenProcess", wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
_TerminateProcess = _fn("TerminateProcess", wintypes.BOOL, wintypes.HANDLE, wintypes.UINT)
_QueryFullProcessImageNameW = _fn(
    "QueryFullProcessImageNameW",
    wintypes.BOOL,
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPWSTR,
    wintypes.LPDWORD,
)
_CloseHandle = _fn("CloseHandle", wintypes.BOOL, wintypes.HANDLE)


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_uint64)
        for name in (
            "ReadOperationCount",
            "WriteOperationCount",
            "OtherOperationCount",
            "ReadTransferCount",
            "WriteTransferCount",
            "OtherTransferCount",
        )
    ]


class _ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


def _check(ok: object) -> None:
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())


def _limit(name: str, value: int, maximum: int) -> int:
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(f"{name} must be an integer between 0 and {maximum}")
    return value


def _image_name(pid: int) -> str:
    handle = _OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if not _QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return ""
        return os.path.basename(buf.value).lower()
    finally:
        _CloseHandle(handle)


def _terminate_pid(pid: int) -> bool:
    handle = _OpenProcess(_PROCESS_TERMINATE, False, pid)
    if not handle:
        return False
    try:
        return bool(_TerminateProcess(handle, 1))
    finally:
        _CloseHandle(handle)


class ProcessJob:
    """Own a process tree and optionally bound its kernel-accounted resources.

    Zero disables each optional limit. ``memory_limit_bytes`` caps committed
    memory for both an individual process and the entire job, INCLUDING the
    interpreter baseline (not RSS or extra allocation headroom).
    ``cpu_time_limit_s`` caps aggregate user-mode CPU time for the job's lifetime,
    not elapsed time or one cell. ``active_process_limit`` includes the root.

    The unnamed, non-inheritable handle stays parent-side. Closing it, including
    on abrupt owner exit, terminates the job. This alone is not a security sandbox.
    Limits cannot be updated through this interface.
    """

    def __init__(
        self,
        *,
        memory_limit_bytes: int = 0,
        cpu_time_limit_s: int = 0,
        active_process_limit: int = 0,
    ) -> None:
        self._handle: int | None = None
        memory = _limit(
            "memory_limit_bytes",
            memory_limit_bytes,
            (1 << (8 * ctypes.sizeof(ctypes.c_size_t))) - 1,
        )
        cpu = _limit("cpu_time_limit_s", cpu_time_limit_s, ((1 << 63) - 1) // _TICKS_PER_SECOND)
        processes = _limit("active_process_limit", active_process_limit, (1 << 32) - 1)
        info = _ExtendedLimitInformation()
        basic = info.BasicLimitInformation
        basic.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if memory:
            basic.LimitFlags |= _JOB_OBJECT_LIMIT_PROCESS_MEMORY | _JOB_OBJECT_LIMIT_JOB_MEMORY
            info.ProcessMemoryLimit = info.JobMemoryLimit = memory
        if cpu:
            basic.LimitFlags |= _JOB_OBJECT_LIMIT_JOB_TIME
            basic.PerJobUserTimeLimit = cpu * _TICKS_PER_SECOND
        if processes:
            basic.LimitFlags |= _JOB_OBJECT_LIMIT_ACTIVE_PROCESS
            basic.ActiveProcessLimit = processes
        handle = _CreateJobObjectW(None, None)
        _check(handle)
        try:
            _check(
                _SetInformationJobObject(
                    handle,
                    _JobObjectExtendedLimitInformation,
                    ctypes.byref(info),
                    ctypes.sizeof(info),
                )
            )
        except OSError:
            _CloseHandle(handle)
            raise
        self._handle = handle

    def _creation_handle(self) -> int:
        """Borrow the handle for PROC_THREAD_ATTRIBUTE_JOB_LIST.

        The trusted launcher must keep this job open until process creation
        completes. This does not transfer ownership or make the handle inheritable.
        """
        if self._handle is None:
            raise RuntimeError("Cannot create a process in a closed job")
        return self._handle

    def assign(self, pid: int) -> None:
        """Add a trusted/bootstrap process; its subsequent children inherit the job.

        Assignment failure raises: callers must terminate the unassigned worker,
        never proceed with an unguarded fallback. Existing descendants are not
        collected, so do not let the worker execute cell code before assignment.
        """
        if self._handle is None:
            raise RuntimeError("Cannot assign a process to a closed job")
        process = _OpenProcess(_PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, pid)
        _check(process)
        try:
            _check(_AssignProcessToJobObject(self._handle, process))
        finally:
            _CloseHandle(process)

    def pids(self) -> list[int]:
        """IDs of the live processes in the job."""
        if self._handle is None:
            return []
        capacity = 64
        while True:

            class _ProcessIdList(ctypes.Structure):
                _fields_ = [
                    ("NumberOfAssignedProcesses", wintypes.DWORD),
                    ("NumberOfProcessIdsInList", wintypes.DWORD),
                    ("ProcessIdList", ctypes.c_size_t * capacity),
                ]

            ids = _ProcessIdList()
            if _QueryInformationJobObject(
                self._handle,
                _JobObjectBasicProcessIdList,
                ctypes.byref(ids),
                ctypes.sizeof(ids),
                None,
            ):
                return list(ids.ProcessIdList[: ids.NumberOfProcessIdsInList])
            if ctypes.get_last_error() != _ERROR_MORE_DATA:
                raise ctypes.WinError(ctypes.get_last_error())
            capacity *= 4

    def kill_descendants(self, root_pid: int) -> bool:
        """Terminate job members except the root and its Windows console host.

        Used by shell interruption: killing conhost.exe would break subsequent
        console commands. Sandbox teardown must use close(), which kills ALL.
        """
        killed = False
        for pid in self.pids():
            if pid == root_pid or _image_name(pid) == "conhost.exe":
                continue
            killed = _terminate_pid(pid) or killed
        return killed

    def close(self) -> None:
        """Kill every process in the job and release it. Idempotent."""
        handle, self._handle = self._handle, None
        if handle is not None:
            _TerminateJobObject(handle, 1)
            _CloseHandle(handle)

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
